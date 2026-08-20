"""Recorded Claude Code sessions retain their genuine repository and run evidence."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = PLUGIN_ROOT / "scripts" / "claude_code_sample_loader.py"
MODULE_SPEC = importlib.util.spec_from_file_location("claude_code_sample_loader_test", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
loader = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = loader
MODULE_SPEC.loader.exec_module(loader)


def load_server():
    path = PLUGIN_ROOT / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("claude_code_sample_server_test", path)
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)
    return server


class ClaudeCodeSampleLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.records = self.assets / "records" / "jq-2919--claude-sonnet-5"
        self.records.mkdir(parents=True)
        self.index = self.assets / "index.json"
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.git("init", "--quiet", str(self.upstream))
        self.git("-C", str(self.upstream), "config", "user.name", "Claude sample fixture")
        self.git("-C", str(self.upstream), "config", "user.email", "sample@example.invalid")
        (self.upstream / "program.txt").write_text("before\n", encoding="utf-8")
        self.git("-C", str(self.upstream), "add", "program.txt")
        self.git("-C", str(self.upstream), "commit", "--quiet", "--no-gpg-sign", "-m", "Baseline")
        self.baseline = self.git("-C", str(self.upstream), "rev-parse", "HEAD")
        (self.upstream / "program.txt").write_text("after\n", encoding="utf-8")
        self.patch = self.git("-C", str(self.upstream), "diff") + "\n"
        self.git("-C", str(self.upstream), "checkout", "--", "program.txt")

        prompt = {
            "type": "user",
            "uuid": "prompt-1",
            "sessionId": "claude-session-1",
            "cwd": loader.REPOSITORY_PATH_MARKER,
            "timestamp": "2026-08-11T10:00:00Z",
            "message": {"role": "user", "content": "Fix argument parsing."},
        }
        response = {
            "type": "assistant",
            "uuid": "response-1",
            "parentUuid": "prompt-1",
            "requestId": "request-1",
            "sessionId": "claude-session-1",
            "cwd": loader.REPOSITORY_PATH_MARKER,
            "timestamp": "2026-08-11T10:00:12Z",
            "message": {
                "id": "message-1",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 14, "output_tokens": 32},
                "content": [
                    {"type": "text", "text": "Updated argument parsing."},
                    {
                        "type": "tool_use",
                        "id": "edit-1",
                        "name": "Edit",
                        "input": {"file_path": "program.txt"},
                    },
                ],
            },
        }
        (self.records / "transcript.000.jsonl").write_text(
            json.dumps(prompt) + "\n",
            encoding="utf-8",
        )
        (self.records / "transcript.001.jsonl").write_text(
            json.dumps(response) + "\n",
            encoding="utf-8",
        )
        (self.records / "patch.diff").write_text(self.patch, encoding="utf-8")
        self.result = {
            "type": "result",
            "subtype": "success",
            "session_id": "claude-session-1",
            "duration_ms": 12_300,
            "duration_api_ms": 9_100,
            "total_cost_usd": 0.027,
            "usage": {
                "input_tokens": 14,
                "output_tokens": 32,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "modelUsage": {
                "claude-sonnet-5": {
                    "inputTokens": 14,
                    "outputTokens": 32,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "costUSD": 0.027,
                }
            },
        }
        self.write_result(self.result)
        self.sample = {
            "id": "jq-2919--claude-sonnet-5",
            "task_id": "jq-2919",
            "title": "Allow scripts after --",
            "repository": "jqlang/jq",
            "baseline_commit": self.baseline,
            "prompt": "Fix argument parsing.",
            "source_url": "https://github.com/jqlang/jq/pull/2919",
            "model": "claude-sonnet-5",
            "model_label": "Claude Sonnet 5",
            "session_id": "claude-session-1",
            "transcript_parts": [
                "records/jq-2919--claude-sonnet-5/transcript.000.jsonl",
                "records/jq-2919--claude-sonnet-5/transcript.001.jsonl",
            ],
            "patch_path": "records/jq-2919--claude-sonnet-5/patch.diff",
            "result_path": "records/jq-2919--claude-sonnet-5/result.json",
            "recorded_at": "2026-08-11T10:00:12Z",
            "duration_ms": 12_300,
            "duration_api_ms": 9_100,
            "total_cost_usd": 0.027,
            "verification": {"passed": True},
            "original_repository_path_marker": loader.REPOSITORY_PATH_MARKER,
        }
        self.write_index([self.sample])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()

    def write_index(self, samples: list[dict]) -> None:
        self.index.write_text(
            json.dumps({"schema_version": 1, "samples": samples}),
            encoding="utf-8",
        )

    def write_result(self, payload: dict) -> None:
        (self.records / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_optional_catalog_has_no_sample_threads(self) -> None:
        self.assertEqual(loader.load_samples(self.root / "missing.json"), [])
        self.assertEqual(loader.list_sample_threads(self.root / "missing.json"), [])

    def test_threads_identify_genuine_claude_session_without_disclosing_prompt_or_patch(
        self,
    ) -> None:
        (thread,) = loader.list_sample_threads(self.index)

        self.assertEqual(thread["imported_thread_id"], "claude-sample:jq-2919--claude-sonnet-5")
        self.assertEqual(thread["session_id"], "claude-session-1")
        self.assertEqual(thread["claude_model"], "claude-sonnet-5")
        self.assertEqual(thread["duration_ms"], 12_300)
        self.assertEqual(thread["total_cost_usd"], 0.027)
        self.assertNotIn("prompt", thread)
        self.assertNotIn("patch", thread)

    def test_original_prompt_whitespace_is_preserved_exactly(self) -> None:
        exact_prompt = "Fix argument parsing.\r\n"
        self.write_index([{**self.sample, "prompt": exact_prompt}])

        (sample,) = loader.load_samples(self.index)

        self.assertEqual(sample.prompt, exact_prompt)

    def test_missing_or_fabricated_run_metrics_are_rejected_before_listing(self) -> None:
        for field in ("duration_ms", "duration_api_ms", "total_cost_usd"):
            with self.subTest(missing_index_field=field):
                invalid = dict(self.sample)
                invalid.pop(field)
                self.write_index([invalid])
                with self.assertRaisesRegex(loader.SampleError, field):
                    loader.list_sample_threads(self.index)

        self.write_index([self.sample])
        invalid_results = (
            ({**self.result, "session_id": "different"}, "genuine session"),
            ({**self.result, "duration_ms": 0}, "duration_ms"),
            ({**self.result, "duration_api_ms": 8_000}, "duration_api_ms"),
            ({**self.result, "total_cost_usd": 0}, "total_cost_usd"),
            ({**self.result, "usage": {}}, "token usage"),
            ({**self.result, "modelUsage": {}}, "recorded model"),
            (
                {
                    **self.result,
                    "modelUsage": {
                        "claude-sonnet-5": {
                            "inputTokens": 0,
                            "outputTokens": 0,
                            "cacheReadInputTokens": 0,
                            "cacheCreationInputTokens": 0,
                            "costUSD": 0.027,
                        }
                    },
                },
                "model token usage",
            ),
        )
        for invalid, message in invalid_results:
            with self.subTest(invalid_result=message):
                self.write_result(invalid)
                with self.assertRaisesRegex(loader.SampleError, message):
                    loader.list_sample_threads(self.index)

    def test_materialization_retains_real_upstream_commit_transcript_and_recorded_metrics(
        self,
    ) -> None:
        controller = self.root / "controller"
        with mock.patch.object(loader, "_repository_url", return_value=str(self.upstream)):
            materialized = loader.materialize_sample(
                self.sample["id"], controller, index_path=self.index
            )

        repository = Path(materialized["repository_path"])
        self.assertEqual(materialized["baseline_commit"], self.baseline)
        self.assertEqual(self.git("-C", str(repository), "rev-parse", "HEAD^"), self.baseline)
        self.assertEqual((repository / "program.txt").read_text(encoding="utf-8"), "after\n")
        self.assertEqual(
            self.git("-C", str(repository), "diff", self.baseline, materialized["ending_commit"]),
            self.patch.strip(),
        )
        events = [
            json.loads(line)
            for line in Path(materialized["transcript_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            [event["timestamp"] for event in events],
            [
                "2026-08-11T10:00:00Z",
                "2026-08-11T10:00:12Z",
            ],
        )
        self.assertEqual(events[0]["cwd"], str(repository))
        self.assertEqual(events[1]["message"]["usage"], {"input_tokens": 14, "output_tokens": 32})

        ledger = json.loads(loader.ledger_path(controller).read_text(encoding="utf-8"))
        (record,) = ledger["records"]
        self.assertEqual(record["imported_thread_id"], "claude-sample:jq-2919--claude-sonnet-5")
        self.assertEqual(record["recorded_claude_result"]["duration_ms"], 12_300)
        self.assertEqual(record["recorded_claude_result"]["duration_api_ms"], 9_100)
        self.assertEqual(record["recorded_claude_result"]["total_cost_usd"], 0.027)
        self.assertIn("modelUsage", record["recorded_claude_result"])

        replay_result = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "scripts" / "historical_bakeoff.py"),
                "replay",
                "--imported-thread-id",
                record["imported_thread_id"],
                "--ledger",
                str(loader.ledger_path(controller)),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        replay = json.loads(replay_result.stdout)["replay"]
        self.assertEqual(replay["claude_model"], "claude-sonnet-5")
        self.assertEqual(replay["historical_model_request_seconds"], 9.1)
        self.assertEqual(replay["historical_wall_clock_seconds"], 12.3)
        self.assertEqual(replay["historical_usage"]["input_tokens"], 14)
        self.assertEqual(replay["historical_usage"]["output_tokens"], 32)
        self.assertEqual(replay["recorded_claude_result"]["total_cost_usd"], 0.027)
        self.assertEqual(replay["historical_changed_files"], [str(repository / "program.txt")])

        baseline_result = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "scripts" / "historical_bakeoff.py"),
                "baseline",
                "--imported-thread-id",
                record["imported_thread_id"],
                "--repo",
                str(repository),
                "--beginning-kind",
                "git",
                "--ending-kind",
                "git",
                "--baseline-commit",
                materialized["baseline_commit"],
                "--ending-commit",
                materialized["ending_commit"],
                "--ledger",
                str(loader.ledger_path(controller)),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        baseline = json.loads(baseline_result.stdout)
        self.assertEqual(baseline["repository_blocking_reasons"], [])
        self.assertEqual(baseline["baseline"]["commit"], materialized["baseline_commit"])
        self.assertEqual(baseline["baseline"]["ending_commit"], materialized["ending_commit"])

        with mock.patch.object(loader, "_run_git") as git:
            repeated = loader.materialize_sample(
                self.sample["id"], controller, index_path=self.index
            )
        git.assert_not_called()
        self.assertEqual(repeated, materialized)

    def test_empty_recorded_patch_remains_an_honest_empty_child_commit(self) -> None:
        (self.records / "patch.diff").write_text("", encoding="utf-8")
        with mock.patch.object(loader, "_repository_url", return_value=str(self.upstream)):
            materialized = loader.materialize_sample(
                self.sample["id"],
                self.root / "empty-controller",
                index_path=self.index,
            )

        repository = materialized["repository_path"]
        self.assertEqual(self.git("-C", repository, "rev-parse", "HEAD^"), self.baseline)
        self.assertEqual(
            self.git("-C", repository, "diff", self.baseline, materialized["ending_commit"]),
            "",
        )

    def test_unsafe_identifiers_and_asset_paths_are_rejected(self) -> None:
        for sample_id in ("../escape", "nested/name", "", "."):
            with self.subTest(sample_id=sample_id):
                with self.assertRaises(loader.SampleError):
                    loader.materialize_sample(sample_id, self.root, index_path=self.index)

        unsafe = {**self.sample, "patch_path": "../outside.diff"}
        self.write_index([unsafe])
        with self.assertRaisesRegex(loader.SampleError, "escapes its asset directory"):
            loader.load_samples(self.index)

    def test_unknown_sample_and_duplicate_catalog_entries_are_rejected(self) -> None:
        with self.assertRaisesRegex(loader.SampleError, "unavailable"):
            loader.materialize_sample("missing", self.root, index_path=self.index)

        self.write_index([self.sample, dict(self.sample)])
        with self.assertRaisesRegex(loader.SampleError, "repeats an identifier"):
            loader.load_samples(self.index)


class RecordedClaudeSampleControllerTests(unittest.TestCase):
    def test_real_imported_threads_are_preferred_over_recorded_samples(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        samples = [
            {
                "imported_thread_id": "claude-sample:jq-2157--claude-fable-5",
                "title": "Sample: Fix URI escaping",
                "claude_model": "claude-fable-5",
            },
            {
                "imported_thread_id": "claude-sample:jq-2919--claude-sonnet-5",
                "title": "Sample: Allow scripts after --",
                "claude_model": "claude-sonnet-5",
            },
        ]
        imported = {"imported_thread_id": "thread-1", "title": "An imported Claude thread"}
        with (
            mock.patch.object(sample_loader, "list_sample_threads", return_value=samples),
            mock.patch.object(
                server,
                "_engine",
                return_value={"sessions": [imported], "total": 4, "has_more": True},
            ) as engine,
        ):
            result = server._call_tool(
                {"name": "list_threads", "arguments": {"offset": 1, "limit": 2}}
            )

        engine.assert_called_once_with("sessions", ["--limit", "2", "--offset", "1"])
        payload = result["structuredContent"]
        self.assertEqual(payload["threads"], [imported])
        self.assertEqual(payload["source"], "imported")
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["imported_total"], 4)
        self.assertEqual(payload["sample_total"], 2)
        self.assertTrue(payload["has_more"])

    def test_recorded_samples_are_default_when_no_real_threads_exist(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        sample = {"imported_thread_id": "claude-sample:jq-2919--claude-opus-5"}
        with (
            mock.patch.object(sample_loader, "list_sample_threads", return_value=[sample]),
            mock.patch.object(server, "_engine", return_value={"sessions": [], "total": 0}),
        ):
            payload = server._thread_payload({"offset": 0, "limit": 20})

        self.assertEqual(payload["source"], "sample")
        self.assertEqual(payload["threads"], [sample])
        self.assertEqual(payload["imported_total"], 0)
        self.assertEqual(payload["sample_total"], 1)

    def test_recorded_samples_can_be_selected_separately_from_real_threads(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        samples = [
            {"imported_thread_id": "claude-sample:first"},
            {"imported_thread_id": "claude-sample:second"},
        ]
        with (
            mock.patch.object(sample_loader, "list_sample_threads", return_value=samples),
            mock.patch.object(
                server,
                "_engine",
                return_value={"sessions": [{"imported_thread_id": "real-thread"}], "total": 7},
            ),
        ):
            payload = server._thread_payload({"source": "sample", "offset": 1, "limit": 1})

        self.assertEqual(payload["source"], "sample")
        self.assertEqual(payload["threads"], [samples[1]])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["imported_total"], 7)

    def test_recorded_claude_sample_search_stays_within_selected_source(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        sample = {
            "imported_thread_id": "claude-sample:jq-2919--claude-opus-5",
            "title": "Sample: Allow scripts after --",
            "project_dir": "jqlang/jq",
            "claude_model": "claude-opus-5",
        }
        with (
            mock.patch.object(sample_loader, "list_sample_threads", return_value=[sample]),
            mock.patch.object(
                server,
                "_engine",
                return_value={"sessions": [{"title": "Different task"}], "total": 1},
            ),
        ):
            payload = server._thread_payload(
                {"source": "sample", "query": "opus", "offset": 0, "limit": 20}
            )

        self.assertEqual(payload["threads"], [sample])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["source"], "sample")

    def test_unknown_thread_source_is_rejected(self) -> None:
        server = load_server()
        for source in ("all", [], 7):
            with self.subTest(source=source):
                with self.assertRaisesRegex(server.ControllerError, "source must be"):
                    server._thread_payload({"source": source})

    def test_thread_picker_separates_imported_and_sample_data(self) -> None:
        controller = (PLUGIN_ROOT / "mcp" / "controller.html").read_text(encoding="utf-8")
        thread_picker = controller.split("function renderThreadStep", 1)[1].split(
            "function capabilityRows", 1
        )[0]

        self.assertIn('"Imported threads"', thread_picker)
        self.assertIn('"Sample data"', thread_picker)
        self.assertIn('data-action="thread-source"', thread_picker)
        self.assertIn("state.threadSource === source", thread_picker)
        self.assertIn("source: state.threadSource", controller)

    def test_recorded_sample_engine_commands_use_only_the_controller_private_ledger(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        with tempfile.TemporaryDirectory() as temporary:
            controller = Path(temporary)
            private_ledger = sample_loader.ledger_path(controller)
            private_ledger.parent.mkdir(parents=True)
            private_ledger.write_text('{"records": []}', encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"status": "ok"}',
                stderr="",
            )
            with (
                mock.patch.object(server, "CONTROLLER_INSTANCE_ROOT", controller),
                mock.patch.object(server, "_run_process", return_value=completed) as process,
            ):
                server._engine(
                    "replay",
                    ["--imported-thread-id", "claude-sample:jq-2919--claude-opus-5"],
                )

            command = process.call_args.args[0]
            self.assertIn("--ledger", command)
            self.assertEqual(command[command.index("--ledger") + 1], str(private_ledger))

    def test_recorded_sample_inspection_materializes_exact_upstream_baseline_on_demand(
        self,
    ) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        baseline = "a" * 40
        ending = "b" * 40
        original_prompt = "Fix argument parsing.\r\n"
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            materialized = {
                "repository_path": str(repository),
                "baseline_commit": baseline,
                "ending_commit": ending,
            }

            def fake_engine(command: str, arguments=()):
                if command == "replay":
                    return {
                        "replay": {
                            "imported_thread_id": "claude-sample:jq-2919--claude-opus-5",
                            "claude_model": "claude-opus-5",
                            "request": original_prompt,
                            "prompt_reconstruction_turns": [
                                {"role": "user", "text": original_prompt}
                            ],
                            "project_dir": str(repository),
                        }
                    }
                if command == "baseline":
                    return {
                        "baseline": {
                            "commit": baseline,
                            "ending_commit": ending,
                            "beginning_kind": "git",
                            "ending_kind": "git",
                        }
                    }
                if command == "models":
                    return {"options": [{"id": "gpt-test"}]}
                if command == "capabilities":
                    return {"items": []}
                raise AssertionError(command)

            with (
                mock.patch.object(
                    sample_loader,
                    "materialize_sample",
                    return_value=materialized,
                ) as materialize,
                mock.patch.object(server, "_engine", side_effect=fake_engine) as engine,
            ):
                inspected = server._inspect_thread(
                    {"thread_id": "claude-sample:jq-2919--claude-opus-5"}
                )

            materialize.assert_called_once_with(
                "jq-2919--claude-opus-5",
                server.CONTROLLER_INSTANCE_ROOT,
            )
            baseline_call = next(
                call for call in engine.call_args_list if call.args[0] == "baseline"
            )
            self.assertEqual(
                baseline_call.args[1],
                [
                    "--imported-thread-id",
                    "claude-sample:jq-2919--claude-opus-5",
                    "--repo",
                    str(repository),
                    "--beginning-kind",
                    "git",
                    "--ending-kind",
                    "git",
                    "--baseline-commit",
                    baseline,
                    "--ending-commit",
                    ending,
                ],
            )

        self.assertEqual(inspected["replay"]["claude_model"], "claude-opus-5")
        self.assertEqual(inspected["replay"]["request"], original_prompt)
        self.assertEqual(inspected["baseline"]["commit"], baseline)
        self.assertEqual(inspected["baseline"]["ending_commit"], ending)

    def test_recorded_sample_rejects_traversal_before_materializing(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        with mock.patch.object(sample_loader, "materialize_sample") as materialize:
            with self.assertRaisesRegex(server.ControllerError, "identifier is invalid"):
                server._inspect_thread({"thread_id": "claude-sample:../escape"})
        materialize.assert_not_called()

    def test_packaged_text_files_remain_below_monorepo_size_limit(self) -> None:
        suffixes = frozenset({".py", ".json", ".jsonl", ".html", ".md", ".mjs", ".diff"})
        for path in PLUGIN_ROOT.rglob("*"):
            if path.is_file() and path.suffix in suffixes:
                with self.subTest(path=path.relative_to(PLUGIN_ROOT)):
                    self.assertLessEqual(
                        path.stat().st_size,
                        150_000,
                        "Packaged text files must satisfy the monorepo file-size limit.",
                    )


if __name__ == "__main__":
    unittest.main()
