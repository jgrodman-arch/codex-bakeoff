"""Ensure reviewer responses do not leak into durable controller logs."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SERVER_PATH = Path(__file__).resolve().parent.parent / "mcp" / "server.py"
REPLAY_PATH = SERVER_PATH.parent.parent / "scripts" / "historical_bakeoff.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("replay_reviewer_privacy_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the MCP server.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_replay():
    spec = importlib.util.spec_from_file_location("replay_reviewer_privacy_engine", REPLAY_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the replay engine.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewerPrivacyTests(unittest.TestCase):
    def test_reviewer_stdout_is_captured_without_persisting_reviewer_text(self) -> None:
        server = _load_server()
        response = '{"finalResponse":"PRIVATE_REVIEWER_EXPLANATION"}\n'

        for source in (
            "review:codex:stdout",
            "normalization:codex-for-codex:stdout",
        ):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "run.log"
                stream = io.StringIO(response)
                chunks: list[str] = []

                server._stream_process_output(stream, chunks, path, source)

                self.assertEqual(chunks, [response])
                self.assertTrue(stream.closed)
                log = path.read_text(encoding="utf-8")
                self.assertIn("[reviewer output omitted]", log)
                self.assertNotIn("PRIVATE_REVIEWER_EXPLANATION", log)

    def test_implementation_output_and_reviewer_diagnostics_remain_visible(self) -> None:
        server = _load_server()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.log"

            server._append_run_log(path, "implementation:stdout", "implementation progress")
            server._append_run_log(path, "review:codex:stderr", "reviewer connection failed")

            log = path.read_text(encoding="utf-8")
            self.assertIn("implementation progress", log)
            self.assertIn("reviewer connection failed", log)

    def test_codex_reviewer_and_normalizer_results_are_sanitized_before_persistence(self) -> None:
        server = _load_server()
        replay = _load_replay()
        execution = replay._execution()
        reviewer_explanation = "PRIVATE_CODEX_REVIEWER_EXPLANATION"

        for normalization in (False, True):
            with (
                self.subTest(normalization=normalization),
                tempfile.TemporaryDirectory() as temporary,
            ):
                run_directory = Path(temporary).resolve()
                (run_directory / "run.json").write_text("{}\n", encoding="utf-8")
                review_directory = run_directory / "reviews"
                review_directory.mkdir()
                candidate_paths = []
                for label in ("a", "b"):
                    path = review_directory / f"candidate-{label}.json"
                    path.write_text(json.dumps({"label": label.upper()}), encoding="utf-8")
                    candidate_paths.append(str(path))

                ballot = {
                    "dimensions": {
                        dimension: {
                            "candidates": {
                                label: {"checks": {check: 1 for check in checks}}
                                for label in ("A", "B")
                            }
                        }
                        for dimension, checks in execution.REVIEW_DIMENSION_CHECKS.items()
                    }
                }
                if not normalization:
                    ballot["explanation"] = reviewer_explanation

                raw_result = {
                    "status": "completed",
                    "model": "gpt-test",
                    "thread_id": "review-thread",
                    "final_output": json.dumps(ballot),
                    "final_response": reviewer_explanation,
                    "explanation": reviewer_explanation,
                }
                if not normalization:
                    raw_result["evaluator"] = "codex"

                request = {
                    "purpose": "review_normalization" if normalization else "evaluation",
                    "model": "gpt-test",
                    "expected_schema": execution.REVIEW_BALLOT_JSON_SCHEMA,
                }
                if normalization:
                    request["normalization_for"] = "codex"
                    request["prompt"] = "Normalize the existing Codex reviewer ballot."
                else:
                    request["evaluator"] = "codex"
                    request["prompt"] = f"Read {candidate_paths[0]} and {candidate_paths[1]}"
                    request["candidate_paths"] = candidate_paths

                def run_worker(
                    worker_request,
                    *,
                    run_directory,
                    working_directory,
                    read_only,
                    log_label,
                    is_normalization=normalization,
                    source_paths=candidate_paths,
                    collected_result=raw_result,
                ):
                    self.assertTrue(read_only)
                    self.assertEqual(
                        log_label,
                        "normalization:codex-for-codex" if is_normalization else "review:codex",
                    )
                    if is_normalization:
                        self.assertEqual(list(working_directory.iterdir()), [])
                    else:
                        self.assertEqual(
                            sorted(path.name for path in working_directory.iterdir()),
                            ["candidate-a.json", "candidate-b.json"],
                        )
                        self.assertNotIn(source_paths[0], worker_request["prompt"])
                        self.assertNotIn(source_paths[1], worker_request["prompt"])
                    collected_result["worktree"] = str(working_directory)
                    return {"thread_id": "review-thread", "worktree": str(working_directory)}

                def collect(command, arguments=(), **_kwargs):
                    self.assertEqual(command, "collect-native-result")
                    parsed = replay.build_parser().parse_args([command, *arguments, "--json"])
                    return replay._command_collect_native_result(parsed)

                with (
                    mock.patch.object(server, "_run_worker", side_effect=run_worker) as worker,
                    mock.patch.object(server, "_engine", side_effect=collect),
                    mock.patch.object(
                        execution,
                        "collect_native_task_result",
                        return_value=raw_result,
                    ),
                ):
                    paths = server._run_review_requests(
                        run_directory,
                        [request],
                        normalization=normalization,
                    )

                worker.assert_called_once()
                persisted = paths[0].read_text(encoding="utf-8")
                self.assertNotIn(reviewer_explanation, persisted)
                self.assertNotIn("final_output", persisted)
                self.assertNotIn("final_response", persisted)
                self.assertNotIn("explanation", persisted)
                stored = json.loads(persisted)
                self.assertEqual(len(stored["ballot"]["dimensions"]), 6)
                if normalization:
                    self.assertEqual(stored["normalization_for"], "codex")
                else:
                    self.assertEqual(stored["evaluator"], "codex")
                    self.assertTrue(stored["normalization_required"])

    def test_removed_claude_reviewer_is_rejected_without_starting_a_worker(self) -> None:
        server = _load_server()

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(server, "_run_worker") as worker:
                with self.assertRaisesRegex(server.ControllerError, "Unsupported review evaluator"):
                    server._run_review_requests(
                        Path(temporary),
                        [{"evaluator": "claude", "model": "sonnet", "prompt": "Review"}],
                    )

                worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
