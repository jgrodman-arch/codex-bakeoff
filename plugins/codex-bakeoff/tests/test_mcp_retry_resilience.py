"""Focused retry coverage for Codex Bakeoff MCP workers."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PLUGIN_ROOT / "mcp" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("replay_controller_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the controller server.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class McpRetryResilienceTests(unittest.TestCase):
    def test_reviews_retry_retryable_worker_failures(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            reviews = run_directory / "reviews"
            reviews.mkdir()
            candidate_paths = []
            for label in ("a", "b"):
                path = reviews / f"candidate-{label}.json"
                path.write_text(json.dumps({"label": label.upper()}), encoding="utf-8")
                candidate_paths.append(str(path))
            prompt = f"Read {candidate_paths[0]} and {candidate_paths[1]}"
            calls = []

            def fake_worker(
                request,
                *,
                run_directory,
                working_directory,
                read_only,
                log_label,
            ):
                calls.append(
                    {
                        "workspace": str(working_directory),
                        "label": log_label,
                        "files": sorted(item.name for item in working_directory.iterdir()),
                    }
                )
                self.assertTrue(read_only)
                self.assertNotIn(candidate_paths[0], request["prompt"])
                self.assertNotIn(candidate_paths[1], request["prompt"])
                if len(calls) == 1:
                    raise server.WorkerError(
                        "stream_error",
                        "stream_error: connection closed",
                        retryable=True,
                    )
                return {"thread_id": "review-thread", "worktree": str(working_directory)}

            with (
                mock.patch.object(server, "_run_worker", side_effect=fake_worker),
                mock.patch.object(
                    server,
                    "_collect_result",
                    return_value={"native_result_path": str(reviews / "result.json")},
                ) as collect_result,
            ):
                server._run_review_requests(
                    run_directory,
                    [
                        {
                            "purpose": "evaluation",
                            "evaluator": "codex",
                            "model": "gpt-test",
                            "prompt": prompt,
                            "candidate_paths": candidate_paths,
                        }
                    ],
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["workspace"], calls[1]["workspace"])
            self.assertEqual(calls[0]["label"], "review:codex")
            self.assertEqual(calls[1]["label"], "review:codex:retry-1")
            self.assertEqual(calls[0]["files"], ["candidate-a.json", "candidate-b.json"])
            self.assertEqual(calls[0]["files"], calls[1]["files"])
            collect_result.assert_called_once()
            self.assertIn(
                "review:codex attempt 1 failed with retryable stream_error",
                server._run_log_path(run_directory).read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
