from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = PLUGIN_ROOT / "scripts" / "historical_discovery.py"
MODULE_SPEC = importlib.util.spec_from_file_location("historical_discovery", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
discovery = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = discovery
MODULE_SPEC.loader.exec_module(discovery)


class HistoricalDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.ledger = self.root / "imports.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def user(self, uuid: str, text: str, timestamp: str) -> dict:
        return {
            "type": "user",
            "uuid": uuid,
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": timestamp,
            "message": {"role": "user", "content": text},
        }

    def assistant(self, uuid: str, text: str, timestamp: str) -> dict:
        return {
            "type": "assistant",
            "uuid": uuid,
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [{"type": "text", "text": text}],
            },
        }

    def queued_command(self, uuid: str, text: str, timestamp: str) -> dict:
        return {
            "type": "attachment",
            "uuid": uuid,
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": timestamp,
            "attachment": {
                "type": "queued_command",
                "prompt": text,
                "commandMode": "prompt",
                "origin": {"kind": "human"},
            },
        }

    def write_transcript(self, name: str, events: list[dict]) -> Path:
        path = self.root / f"{name}.jsonl"
        path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return path

    def record(self, source: Path, *, thread: str, imported_at: int) -> dict:
        return {
            "source_path": str(source),
            "content_sha256": "0" * 64,
            "imported_thread_id": thread,
            "imported_at": imported_at,
            "source_modified_at": imported_at - 1,
            "connector_names": [],
        }

    def write_ledger(self, records: list[dict]) -> None:
        self.ledger.write_text(json.dumps({"records": records}), encoding="utf-8")

    def select(self, events: list[dict]) -> tuple[dict, dict]:
        source = self.write_transcript("selected", events)
        self.write_ledger([self.record(source, thread="thread-1", imported_at=100)])
        session = discovery.list_imported_sessions(self.ledger)[0]
        return session, discovery.build_thread_task(session)

    def status_events(
        self,
        command: str,
        *,
        cwd: Path,
        timestamp: str | None = "2026-01-01T10:01:00Z",
    ) -> list[dict]:
        tool_event = {
            "type": "assistant",
            "cwd": str(cwd),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "status-1",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        }
        result_event = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "status-1",
                        "content": "nothing to commit, working tree clean",
                    }
                ],
            },
        }
        if timestamp is not None:
            tool_event["timestamp"] = timestamp
            result_event["timestamp"] = "2026-01-01T10:01:01Z"
        return [tool_event, result_event]

    def test_sessions_sort_by_original_creation_time(self) -> None:
        older = self.write_transcript(
            "older",
            [self.user("old", "old request", "2026-01-01T10:00:00Z")],
        )
        newer = self.write_transcript(
            "newer",
            [self.user("new", "new request", "2026-01-02T10:00:00Z")],
        )
        self.write_ledger(
            [
                self.record(newer, thread="new", imported_at=100),
                self.record(older, thread="old", imported_at=200),
            ]
        )

        sessions = discovery.list_imported_sessions(self.ledger)

        self.assertEqual([session["imported_thread_id"] for session in sessions], ["new", "old"])

    def test_duplicate_import_uses_latest_record_without_hash_validation(self) -> None:
        source = self.write_transcript(
            "duplicate",
            [self.user("u1", "request", "2026-01-01T10:00:00Z")],
        )
        self.write_ledger(
            [
                self.record(source, thread="older", imported_at=100),
                self.record(source, thread="newer", imported_at=200),
            ]
        )

        sessions = discovery.list_imported_sessions(self.ledger)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["imported_thread_id"], "newer")
        self.assertNotIn("source_hash_status", sessions[0])
        self.assertNotIn("source_sha256", sessions[0])

    def test_whole_thread_replay_combines_user_requests(self) -> None:
        session, task = self.select(
            [
                self.user("u1", "build it", "2026-01-01T10:00:00Z"),
                self.assistant("a1", "working", "2026-01-01T10:01:00Z"),
                self.user("u2", "make it blue", "2026-01-01T10:02:00Z"),
                self.assistant("a2", "done", "2026-01-01T10:03:00Z"),
            ]
        )

        replay = discovery.build_replay_spec(session, task)

        self.assertEqual(task["request"], "build it\n\nmake it blue")
        self.assertEqual(replay["request"], task["request"])
        self.assertEqual(replay["message_uuids"], ["u1", "u2"])
        self.assertEqual(replay["preceding_context"], [])
        self.assertEqual(
            replay["prompt_reconstruction_turns"],
            [
                {"role": "user", "text": "build it"},
                {"role": "user", "text": "make it blue"},
            ],
        )
        self.assertFalse(replay["prompt_reconstruction_truncated"])
        self.assertNotIn("source_sha256", replay)
        self.assertNotIn("configuration_fingerprint", replay)

    def test_whole_thread_replay_includes_human_queued_commands(self) -> None:
        requests = [
            "download the linux repo",
            "create an html diagram walkthrough",
            "create a welcome.sh script to get someone oriented with it",
            "add tests for welcome.sh",
            "commit everything except the html",
        ]
        session, task = self.select(
            [
                self.user("u1", requests[0], "2026-01-01T10:00:00Z"),
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "content": requests[1],
                    "timestamp": "2026-01-01T10:00:01Z",
                },
                self.queued_command("q1", requests[1], "2026-01-01T10:00:01Z"),
                self.queued_command("q2", requests[2], "2026-01-01T10:00:02Z"),
                self.queued_command("q3", requests[3], "2026-01-01T10:00:03Z"),
                self.user("u2", requests[4], "2026-01-01T10:00:04Z"),
            ]
        )

        replay = discovery.build_replay_spec(session, task)

        self.assertEqual(session["task_count"], 5)
        self.assertEqual(task["request"], "\n\n".join(requests))
        self.assertEqual(replay["message_uuids"], ["u1", "q1", "q2", "q3", "u2"])
        self.assertEqual(
            replay["prompt_reconstruction_turns"],
            [{"role": "user", "text": request} for request in requests],
        )

    def test_prompt_reconstruction_resolves_numbered_reply_without_solution_output(self) -> None:
        clarification = (
            "I need clarification. Are you asking me to:\n\n"
            "1. Create a CLI script?\n"
            "2. Add a documentation example?\n"
            "3. Add a simple program somewhere in the project?\n"
            "4. Something else?\n\n"
            "What would be most helpful?"
        )
        session, task = self.select(
            [
                self.user("u1", "add hello world", "2026-01-01T10:00:00Z"),
                self.assistant(
                    "a1",
                    "I inspected the project and found the implementation location.",
                    "2026-01-01T10:01:00Z",
                ),
                self.assistant("a2", clarification, "2026-01-01T10:02:00Z"),
                self.user("u2", "3", "2026-01-01T10:03:00Z"),
                self.assistant(
                    "a3",
                    "Implemented the program and all tests pass.",
                    "2026-01-01T10:04:00Z",
                ),
                self.user("u3", "make it executable", "2026-01-01T10:05:00Z"),
            ]
        )

        replay = discovery.build_replay_spec(session, task)

        self.assertEqual(
            replay["prompt_reconstruction_turns"],
            [
                {"role": "user", "text": "add hello world"},
                {
                    "role": "assistant",
                    "text": (
                        "1. Create a CLI script?\n"
                        "2. Add a documentation example?\n"
                        "3. Add a simple program somewhere in the project?\n"
                        "4. Something else?\n\n"
                        "What would be most helpful?"
                    ),
                },
                {"role": "user", "text": "3"},
                {"role": "user", "text": "make it executable"},
            ],
        )
        serialized = json.dumps(replay["prompt_reconstruction_turns"])
        self.assertNotIn("implementation location", serialized)
        self.assertNotIn("all tests pass", serialized)

    def test_prompt_reconstruction_includes_structured_question_answers(self) -> None:
        question_event = {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": "2026-01-01T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "question-1",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Which output should I create?",
                                    "options": [
                                        {
                                            "label": "CLI",
                                            "description": "Create a command-line example.",
                                        },
                                        {
                                            "label": "Docs",
                                            "description": "Create a documentation example.",
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
        }
        answer_event = {
            "type": "user",
            "uuid": "answer-1",
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": "2026-01-01T10:02:00Z",
            "sourceToolAssistantUUID": "a1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "question-1",
                        "content": "The user selected CLI.",
                    }
                ],
            },
            "toolUseResult": {
                "answers": {"Which output should I create?": "CLI"}
            },
        }
        session, task = self.select(
            [
                self.user("u1", "add an example", "2026-01-01T10:00:00Z"),
                question_event,
                answer_event,
            ]
        )

        replay = discovery.build_replay_spec(session, task)

        self.assertEqual(
            replay["prompt_reconstruction_turns"],
            [
                {"role": "user", "text": "add an example"},
                {
                    "role": "assistant",
                    "text": (
                        "Which output should I create?\n"
                        "1. CLI — Create a command-line example.\n"
                        "2. Docs — Create a documentation example."
                    ),
                },
                {
                    "role": "user",
                    "text": "Which output should I create?: CLI",
                },
            ],
        )

    def test_prompt_reconstruction_marks_truncated_turns(self) -> None:
        request = "x" * (discovery.MAX_PROMPT_RECONSTRUCTION_TURN_CHARS + 1)
        session, task = self.select(
            [self.user("u1", request, "2026-01-01T10:00:00Z")]
        )

        replay = discovery.build_replay_spec(session, task)

        self.assertTrue(replay["prompt_reconstruction_truncated"])
        self.assertEqual(
            len(replay["prompt_reconstruction_turns"][0]["text"]),
            discovery.MAX_PROMPT_RECONSTRUCTION_TURN_CHARS,
        )

    def test_source_changes_do_not_invalidate_replay(self) -> None:
        session, task = self.select([self.user("u1", "request", "2026-01-01T10:00:00Z")])
        replay = discovery.build_replay_spec(session, task)
        source = Path(replay["source_path"])
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(self.assistant("a1", "later observation", "2026-01-01T10:01:00Z")) + "\n"
            )

        current = discovery.validate_replay_sources(replay)

        self.assertEqual(current["source_path"], str(source))
        self.assertEqual(current["linked_sources"], [])
        self.assertEqual(
            discovery.recover_historical_final_response(
                source,
                task["message_uuid"],
                whole_thread=True,
            ),
            "later observation",
        )

    def test_git_status_evidence_matches_selected_repository(self) -> None:
        other = self.root / "other"
        other.mkdir()
        command = f"cd {other} && git status"
        events = [
            {
                "type": "assistant",
                "cwd": str(self.project),
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "write-1",
                            "name": "Write",
                            "input": {
                                "file_path": str(self.project / "changed.txt"),
                                "content": "changed\n",
                            },
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "cwd": str(self.project),
                "timestamp": "2026-01-01T10:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "status-1",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-01-01T10:01:01Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "status-1",
                            "content": "nothing to commit, working tree clean",
                        }
                    ],
                },
            },
        ]

        matched = discovery._status_evidence(
            events,
            source_path=self.root / "selected.jsonl",
            repository=other,
        )
        mismatched = discovery._status_evidence(
            events,
            source_path=self.root / "selected.jsonl",
            repository=self.project,
        )

        self.assertEqual(matched["state"], "verified_clean")
        self.assertEqual(matched["evidence"]["command"], command)
        self.assertEqual(mismatched["state"], "unknown")

    def test_git_status_evidence_rejects_ambiguous_shell_forms(self) -> None:
        other = self.root / "other"
        other.mkdir()

        matched = discovery._status_evidence(
            self.status_events(
                f"git -C {other} status --short",
                cwd=self.project,
            ),
            source_path=self.root / "selected.jsonl",
            repository=other,
        )
        self.assertEqual(matched["state"], "verified_clean")
        for command in (
            f"cd {other}; git status",
            f"git --no-pager -C {other} status",
            f"git status && git -C {other} status",
        ):
            with self.subTest(command=command):
                evidence = discovery._status_evidence(
                    self.status_events(command, cwd=self.project),
                    source_path=self.root / "selected.jsonl",
                    repository=self.project,
                )
                self.assertEqual(evidence["state"], "unknown")

    def test_child_directory_git_mutation_stops_clean_status_evidence(self) -> None:
        subprocess.run(
            ["git", "init", "-q", str(self.project)],
            check=True,
            capture_output=True,
            text=True,
        )
        child = self.project / "child"
        child.mkdir()
        mutation = {
            "type": "assistant",
            "cwd": str(child),
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "reset-1",
                        "name": "Bash",
                        "input": {"command": "git reset --hard"},
                    }
                ],
            },
        }

        evidence = discovery._status_evidence(
            [mutation, *self.status_events("git status", cwd=self.project)],
            source_path=self.root / "selected.jsonl",
            repository=self.project,
        )

        self.assertEqual(evidence["state"], "unknown")
        self.assertIsNone(evidence["evidence"])

    def test_nonexistent_child_git_mutation_fails_closed(self) -> None:
        subprocess.run(
            ["git", "init", "-q", str(self.project)],
            check=True,
            capture_output=True,
            text=True,
        )
        mutation = {
            "type": "assistant",
            "cwd": str(self.project / "removed-child"),
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "reset-1",
                        "name": "Bash",
                        "input": {"command": "git reset --hard"},
                    }
                ],
            },
        }

        evidence = discovery._status_evidence(
            [mutation, *self.status_events("git status", cwd=self.project)],
            source_path=self.root / "selected.jsonl",
            repository=self.project,
        )

        self.assertEqual(evidence["state"], "unknown")
        self.assertIsNone(evidence["evidence"])

    def test_unresolved_mutation_targets_fail_closed(self) -> None:
        other = self.root / "other"
        other.mkdir()
        mutations = (
            (
                "missing structured path",
                "Edit",
                {"old_string": "before", "new_string": "after"},
                self.project,
            ),
            (
                "apply patch",
                "apply_patch",
                {"patch": "*** Begin Patch\n*** End Patch"},
                other,
            ),
            (
                "absolute shell target",
                "Bash",
                {"command": f"touch {self.project / 'changed.txt'}"},
                other,
            ),
        )

        for label, name, arguments, cwd in mutations:
            with self.subTest(label=label):
                mutation = {
                    "type": "assistant",
                    "cwd": str(cwd),
                    "timestamp": "2026-01-01T10:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "mutation-1",
                                "name": name,
                                "input": arguments,
                            }
                        ],
                    },
                }
                evidence = discovery._status_evidence(
                    [mutation, *self.status_events("git status", cwd=self.project)],
                    source_path=self.root / "selected.jsonl",
                    repository=self.project,
                )

                self.assertEqual(evidence["state"], "unknown")
                self.assertIsNone(evidence["evidence"])

    def test_git_project_subdirectory_uses_top_level_mutation_scope(self) -> None:
        subprocess.run(
            ["git", "init", "-q", str(self.project)],
            check=True,
            capture_output=True,
            text=True,
        )
        selected = self.project / "selected"
        selected.mkdir()
        events = [
            {
                **self.user("u1", "change it", "2026-01-01T09:59:00Z"),
                "cwd": str(selected),
            },
            {
                "type": "assistant",
                "cwd": str(selected),
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "write-1",
                            "name": "Write",
                            "input": {
                                "file_path": str(self.project / "sibling.txt"),
                                "content": "changed\n",
                            },
                        }
                    ],
                },
            },
            *self.status_events("git status", cwd=selected),
        ]
        source = self.write_transcript("nested-project", events)

        baseline = discovery.inspect_baseline(
            {
                "project_dir": str(selected),
                "source_path": str(source),
                "message_uuid": "u1",
                "task_timestamp": "2026-01-01T09:59:00Z",
            }
        )

        self.assertEqual(baseline["repository"], str(self.project.resolve()))
        self.assertEqual(baseline["working_tree_state"], "unknown")
        self.assertIsNone(baseline["working_tree"]["evidence"])

    def test_baseline_inspection_accepts_linked_sources(self) -> None:
        subprocess.run(
            ["git", "init", "-q", str(self.project)],
            check=True,
            capture_output=True,
            text=True,
        )
        source = self.write_transcript(
            "linked-source",
            [
                self.user("u1", "change it", "2026-01-01T09:59:00Z"),
                *self.status_events("git status", cwd=self.project),
            ],
        )

        baseline = discovery.inspect_baseline(
            {
                "project_dir": str(self.project),
                "source_path": str(source),
                "message_uuid": "u1",
                "task_timestamp": "2026-01-01T09:59:00Z",
                "linked_sources": [{"source_path": str(source)}],
            }
        )

        self.assertEqual(baseline["repository"], str(self.project.resolve()))

    def test_undated_status_cannot_cross_mutation_cutoff(self) -> None:
        evidence = discovery._status_evidence(
            self.status_events("git status", cwd=self.project, timestamp=None),
            source_path=self.root / "selected.jsonl",
            repository=self.project,
            mutation_cutoff=discovery._parse_timestamp("2026-01-01T10:00:00Z"),
        )

        self.assertEqual(evidence["state"], "unknown")
        self.assertIsNone(evidence["evidence"])

    def test_reviewed_ending_commit_controls_historical_git_range(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.com"],
            check=True,
        )
        target = self.project / "feature.txt"
        target.write_text("start\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "feature.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "commit", "-qm", "start"],
            check=True,
        )
        start = subprocess.run(
            ["git", "-C", str(self.project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        target.write_text("ending\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "feature.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "commit", "-qm", "ending"],
            check=True,
        )
        ending = subprocess.run(
            ["git", "-C", str(self.project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        source = self.write_transcript(
            "reviewed-ending",
            [self.user("u1", "change it", "2026-01-01T10:00:00Z")],
        )
        replay = {
            "project_dir": str(self.project),
            "source_path": str(source),
            "message_uuid": "u1",
        }

        recovered = discovery.recover_historical_solution(
            replay,
            start,
            ending_commit=ending,
        )
        self.assertEqual(recovered["provenance"], "user_reviewed_git_commit")
        self.assertEqual(recovered["commit"], ending)
        self.assertEqual(recovered["changed_files"], ["feature.txt"])
        self.assertIn("+ending", recovered["diff"])

        unchanged = discovery.recover_historical_solution(
            replay,
            start,
            ending_commit=start,
        )
        self.assertEqual(unchanged["commit"], start)
        self.assertEqual(unchanged["diff"], "")

        tree = subprocess.run(
            ["git", "-C", str(self.project), "rev-parse", f"{ending}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        unrelated = subprocess.run(
            ["git", "-C", str(self.project), "commit-tree", tree],
            check=True,
            capture_output=True,
            text=True,
            input="unrelated\n",
        ).stdout.strip()
        rejected = discovery.recover_historical_solution(
            replay,
            start,
            ending_commit=unrelated,
        )
        self.assertIsNone(rejected["diff"])
        self.assertIn("does not descend", rejected["limitations"][0])

    def test_non_git_beginning_to_git_end_diffs_from_empty_tree(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.com"],
            check=True,
        )
        (self.project / "index.html").write_text("<h1>Hello</h1>\n", encoding="utf-8")
        (self.project / "app.js").write_text("console.log('ready');\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "index.html", "app.js"], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "commit", "-qm", "historical result"],
            check=True,
        )
        ending = subprocess.run(
            ["git", "-C", str(self.project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        source = self.write_transcript(
            "non-git-to-git",
            [self.user("u1", "build it", "2026-01-01T10:00:00Z")],
        )

        recovered = discovery.recover_historical_solution(
            {
                "project_dir": str(self.project),
                "source_path": str(source),
                "message_uuid": "u1",
            },
            baseline_kind="empty_directory",
            ending_commit=ending,
        )

        self.assertEqual(recovered["provenance"], "user_reviewed_git_commit")
        self.assertEqual(recovered["commit"], ending)
        self.assertEqual(recovered["changed_files"], ["app.js", "index.html"])
        self.assertIn("+<h1>Hello</h1>", recovered["diff"])
        self.assertTrue(recovered["evidence"][0]["empty_tree_baseline"])


if __name__ == "__main__":
    unittest.main()
