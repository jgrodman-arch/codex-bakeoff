"""Range identity, overlapping execution, and carried-input runtime contracts."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_mcp_server import load_server


class ControllerRangeTests(unittest.TestCase):
    def test_later_handoff_uses_prior_requests_as_completed_background(self) -> None:
        server = load_server()
        replay = {
            "task_scope": "range",
            "request": "Now extend it.",
            "prior_user_requests": ["Create a seed file."],
            "prompt_reconstruction_turns": [{"role": "user", "text": "Now extend it."}],
        }
        self.assertIsNone(server._single_user_prompt(replay))
        handoff = server._handoff_request(replay)
        self.assertIn("Create a seed file.", handoff)
        self.assertIn("Current chunk:\nNow extend it.", handoff)
        self.assertEqual(
            server._handoff_request({**replay, "task_scope": "whole_thread"}), "Now extend it."
        )
        clarified = server._handoff_request(
            {
                **replay,
                "request": "2",
                "prompt_reconstruction_turns": [
                    {"role": "assistant", "text": "Which format? 1 CSV 2 JSON"},
                    {"role": "user", "text": "2"},
                ],
            }
        )
        self.assertIn("1 CSV 2 JSON", clarified)
        self.assertIn("Current chunk:\n2", clarified)

    def test_boundaries_are_authoritative_and_part_of_approval(self) -> None:
        server = load_server()
        arguments = {
            "thread_id": "thread",
            "model": "model",
            "start_message_uuid": "first",
            "end_message_uuid": "last",
            "source_path": "/tmp/transcript.jsonl",
            "carried_forward_files": ["seed.txt"],
        }
        configuration = server._normalized_configuration(arguments)
        self.assertIsNone(configuration["message_uuid"])
        command = server._configuration_arguments(configuration)
        for flag, value in (
            ("--start-message-uuid", "first"),
            ("--end-message-uuid", "last"),
            ("--carried-forward-file", "seed.txt"),
        ):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("--message-uuid", command)
        changed = server._normalized_configuration({**arguments, "end_message_uuid": "other"})
        self.assertNotEqual(
            server._configuration_fingerprint(configuration),
            server._configuration_fingerprint(changed),
        )
        with self.assertRaisesRegex(server.ControllerError, "starting boundary"):
            server._normalized_configuration({**arguments, "message_uuid": "unrelated"})
        with self.assertRaisesRegex(server.ControllerError, "both"):
            server._normalized_configuration({**arguments, "end_message_uuid": None})

    def test_every_inspection_uses_the_same_range(self) -> None:
        server = load_server()
        arguments = {
            "thread_id": "thread",
            "start_message_uuid": "first",
            "end_message_uuid": "last",
            "carried_forward_files": ["seed.txt"],
            "excluded_files": ["ignored.txt"],
            "repo": "/tmp/project",
            "beginning_kind": "non_git",
            "ending_kind": "git",
            "ending_commit": "abc123",
        }
        calls: dict[str, list[str]] = {}

        def engine(command: str, args: list[str]) -> dict[str, object]:
            calls[command] = list(args)
            return {}

        with (
            mock.patch.object(server, "_resolved_sample", return_value=None),
            mock.patch.object(server, "_engine", side_effect=engine),
        ):
            server._inspect_thread(arguments)
        for command in ("replay", "capabilities", "baseline"):
            self.assertIn("--start-message-uuid", calls[command])
            self.assertIn("last", calls[command])
        self.assertIn("--carried-forward-file", calls["baseline"])
        self.assertIn("--exclude-file", calls["baseline"])
        for flag, value in (
            ("--repo", "/tmp/project"),
            ("--beginning-kind", "non_git"),
            ("--ending-kind", "git"),
            ("--ending-commit", "abc123"),
        ):
            self.assertEqual(calls["baseline"][calls["baseline"].index(flag) + 1], value)
        self.assertEqual(calls["models"], [])

    def test_controller_allows_distinct_overlapping_ranges_in_same_thread(self) -> None:
        server = load_server()
        first = {"thread_id": "thread", "start_message_uuid": "first", "end_message_uuid": "first"}
        second = {**first, "start_message_uuid": "second", "end_message_uuid": "second"}
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(server, "RUN_ROOT", Path(temporary) / "runs"),
            mock.patch.object(server, "_recent_runs") as recent,
        ):
            server._update_attempt(start_requested=True, start_request=first)
            recent.return_value = [{**first, "status": "running"}]
            server._ensure_controller_can_start_replay(second)
            recent.return_value = [{**first, "status": "completed"}]
            server._ensure_controller_can_start_replay(second)
            with self.assertRaisesRegex(server.ControllerError, "already"):
                server._ensure_controller_can_start_replay(first)
            with self.assertRaisesRegex(server.ControllerError, "only one"):
                server._ensure_controller_can_start_replay({**second, "thread_id": "other"})
            with self.assertRaisesRegex(server.ControllerError, "only one"):
                server._ensure_controller_can_start_replay({**second, "source_path": "/other"})
            with self.assertRaisesRegex(server.ControllerError, "only one"):
                server._ensure_controller_can_start_replay({"thread_id": "thread"})
            with mock.patch.dict(server._prepared_runs, {"launching": {"starting": True}}):
                with self.assertRaisesRegex(server.ControllerError, "still starting"):
                    server._ensure_controller_can_start_replay(second)
            server._update_attempt(start_request={"thread_id": "thread"})
            with self.assertRaisesRegex(server.ControllerError, "only one"):
                server._ensure_controller_can_start_replay(second)

    def test_serial_starts_run_distinct_chunks_concurrently_without_duplicate_workers(self) -> None:
        server = load_server()
        barrier = threading.Barrier(3)
        entered = {name: threading.Event() for name in ("first", "second")}
        launched: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve() / "runs"
            run_root.mkdir()

            def engine(command: str, arguments=(), **kwargs):
                if command in {"prepare", "run"}:
                    boundary = arguments[arguments.index("--start-message-uuid") + 1]
                    if command == "prepare":
                        return {
                            "status": "ready_for_approval",
                            "historical_result_sha256": "a" * 64,
                            "prepared_configuration_sha256": "b" * 64,
                        }
                    launched.append(boundary)
                    directory = run_root / f"run-{boundary}"
                    directory.mkdir()
                    return {
                        "run_directory": str(directory),
                        "task_request": {
                            "model": "model",
                            "prompt": boundary,
                            "target": {"type": "projectless"},
                        },
                    }
                directory = Path(kwargs["run_directory"])
                if command == "collect-native-result":
                    return {"native_result_path": str(directory / "native-result.json")}
                if command == "complete-run":
                    return {}
                if command == "report":
                    path = directory / "report.json"
                    path.write_text(json.dumps({"evaluation": {"status": "completed"}}))
                    return {"report_json": str(path)}
                raise AssertionError(command)

            def worker(request, *, run_directory, working_directory, read_only, log_label):
                boundary = request["prompt"]
                self.assertFalse(read_only)
                self.assertEqual(log_label, "implementation")
                self.assertEqual(working_directory, run_directory / "workspaces" / "codex")
                (working_directory / "output.txt").write_text(boundary)
                entered[boundary].set()
                barrier.wait(timeout=10)
                return {"thread_id": f"worker-{boundary}", "worktree": str(working_directory)}

            configs = [
                {
                    "thread_id": "thread",
                    "model": "model",
                    "request": name,
                    "start_message_uuid": name,
                    "end_message_uuid": name,
                }
                for name in entered
            ]
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=engine),
                mock.patch.object(server, "_run_worker", side_effect=worker) as workers,
                mock.patch.object(server, "_review_replay") as reviews,
            ):
                prepared = [server._prepare_payload(config) for config in configs]
                duplicate = server._prepare_payload(configs[0])
                approvals = [
                    {**config, "approved": True, "prepare_token": receipt["prepare_token"]}
                    for config, receipt in zip(configs, prepared)
                ]
                threads: list[threading.Thread] = []
                released = False
                try:
                    first = server._start_run(approvals[0])
                    self.assertTrue(entered["first"].wait(timeout=5))
                    second = server._start_run(approvals[1])
                    self.assertTrue(entered["second"].wait(timeout=5))
                    with server._active_processes_lock:
                        threads = list(server._run_threads.values())
                    self.assertEqual(len(threads), 2)
                    self.assertTrue(all(thread.is_alive() for thread in threads))
                    self.assertEqual(first["run"]["status"], "running")
                    self.assertEqual(second["run"]["status"], "running")
                    retried = server._start_run(approvals[0])
                    self.assertTrue(retried["idempotent"])
                    self.assertEqual(retried["run_id"], first["run_id"])
                    with self.assertRaisesRegex(server.ControllerError, "already been started"):
                        server._start_run(
                            {
                                **configs[0],
                                "approved": True,
                                "prepare_token": duplicate["prepare_token"],
                            }
                        )
                    server._prepared_runs.clear()
                    recovered = server._start_run(approvals[1])
                    self.assertTrue(recovered["idempotent"])
                    self.assertEqual(recovered["run_id"], second["run_id"])
                    self.assertEqual(workers.call_count, 2)
                    self.assertEqual(launched, ["first", "second"])
                    barrier.wait(timeout=5)
                    released = True
                finally:
                    if not released:
                        barrier.abort()
                    with server._active_processes_lock:
                        remaining = list(server._run_threads.values())
                    for thread in set(threads + remaining):
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(reviews.call_count, 2)
                for name in entered:
                    directory = run_root / f"run-{name}"
                    state = server._read_json(server._state_path(directory))
                    self.assertEqual(state["status"], "completed", state.get("error"))
                    self.assertEqual((directory / "workspaces/codex/output.txt").read_text(), name)

    def test_start_enforces_model_capacity_and_releases_terminal_slots(self) -> None:
        server = load_server()
        previous = {
            "thread_id": "thread",
            "start_message_uuid": "first",
            "end_message_uuid": "first",
        }
        config = {
            **previous,
            "start_message_uuid": "later",
            "end_message_uuid": "later",
            "model": "model-a",
            "models": ["model-a", "model-b"],
            "request": "Continue with the next chunk.",
        }
        active = [{**previous, "status": "running"} for _ in range(server.MAX_PARALLEL_RUNS)]
        prepared_configuration = server._normalized_configuration(config)

        def prepare(command, arguments=(), **kwargs):
            self.assertEqual(command, "prepare")
            model = arguments[arguments.index("--model") + 1]
            return {
                "status": "ready_for_approval",
                "configuration": {**prepared_configuration, "model": model},
                "historical_result_sha256": "a" * 64,
                "prepared_configuration_sha256": "b" * 64,
            }

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(server, "RUN_ROOT", Path(temporary) / "runs"),
            mock.patch.object(server, "_recent_runs", return_value=active),
            mock.patch.object(server, "_engine", side_effect=prepare),
            mock.patch.object(server, "_start_prepared_model") as start,
            mock.patch.object(server, "_started_runs_response", return_value={"started": True}),
        ):
            server._update_attempt(start_requested=True, start_request=previous)
            approval = server._prepare_payload(config)
            arguments = {**config, "approved": True, "prepare_token": approval["prepare_token"]}
            for status in ("running", "completed"):
                active[0]["status"] = status
                with self.assertRaisesRegex(server.ControllerError, "up to 8 model variants"):
                    server._start_run(arguments)
                start.assert_not_called()
                self.assertFalse(server._prepared_runs[approval["prepare_token"]]["starting"])
            active[1]["status"] = "failed"
            start.side_effect = [
                {"run_id": "later-a", "model": "model-a"},
                {"run_id": "later-b", "model": "model-b"},
            ]
            self.assertEqual(server._start_run(arguments), {"started": True})
            self.assertEqual(start.call_count, 2)

    def test_capacity_counts_live_workers_beyond_recent_run_window(self) -> None:
        server = load_server()
        previous = {
            "thread_id": "thread",
            "start_message_uuid": "first",
            "end_message_uuid": "first",
        }
        later = {
            **previous,
            "start_message_uuid": "later",
            "end_message_uuid": "later",
            "model": "model",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(server, "RUN_ROOT", Path(temporary) / "runs"),
            mock.patch.object(server, "_recent_runs", return_value=[]),
            mock.patch.dict(
                server._run_threads,
                {str(i): mock.Mock(is_alive=lambda: True) for i in range(server.MAX_PARALLEL_RUNS)},
            ),
        ):
            server._update_attempt(start_requested=True, start_request=previous)
            with self.assertRaisesRegex(server.ControllerError, "up to 8 model variants"):
                server._ensure_controller_can_start_replay(later, launching=True)

    def test_projectless_workspace_receives_carried_inputs_before_worker(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("alpha\n", encoding="utf-8")
            run = root / "run"
            run.mkdir()
            (run / "run.json").write_text(
                json.dumps(
                    {
                        "replay": {"task_scope": "range"},
                        "file_selection": {
                            "source_kind": "non_git",
                            "source_root": str(source),
                            "complete": True,
                            "before_files": [
                                {
                                    "path": "seed.txt",
                                    "source_path": str(source / "seed.txt"),
                                    "source_kind": "file",
                                    "size": 6,
                                    "classification": "existed_before_claude",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            workspace = server._materialize_workspace(run, {"type": "projectless"})
            self.assertEqual((workspace / "seed.txt").read_text(encoding="utf-8"), "alpha\n")
            self.assertFalse((workspace / ".git").exists())
