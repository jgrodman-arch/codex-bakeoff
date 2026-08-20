from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "scripts" / "record_claude_code_samples.py"
MODULE_NAME = "codex_bakeoff_record_claude_code_samples_test"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
recorder = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = recorder
SPEC.loader.exec_module(recorder)


class ClaudeCodeSampleRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        tasks, models = recorder.load_manifest()
        self.task = tasks[1]
        self.model = next(model for model in models if model.id == "claude-opus-5")
        self.session_id = "a32802b5-b70c-4923-aa58-05ef24e36731"

    def genuine_result(self) -> dict:
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": self.session_id,
            "duration_ms": 14_250,
            "duration_api_ms": 9_750,
            "total_cost_usd": 0.085,
            "usage": {
                "input_tokens": 270,
                "output_tokens": 81,
                "cache_read_input_tokens": 1024,
                "cache_creation_input_tokens": 256,
            },
            "modelUsage": {
                self.model.id: {
                    "inputTokens": 270,
                    "outputTokens": 81,
                    "cacheReadInputTokens": 1024,
                    "cacheCreationInputTokens": 256,
                    "costUSD": 0.085,
                }
            },
            "num_turns": 3,
            "result": "Sensitive final response that must not enter bundled samples.",
            "permission_denials": [
                {"tool_name": "Bash", "tool_input": {"command": "private command"}}
            ],
        }

    def seeded_repository(self) -> tuple[Path, str]:
        repository = self.root / "seed-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
        (repository / "main.c").write_text("int value = 1;\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "main.c"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Replay Test",
                "-c",
                "user.email=replay-test@example.invalid",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "-m",
                "baseline",
            ],
            check=True,
        )
        commit = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
        ).strip()
        return repository, commit

    def create_recording(self) -> tuple[Path, Path, object]:
        baseline, commit = self.seeded_repository()
        task = replace(self.task, baseline_commit=commit)
        project_root = self.root / "claude-projects"
        projects = project_root / "-private-tmp-replay-samples"
        projects.mkdir(parents=True)
        verification = {
            "task_id": task.id,
            "passed": True,
            "repository": "",
            "build": {"command": ["/opt/homebrew/bin/cc"], "stderr": "private compiler path"},
            "checks": [
                {
                    "name": "script after separator",
                    "passed": True,
                    "returncode": 0,
                    "stdout": "private compiler output",
                }
            ],
        }

        def invoke(command: list[str], worktree: Path, recording: Path) -> None:
            self.assertEqual(command[command.index("-p") + 1], task.prompt)
            recording.mkdir(parents=True)
            (worktree / "main.c").write_text("int value = 2;\n", encoding="utf-8")
            (worktree / "new-file.c").write_text("int added = 3;\n", encoding="utf-8")
            events = [
                {
                    "type": "user",
                    "sessionId": self.session_id,
                    "uuid": "user-message",
                    "timestamp": "2026-08-11T10:00:00.000Z",
                    "cwd": str(worktree),
                    "message": {"role": "user", "content": task.prompt},
                },
                {
                    "type": "assistant",
                    "sessionId": self.session_id,
                    "uuid": "assistant-message",
                    "parentUuid": "user-message",
                    "requestId": "request_123",
                    "timestamp": "2026-08-11T10:00:09.750Z",
                    "cwd": str(worktree),
                    "message": {
                        "role": "assistant",
                        "id": "msg_123",
                        "model": self.model.id,
                        "usage": self.genuine_result()["usage"],
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "PRIVATE_HIDDEN_REASONING",
                                "signature": "PRIVATE_SIGNATURE",
                            },
                            {
                                "type": "redacted_thinking",
                                "data": "PRIVATE_REDACTED_REASONING",
                            },
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "id": "edit_123",
                                "input": {
                                    "file_path": str(worktree / "main.c"),
                                    "note": "sk-ant-abcdefghijklmnopqrstuvwxyz123456",
                                    "plugin": str(PLUGIN_ROOT / "scripts" / "verify.py"),
                                    "brew": "/opt/homebrew/bin/jq",
                                },
                            },
                        ],
                    },
                },
            ]
            native = projects / f"{self.session_id}.jsonl"
            native.write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )
            stream = [
                {"type": "system", "subtype": "init", "session_id": self.session_id},
                self.genuine_result(),
            ]
            (recording / "stream.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in stream), encoding="utf-8"
            )

        with (
            mock.patch.object(recorder, "_ensure_baseline", return_value=baseline),
            mock.patch.object(recorder, "CLAUDE_PROJECTS_ROOT", project_root),
            mock.patch.object(recorder.uuid, "uuid4", return_value=uuid.UUID(self.session_id)),
            mock.patch.object(recorder, "_invoke_claude", side_effect=invoke),
            mock.patch.object(recorder, "_verify", return_value=verification),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            recording = recorder.record_sample(task, self.model, self.root, Decimal("1.50"))
        return recording, baseline, task

    def test_manifest_loads_two_exact_tasks_and_four_pinned_models(self) -> None:
        tasks, models = recorder.load_manifest()

        self.assertEqual([task.id for task in tasks], ["jqlang__jq-2157", "jqlang__jq-2919"])
        self.assertEqual(
            [model.id for model in models],
            [
                "claude-fable-5",
                "claude-opus-5",
                "claude-sonnet-5",
                "claude-haiku-4-5-20251001",
            ],
        )
        self.assertTrue(tasks[1].prompt.endswith("\r\n"))

    def test_dry_run_requires_no_budget_or_filesystem_changes(self) -> None:
        unused = self.root / "unused-output"
        output = io.StringIO()
        with (
            mock.patch.object(recorder, "record_sample") as record,
            contextlib.redirect_stdout(output),
        ):
            result = recorder.main(["--dry-run", "--output-root", str(unused)])

        self.assertEqual(result, 0)
        matrix = json.loads(output.getvalue())
        self.assertEqual(len(matrix["samples"]), 8)
        self.assertIsNone(matrix["max_budget_usd"])
        self.assertEqual(matrix["jobs"], 8)
        self.assertFalse(unused.exists())
        record.assert_not_called()

    def test_live_recording_runs_all_sessions_concurrently_and_packs_in_matrix_order(self) -> None:
        tasks, models = recorder.load_manifest()
        sample_ids = [f"{task.id}--{model.id}" for task in tasks for model in models]
        started = threading.Barrier(len(sample_ids))
        completed = [threading.Event() for _ in sample_ids]
        completion_order: list[int] = []

        def record(
            task: object,
            model: object,
            output_root: Path,
            budget: Decimal,
            *,
            preparation_lock: object,
        ) -> Path:
            sample_id = f"{task.id}--{model.id}"
            index = sample_ids.index(sample_id)
            self.assertEqual(budget, Decimal("1.50"))
            self.assertIsNotNone(preparation_lock)
            started.wait(timeout=10)
            if index + 1 < len(sample_ids):
                self.assertTrue(completed[index + 1].wait(timeout=10))
            completion_order.append(index)
            completed[index].set()
            return output_root / "recordings" / sample_id

        with (
            mock.patch.object(recorder, "record_sample", side_effect=record),
            mock.patch.object(
                recorder, "pack_recordings", return_value=self.root / "index.json"
            ) as pack,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = recorder.main(
                ["--max-budget-usd", "1.50", "--pack", "--output-root", str(self.root)]
            )

        self.assertEqual(result, 0)
        self.assertEqual(completion_order, list(reversed(range(len(sample_ids)))))
        self.assertEqual([path.name for path in pack.call_args.args[0]], sample_ids)

    def test_jobs_bounds_number_of_concurrent_sessions(self) -> None:
        barrier = threading.Barrier(2)
        active = 0
        maximum_active = 0
        count_lock = threading.Lock()

        def record(
            task: object,
            model: object,
            output_root: Path,
            budget: Decimal,
            *,
            preparation_lock: object,
        ) -> Path:
            nonlocal active, maximum_active
            with count_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                barrier.wait(timeout=10)
            finally:
                with count_lock:
                    active -= 1
            return output_root / f"{task.id}--{model.id}"

        with (
            mock.patch.object(recorder, "record_sample", side_effect=record),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = recorder.main(
                ["--max-budget-usd", "1", "--jobs", "2", "--output-root", str(self.root)]
            )

        self.assertEqual(result, 0)
        self.assertEqual(maximum_active, 2)

    def test_invalid_concurrency_is_rejected_before_recording(self) -> None:
        for jobs in ("0", "-1", "9", "many"):
            with (
                self.subTest(jobs=jobs),
                mock.patch.object(recorder, "record_sample") as record,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    recorder.main(["--dry-run", "--jobs", jobs])

                self.assertEqual(raised.exception.code, 2)
                record.assert_not_called()

    def test_incomplete_recording_stops_before_any_parallel_session_starts(self) -> None:
        tasks, models = recorder.load_manifest()
        incomplete = self.root / "recordings" / f"{tasks[0].id}--{models[0].id}"
        incomplete.mkdir(parents=True)
        stream = incomplete / "stream.jsonl"
        stream.write_text("authentic in-flight events\n", encoding="utf-8")
        errors = io.StringIO()

        with (
            mock.patch.object(recorder, "record_sample") as record,
            contextlib.redirect_stderr(errors),
        ):
            result = recorder.main(["--max-budget-usd", "1", "--output-root", str(self.root)])

        self.assertEqual(result, 1)
        self.assertIn("may still be running", errors.getvalue())
        self.assertEqual(stream.read_text(encoding="utf-8"), "authentic in-flight events\n")
        record.assert_not_called()

    def test_parallel_failures_finish_other_sessions_and_never_package(self) -> None:
        tasks, models = recorder.load_manifest()
        failed_ids = {
            f"{tasks[0].id}--{models[0].id}",
            f"{tasks[1].id}--{models[-1].id}",
        }

        def record(
            task: object,
            model: object,
            output_root: Path,
            budget: Decimal,
            *,
            preparation_lock: object,
        ) -> Path:
            sample_id = f"{task.id}--{model.id}"
            if sample_id in failed_ids:
                raise recorder.RecordingError("genuine run failed")
            return output_root / sample_id

        errors = io.StringIO()
        with (
            mock.patch.object(recorder, "record_sample", side_effect=record) as capture,
            mock.patch.object(recorder, "pack_recordings") as pack,
            contextlib.redirect_stderr(errors),
        ):
            result = recorder.main(
                ["--max-budget-usd", "1", "--pack", "--output-root", str(self.root)]
            )

        self.assertEqual(result, 1)
        self.assertEqual(capture.call_count, 8)
        self.assertIn("2 Claude Code recording(s) failed", errors.getvalue())
        for sample_id in failed_ids:
            self.assertIn(sample_id, errors.getvalue())
        pack.assert_not_called()

    def test_shared_repository_setup_is_serial_but_claude_sessions_are_parallel(self) -> None:
        tasks, models = recorder.load_manifest()
        matrix = [(tasks[0], models[0]), (tasks[0], models[1])]
        preparation_guard = threading.Lock()
        claude_started = threading.Barrier(len(matrix))

        def prepare(task: object, output_root: Path) -> Path:
            if not preparation_guard.acquire(blocking=False):
                raise recorder.RecordingError("shared baseline preparation overlapped")
            return self.root / "baseline"

        def create(repository: Path, output_root: Path, sample_id: str, session_id: str) -> Path:
            preparation_guard.release()
            return self.root / "worktrees" / session_id

        def invoke(command: object, worktree: Path, recording: Path) -> None:
            claude_started.wait(timeout=10)
            raise recorder.RecordingError("test stopped before a real Claude call")

        with (
            mock.patch.object(recorder, "_ensure_baseline", side_effect=prepare),
            mock.patch.object(recorder, "_create_worktree", side_effect=create),
            mock.patch.object(recorder, "_invoke_claude", side_effect=invoke),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(recorder.RecordingError, "2 Claude Code recording"):
                recorder._record_samples(matrix, self.root, Decimal("1"), 2)

        self.assertFalse(preparation_guard.locked())

    def test_recovery_restores_completed_run_with_helper_usage_without_invoking_claude(
        self,
    ) -> None:
        recording, _, task = self.create_recording()
        verification = json.loads((recording / "verification.json").read_text(encoding="utf-8"))
        result = self.genuine_result()
        result["modelUsage"]["claude-haiku-4-5-20251001"] = {
            "inputTokens": 24,
            "outputTokens": 8,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.000856,
        }
        result["total_cost_usd"] = 0.085856
        (recording / "stream.jsonl").write_text(
            json.dumps({"type": "system", "session_id": self.session_id})
            + "\n"
            + json.dumps(result)
            + "\n",
            encoding="utf-8",
        )
        for name in (
            "metadata.json",
            "transcript.jsonl",
            "result.json",
            "patch.diff",
            "verification.json",
        ):
            (recording / name).unlink()

        with (
            mock.patch.object(recorder, "CLAUDE_PROJECTS_ROOT", self.root / "claude-projects"),
            mock.patch.object(recorder, "_invoke_claude") as invoke,
            mock.patch.object(recorder, "_verify", return_value=verification),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            recovered = recorder.recover_sample(task, self.model, self.root)

        self.assertEqual(recovered, recording)
        metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
        preserved = json.loads((recording / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["total_cost_usd"], 0.085856)
        self.assertEqual(metadata["duration_ms"], 14_250)
        self.assertTrue(metadata["verification"]["passed"])
        self.assertEqual(
            set(preserved["modelUsage"]),
            {self.model.id, "claude-haiku-4-5-20251001"},
        )
        self.assertIn("+int value = 2;", (recording / "patch.diff").read_text(encoding="utf-8"))
        invoke.assert_not_called()

    def test_recovery_rejects_worktree_at_unrelated_baseline_without_invoking_claude(self) -> None:
        recording, _, task = self.create_recording()
        (recording / "metadata.json").unlink()
        unrelated = replace(task, baseline_commit="0" * 40)

        with (
            mock.patch.object(recorder, "CLAUDE_PROJECTS_ROOT", self.root / "claude-projects"),
            mock.patch.object(recorder, "_invoke_claude") as invoke,
        ):
            with self.assertRaisesRegex(recorder.RecordingError, "not immutable baseline"):
                recorder.recover_sample(unrelated, self.model, self.root)

        self.assertFalse((recording / "metadata.json").exists())
        invoke.assert_not_called()

    def test_recover_only_never_requires_budget_or_invokes_claude(self) -> None:
        recovered = self.root / "existing-recording"
        with (
            mock.patch.object(recorder, "recover_sample", return_value=recovered) as recover,
            mock.patch.object(recorder, "_invoke_claude") as invoke,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = recorder.main(
                [
                    "--recover-only",
                    "--task",
                    self.task.id,
                    "--model",
                    self.model.id,
                    "--output-root",
                    str(self.root),
                ]
            )

        self.assertEqual(result, 0)
        recover.assert_called_once_with(self.task, self.model, self.root.resolve())
        invoke.assert_not_called()

    def test_recover_only_rejects_budget_before_invoking_claude(self) -> None:
        with (
            mock.patch.object(recorder, "recover_sample") as recover,
            mock.patch.object(recorder, "_invoke_claude") as invoke,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                recorder.main(["--recover-only", "--max-budget-usd", "1"])

        self.assertEqual(raised.exception.code, 2)
        recover.assert_not_called()
        invoke.assert_not_called()

    def test_live_recording_requires_explicit_positive_budget(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as missing:
                recorder.main([])
            with self.assertRaises(SystemExit):
                recorder.main(["--max-budget-usd", "0"])
            with self.assertRaises(SystemExit):
                recorder.main(["--max-budget-usd", "nan"])

        self.assertEqual(missing.exception.code, 2)

    def test_claude_command_preserves_prompt_and_narrows_authority(self) -> None:
        command = recorder._claude_command(self.task, self.model, self.session_id, Decimal("2.25"))

        self.assertEqual(command[command.index("-p") + 1], self.task.prompt)
        self.assertEqual(command[command.index("--model") + 1], "claude-opus-5")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.25")
        for required in ("--safe-mode", "--no-chrome", "--verbose", "--session-id"):
            with self.subTest(flag=required):
                self.assertIn(required, command)
        self.assertIn("acceptEdits", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--bare", command)
        self.assertNotIn("--no-session-persistence", command)
        self.assertNotIn("Bash(python3*)", command)

    def test_git_baseline_fetches_exact_commit_without_configured_remote(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_git(repository: Path, *arguments: str) -> str:
            calls.append(arguments)
            if arguments[:2] == ("rev-parse", "HEAD"):
                return self.task.baseline_commit + "\n"
            return ""

        with mock.patch.object(recorder, "_git", side_effect=fake_git):
            recorder._ensure_baseline(self.task, self.root)

        self.assertIn(
            (
                "fetch",
                "--quiet",
                "--no-tags",
                "https://github.com/jqlang/jq.git",
                self.task.baseline_commit,
            ),
            calls,
        )
        self.assertFalse(any(call and call[0] in {"remote", "push"} for call in calls))

    def test_git_baseline_unshallows_existing_repository(self) -> None:
        repository = self.root / "repositories" / self.task.id
        (repository / ".git").mkdir(parents=True)
        calls: list[tuple[str, ...]] = []

        def fake_git(repository: Path, *arguments: str) -> str:
            calls.append(arguments)
            if arguments == ("rev-parse", "--is-shallow-repository"):
                return "true\n"
            if arguments == ("rev-parse", "HEAD"):
                return self.task.baseline_commit + "\n"
            return ""

        with mock.patch.object(recorder, "_git", side_effect=fake_git):
            recorder._ensure_baseline(self.task, self.root)

        self.assertIn(
            (
                "fetch",
                "--quiet",
                "--unshallow",
                "--no-tags",
                "https://github.com/jqlang/jq.git",
                self.task.baseline_commit,
            ),
            calls,
        )

    def test_records_real_worktree_patch_transcript_result_and_verification(self) -> None:
        recording, baseline, task = self.create_recording()

        metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
        patch = (recording / "patch.diff").read_text(encoding="utf-8")
        self.assertEqual(metadata["prompt"], task.prompt)
        self.assertEqual(metadata["model"], self.model.id)
        self.assertEqual(metadata["session_id"], self.session_id)
        self.assertEqual(metadata["duration_ms"], 14_250)
        self.assertEqual(metadata["total_cost_usd"], 0.085)
        self.assertTrue(metadata["verification"]["passed"])
        self.assertIn("-int value = 1;", patch)
        self.assertIn("+int value = 2;", patch)
        self.assertIn("new-file.c", patch)
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(baseline), "remote"], text=True), ""
        )

    def test_pack_removes_hidden_thinking_paths_secrets_and_private_result_text(self) -> None:
        recording, _, task = self.create_recording()
        package = self.root / "packaged"

        index_path = recorder.pack_recordings([recording], package)

        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["schema_version"], 1)
        sample = index["samples"][0]
        self.assertEqual(sample["prompt"], task.prompt)
        self.assertEqual(
            sample["verification"],
            {
                "passed": True,
                "checks": [{"name": "script after separator", "passed": True, "returncode": 0}],
            },
        )
        transcript = "".join(
            (package / relative).read_text(encoding="utf-8")
            for relative in sample["transcript_parts"]
        )
        for private in (
            "PRIVATE_HIDDEN_REASONING",
            "PRIVATE_REDACTED_REASONING",
            "PRIVATE_SIGNATURE",
            "sk-ant-abcdefghijklmnopqrstuvwxyz123456",
            str(PLUGIN_ROOT),
            "/opt/homebrew",
            "private compiler output",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, transcript)
                self.assertNotIn(private, index_path.read_text(encoding="utf-8"))
        self.assertIn(recorder.REPOSITORY_PATH_MARKER, transcript)
        events = [json.loads(line) for line in transcript.splitlines()]
        self.assertEqual(events[0]["message"]["content"], task.prompt)
        self.assertEqual(events[1]["message"]["model"], self.model.id)
        self.assertEqual(events[1]["message"]["usage"]["output_tokens"], 81)
        self.assertFalse(
            any(
                block["type"] in {"thinking", "redacted_thinking"}
                for block in events[1]["message"]["content"]
            )
        )
        result = json.loads((package / sample["result_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("result", result)
        self.assertNotIn("permission_denials", result)
        self.assertEqual(result["modelUsage"][self.model.id]["costUSD"], 0.085)
        self.assertTrue(
            all(path.stat().st_size < 150_000 for path in package.rglob("*") if path.is_file())
        )

    def test_reusing_recording_rejects_changed_exact_prompt(self) -> None:
        recording, _, task = self.create_recording()
        changed = replace(task, prompt=task.prompt.rstrip())

        with self.assertRaisesRegex(recorder.RecordingError, "mismatched prompt"):
            recorder.record_sample(changed, self.model, self.root, Decimal("1"))

        self.assertTrue((recording / "metadata.json").is_file())

    def test_pack_rejects_tampered_transcript_or_missing_cost(self) -> None:
        recording, _, _ = self.create_recording()
        transcript = recording / "transcript.jsonl"
        transcript.write_text(
            transcript.read_text(encoding="utf-8").replace(self.model.id, "claude-sonnet-5"),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(recorder.RecordingError, "observed claude-sonnet-5"):
            recorder.pack_recordings([recording], self.root / "package")

    def test_result_rejects_missing_metrics_usage_fallback_and_false_zero(self) -> None:
        original = self.genuine_result()
        cases = [
            ("duration_ms", 0, "duration_ms"),
            ("duration_api_ms", None, "duration_api_ms"),
            ("total_cost_usd", 0, "total_cost_usd"),
            ("total_cost_usd", float("nan"), "total_cost_usd"),
            ("usage", {}, "aggregate token usage"),
            ("modelUsage", {}, "requested primary model"),
            ("is_error", True, "is_error=false"),
        ]
        for field, replacement, expected in cases:
            with self.subTest(field=field, replacement=replacement):
                altered = dict(original)
                altered[field] = replacement
                with self.assertRaisesRegex(recorder.RecordingError, expected):
                    recorder._validate_result(altered, self.session_id, self.model.id)
        mixed = json.loads(json.dumps(original))
        mixed["modelUsage"]["claude-haiku-4-5-20251001"] = mixed["modelUsage"][self.model.id]
        recorder._validate_result(mixed, self.session_id, self.model.id)

        missing_primary = json.loads(json.dumps(mixed))
        del missing_primary["modelUsage"][self.model.id]
        with self.assertRaisesRegex(recorder.RecordingError, "requested primary model"):
            recorder._validate_result(missing_primary, self.session_id, self.model.id)

    def test_transcript_chunks_only_at_complete_json_event_boundaries(self) -> None:
        original_limit = recorder.MAX_ARTIFACT_BYTES
        transcript = self.root / "large.jsonl"
        events = [
            {
                "type": "assistant",
                "uuid": f"event-{number}",
                "message": {"role": "assistant", "content": "visible " + "x" * 300},
            }
            for number in range(6)
        ]
        transcript.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        with mock.patch.object(recorder, "MAX_ARTIFACT_BYTES", 800):
            parts = recorder._transcript_parts(transcript, self.root / "parts", str(self.root))

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part.stat().st_size < 800 for part in parts))
        reconstructed = [
            json.loads(line) for part in parts for line in part.read_text().splitlines()
        ]
        self.assertEqual(
            [event["uuid"] for event in reconstructed], [f"event-{i}" for i in range(6)]
        )
        self.assertEqual(recorder.MAX_ARTIFACT_BYTES, original_limit)

    def test_pack_only_refuses_to_create_placeholder_catalog(self) -> None:
        empty = self.root / "no-recordings"
        with contextlib.redirect_stderr(io.StringIO()):
            result = recorder.main(["--pack", "--output-root", str(empty)])

        self.assertEqual(result, 1)
        self.assertFalse(empty.exists())


if __name__ == "__main__":
    unittest.main()
