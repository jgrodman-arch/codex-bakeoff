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

    def precompute(self, entry: dict) -> None:
        sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
        from precompute_sample_configurations import precompute

        precompute(entry, self.assets)

    def write_index(self, samples: list[dict]) -> None:
        for sample in samples:
            if "configuration_path" not in sample:
                self.precompute(sample)
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
        self.sample["prompt"] = exact_prompt
        self.precompute(self.sample)
        self.write_index([self.sample])

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

        with mock.patch.object(
            loader, "_repository_url", side_effect=AssertionError("unexpected fetch")
        ):
            resolved = loader.resolve_sample(
                record["imported_thread_id"], controller, index_path=self.index
            )
            repeated = loader.materialize_sample(
                self.sample["id"], controller, index_path=self.index
            )
        replay = resolved["replay"]
        self.assertEqual(replay["claude_model"], "claude-sonnet-5")
        self.assertEqual(replay["historical_model_request_seconds"], 9.1)
        self.assertEqual(replay["historical_wall_clock_seconds"], 12.3)
        self.assertEqual(replay["historical_usage"]["input_tokens"], 14)
        self.assertEqual(replay["historical_usage"]["output_tokens"], 32)
        self.assertEqual(replay["historical_changed_files"], [str(repository / "program.txt")])
        self.assertEqual(resolved["baseline"]["ending_commit"], materialized["ending_commit"])
        self.assertEqual(resolved["recovery"]["diff"], self.patch)
        self.assertEqual(repeated, materialized)

    def test_recorded_rename_is_packaged_and_materialized(self) -> None:
        self.git("-C", str(self.upstream), "mv", "program.txt", "renamed program.txt")
        patch = self.git("-C", str(self.upstream), "diff", "--binary", "HEAD") + "\n"
        self.assertIn("rename from program.txt", patch)
        (self.records / "patch.diff").write_text(patch, encoding="utf-8")
        self.precompute(self.sample)
        self.write_index([self.sample])

        with mock.patch.object(loader, "_repository_url", return_value=str(self.upstream)):
            resolved = loader.resolve_sample(
                "claude-sample:" + self.sample["id"],
                self.root / "rename-controller",
                index_path=self.index,
            )

        self.assertEqual(resolved["file_selection"]["attributed_files"], ["renamed program.txt"])
        self.assertEqual(resolved["recovery"]["diff"], patch)
        repository = Path(resolved["baseline"]["repository"])
        self.assertFalse((repository / "program.txt").exists())
        self.assertEqual((repository / "renamed program.txt").read_text(), "before\n")

    def test_empty_recorded_patch_remains_an_honest_empty_child_commit(self) -> None:
        (self.records / "patch.diff").write_text("", encoding="utf-8")
        self.precompute(self.sample)
        self.write_index([self.sample])
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

    def test_minimal_prepare_resolves_recorded_ending_without_inspection_or_discovery(self) -> None:
        sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
        import claude_code_sample_loader as engine_loader
        import historical_bakeoff as engine

        server = load_server()
        controller = self.root / "controller"
        thread_id = "claude-sample:" + self.sample["id"]

        def dispatch(command, arguments=(), **kwargs):
            parsed = (
                engine.build_parser().parse_args(
                    [
                        command,
                        *arguments,
                        "--sample-controller-root",
                        str(controller),
                    ]
                )
                if command != "models"
                else engine.build_parser().parse_args([command])
            )
            return engine.dispatch(parsed)

        with (
            mock.patch.object(engine_loader, "SAMPLE_INDEX", self.index),
            mock.patch.object(engine_loader, "_repository_url", return_value=str(self.upstream)),
            mock.patch.object(server, "_claude_code_sample_loader", return_value=engine_loader),
            mock.patch.object(server, "CONTROLLER_INSTANCE_ROOT", controller),
            mock.patch.object(server, "RUN_ROOT", self.root / "runs"),
            mock.patch.object(server, "_engine", side_effect=dispatch),
            mock.patch.object(server, "_run_worker", side_effect=AssertionError("setup LLM call")),
            mock.patch.object(engine, "_selected_model", return_value="gpt-test"),
            mock.patch.object(
                engine, "discover_codex_models", return_value={"options": [{"id": "gpt-test"}]}
            ),
            mock.patch.object(
                engine._discovery(), "build_thread_task", side_effect=AssertionError("rediscovery")
            ),
            mock.patch.object(
                engine._discovery(),
                "recover_historical_solution",
                side_effect=AssertionError("rediscovery"),
            ),
            mock.patch.object(
                engine._discovery(), "inspect_baseline", side_effect=AssertionError("rediscovery")
            ),
            mock.patch.object(
                engine._discovery(), "inspect_capabilities", return_value={"items": []}
            ) as capabilities,
        ):
            prepared = server._prepare_payload({"thread_id": thread_id, "model": "gpt-test"})
            inspected = server._inspect_thread({"thread_id": thread_id})
            self.assertTrue(prepared["ready"])
            self.assertEqual(prepared["baseline"]["commit"], self.baseline)
            self.assertNotEqual(prepared["baseline"]["ending_commit"], self.baseline)
            self.assertEqual(
                prepared["baseline"]["ending_commit"], inspected["baseline"]["ending_commit"]
            )
            self.assertEqual(inspected["replay"]["request_generation"]["method"], "packaged_sample")
            self.assertEqual(
                server._synthesize_request_payload({"thread_id": thread_id})["request"],
                self.sample["prompt"],
            )
            self.assertEqual(
                server._working_directory_payload({"thread_id": thread_id})["source"],
                "packaged_sample",
            )
            capabilities.assert_called()
            self.assertEqual(capabilities.call_args.args[0]["observed_tools"], ["Edit"])
            with self.assertRaisesRegex(server.ControllerError, "cannot be changed"):
                server._resolved_sample({"thread_id": thread_id, "ending_commit": self.baseline})
            context = engine._prepare_context(
                engine.build_parser().parse_args(
                    [
                        "prepare",
                        "--imported-thread-id",
                        thread_id,
                        "--sample-controller-root",
                        str(controller),
                        "--model",
                        "gpt-test",
                    ]
                )
            )
            self.assertEqual(context["historical_candidate"]["candidate"]["diff"], self.patch)

    def test_artifact_and_cached_ending_tampering_are_rejected(self) -> None:
        controller = self.root / "controller"
        with mock.patch.object(loader, "_repository_url", return_value=str(self.upstream)):
            materialized = loader.materialize_sample(
                self.sample["id"], controller, index_path=self.index
            )
        manifest = Path(materialized["repository_path"]).parent / "materialization.json"
        materialized["ending_commit"] = self.baseline
        manifest.write_text(json.dumps(materialized))
        with self.assertRaisesRegex(loader.SampleError, "ending state changed"):
            loader.materialize_sample(self.sample["id"], controller, index_path=self.index)
        (self.records / "patch.diff").write_text(self.patch + "\n")
        with self.assertRaisesRegex(loader.SampleError, "artifact integrity"):
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
        thread_picker = (
            (PLUGIN_ROOT / "mcp" / "controller-ranges.js")
            .read_text(encoding="utf-8")
            .split("function renderThreadStep", 1)[1]
        )

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
            self.assertIn("--sample-controller-root", command)
            self.assertEqual(
                command[command.index("--sample-controller-root") + 1], str(controller)
            )

    def test_recorded_sample_rejects_traversal_before_materializing(self) -> None:
        server = load_server()
        sample_loader = server._claude_code_sample_loader()
        with mock.patch.object(sample_loader, "materialize_sample") as materialize:
            with self.assertRaisesRegex(server.ControllerError, "identifier is invalid"):
                server._inspect_thread({"thread_id": "claude-sample:../escape"})
        materialize.assert_not_called()

    def test_ui_sample_selection_skips_inference_but_imported_selection_still_infers(self) -> None:
        harness = r"""
const source = require("node:fs").readFileSync(process.argv[1], "utf8");
const selectSource = source.slice(source.indexOf("      async function selectThread("), source.indexOf("      async function refreshAttributionAndContinue("));
async function exercise(id) {
  const calls = [];
  const state = {threads: [{id}], selectedModels: [], reviewRevision: 0, promptSynthesisGeneration: 0, workingDirectoryGeneration: 0};
  const select = new Function("state", "callTool", "threadId", "text", "render", "normalizeModels", "setSelectedModels", "initializeClassifications", "reviewDraftFromConfiguration", "synthesizePrompt", "inferWorkingDirectory", "asArray", selectSource + "return selectThread;")(
    state, async name => { calls.push(name); return {replay: {request: "task", request_generation: {method: "pending"}}}; },
    thread => thread.id, value => value || "", () => {}, () => [], () => {}, () => {}, () => ({}),
    () => calls.push("synthesize"), () => calls.push("infer"), value => Array.isArray(value) ? value : []);
  await select(id, 1);
  return {calls, loading: state.workingDirectoryLoading};
}
(async () => process.stdout.write(JSON.stringify([await exercise("claude-sample:test"), await exercise("imported-test") ])))();
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(PLUGIN_ROOT / "mcp" / "controller.html")],
            check=True,
            capture_output=True,
            text=True,
        )
        sample, imported = json.loads(completed.stdout)
        self.assertEqual(sample, {"calls": ["inspect_thread"], "loading": False})
        self.assertEqual(
            imported, {"calls": ["inspect_thread", "synthesize", "infer"], "loading": True}
        )

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
