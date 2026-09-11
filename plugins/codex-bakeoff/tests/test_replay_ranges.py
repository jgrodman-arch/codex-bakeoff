"""Behavioral coverage for Replay ranges and whole-file carried inputs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_lean_bakeoff import _args, replay_engine


class ReplayRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "models.json").write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-test",
                            "display_name": "GPT Test",
                            "description": "Test model",
                            "visibility": "list",
                            "supported_in_api": True,
                            "is_default": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _range_fixture(self, kind: str) -> tuple[argparse.Namespace, list[str]]:
        project = self.root / kind
        project.mkdir()

        def git(*arguments: str, timestamp: str | None = None) -> str:
            env = dict(os.environ)
            if timestamp:
                env.update(GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)
            return subprocess.run(
                ["git", "-C", str(project), *arguments],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        commits: list[str] = []
        if kind != "non_git":
            git("init", "--quiet")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            (project / "README.md").write_text("baseline\n", encoding="utf-8")
            git("add", ".")
            git("commit", "--quiet", "-m", "baseline", timestamp="2026-01-01T10:00:00Z")
            commits.append(git("rev-parse", "HEAD"))
        events = []
        for index, name in enumerate(("seed.txt", "result.txt"), start=1):
            stamp = f"2026-01-01T10:0{index * 2 - 1}:00Z"
            events.append(
                {
                    "type": "user",
                    "uuid": f"u{index}",
                    "cwd": str(project),
                    "timestamp": stamp,
                    "sessionId": "range-fixture",
                    "message": {"role": "user", "content": f"Create {name}."},
                }
            )
            events.extend(
                [
                    {
                        "type": "assistant",
                        "cwd": str(project),
                        "timestamp": stamp,
                        "message": {
                            "role": "assistant",
                            "model": "claude-test",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"write{index}",
                                    "name": "Write",
                                    "input": {
                                        "file_path": str(project / name),
                                        "content": "recorded contents\n",
                                    },
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "timestamp": stamp,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": f"write{index}",
                                    "content": "written",
                                }
                            ],
                        },
                    },
                ]
            )
            (project / name).write_text(f"live {name}\n", encoding="utf-8")
            if kind == "git":
                git("add", name)
                git("commit", "--quiet", "-m", name, timestamp=f"2026-01-01T10:0{index * 2}:00Z")
                commits.append(git("rev-parse", "HEAD"))
                events.extend(
                    [
                        {
                            "type": "assistant",
                            "cwd": str(project),
                            "timestamp": stamp,
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": f"commit{index}",
                                        "name": "Bash",
                                        "input": {"command": "git commit -am result"},
                                    }
                                ],
                            },
                        },
                        {
                            "type": "user",
                            "timestamp": stamp,
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": f"commit{index}",
                                        "content": f"[master {commits[-1]}] result",
                                    }
                                ],
                            },
                        },
                    ]
                )
            events.append(
                {
                    "type": "assistant",
                    "uuid": f"answer{index}",
                    "timestamp": stamp,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"Finished {name}."}],
                    },
                }
            )
        source = self.root / "range.jsonl"
        source.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        (self.root / "ledger.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "source_path": str(source),
                            "content_sha256": "0" * 64,
                            "imported_thread_id": "thread-1",
                            "imported_at": 100,
                            "source_modified_at": 99,
                            "connector_names": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return _args(self.root, kind=kind), commits

    def _initialize_git_after_non_git_turns(self) -> str:
        project = self.root / "non_git"
        subprocess.run(["git", "-C", str(project), "init", "--quiet"], check=True)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--quiet",
                "-m",
                "Initial pages",
            ],
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": "2026-01-01T10:06:00Z",
                "GIT_COMMITTER_DATE": "2026-01-01T10:06:00Z",
            },
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        source = self.root / "range.jsonl"
        with source.open("a", encoding="utf-8") as stream:
            for event in [
                {
                    "type": "user",
                    "uuid": "u3",
                    "cwd": str(project),
                    "timestamp": "2026-01-01T10:05:00Z",
                    "message": {"role": "user", "content": "Initialize Git and commit the files."},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-01-01T10:06:00Z",
                    "cwd": str(project),
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "init-commit",
                                "name": "Bash",
                                "input": {
                                    "command": "git init && git add . && git commit -m pages"
                                },
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-01-01T10:06:00Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "init-commit",
                                "content": f"[master (root-commit) {commit}] pages",
                            }
                        ],
                    },
                },
            ]:
                stream.write(json.dumps(event) + "\n")
        return commit

    def _review_range(self, args: argparse.Namespace) -> dict:
        replay = replay_engine._selected_replay(args)
        baseline = replay_engine._baseline(args, replay)
        _, pending = replay_engine._classified_baseline(args, baseline, replay)
        if pending["source_kind"] == "git":
            args.claude_output_file = [item["path"] for item in pending["claude_output_changes"]]
        else:
            args.created_by_claude = [item["path"] for item in pending["claude_output_files"]]
            args.exclude_file = [item["path"] for item in pending["classifications"]["exclude"]]
        args.confirm_file_selection = True
        args.confirm_empty_beginning = True
        baseline, selected = replay_engine._classified_baseline(args, baseline, replay)
        self.assertTrue(selected["complete"])
        return {"replay": replay, "baseline": baseline, "file_selection": selected}

    def _queued_file_fixture(
        self,
        groups: list[tuple[list[str], list[str]]],
        *,
        failed_paths: tuple[str, ...] = (),
    ) -> argparse.Namespace:
        args, _ = self._range_fixture("non_git")
        project = self.root / "non_git"
        events: list[dict] = []
        turn = 0
        for group_index, (prompts, paths) in enumerate(groups):
            for prompt in prompts:
                turn += 1
                event = {
                    "uuid": f"u{turn}",
                    "cwd": str(project),
                    "timestamp": "2026-01-01T10:01:00Z",
                }
                prompt = prompt.replace("{project}", str(project))
                if turn == 1:
                    event.update(type="user", message={"role": "user", "content": prompt})
                else:
                    event.update(
                        type="attachment",
                        attachment={
                            "type": "queued_command",
                            "commandMode": "prompt",
                            "origin": {"kind": "human"},
                            "prompt": prompt,
                        },
                    )
                events.append(event)
            for name in paths:
                path = project / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"current whole {name}\n")
                tool_id = f"write-{len(events)}"
                events.extend(
                    [
                        {
                            "type": "assistant",
                            "cwd": str(project),
                            "timestamp": "2026-01-01T10:01:01Z",
                            "requestId": f"request-{group_index}",
                            "message": {
                                "id": f"message-{group_index}",
                                "usage": {"input_tokens": 100, "output_tokens": 10},
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": tool_id,
                                        "name": "Write",
                                        "input": {
                                            "file_path": str(path),
                                            "content": "old contents\n",
                                        },
                                    }
                                ],
                            },
                        },
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "is_error": name in failed_paths,
                                        "content": "recorded result",
                                    }
                                ],
                            },
                        },
                    ]
                )
        events.append(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "Finished the queued requests."},
            }
        )
        (self.root / "range.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n"
        )
        return args

    def test_queued_file_attribution_preserves_ranges_and_carries_prior_whole_files(self) -> None:
        args = self._queued_file_fixture(
            [
                (["Create a.html."], ["a.html"]),
                (["b.html", "c.html", "d.html"], ["b.html", "c.html", "d.html"]),
                (["e.html", "f.html"], ["e.html", "f.html"]),
            ]
        )
        whole = self._review_range(args)
        self.assertEqual(whole["replay"]["user_message_count"], 6)
        self.assertEqual(whole["replay"]["observed_tools"], ["Write"])
        self.assertFalse(whole["replay"]["historical_usage_shared"])
        self.assertEqual(whole["replay"]["historical_usage"]["input_tokens"], 300)
        _, recovery, answer = replay_engine._historical_candidate(whole)
        self.assertEqual(recovery["changed_files"], [f"{letter}.html" for letter in "abcdef"])
        self.assertEqual(answer, "Finished the queued requests.")
        for first, last in [(1, 1), (2, 3), (4, 4), (5, 5), (6, 6), (2, 4), (2, 6)]:
            with self.subTest(first=first, last=last):
                args = _args(self.root, kind="non_git")
                args.start_message_uuid, args.end_message_uuid = f"u{first}", f"u{last}"
                chunk = self._review_range(args)
                self.assertEqual(
                    chunk["replay"]["historical_usage_shared"],
                    (first, last) in {(2, 3), (4, 4), (5, 5), (6, 6)},
                )
                expected = [f"{letter}.html" for letter in "abcdef"[first - 1 : last]]
                earlier = [f"{letter}.html" for letter in "abcdef"[: first - 1]]
                self.assertEqual(
                    [item["path"] for item in chunk["file_selection"]["claude_output_files"]],
                    expected,
                )
                self.assertEqual(chunk["file_selection"].get("carried_forward_files", []), earlier)
                self.assertEqual(
                    chunk["replay"]["message_uuids"],
                    [f"u{index}" for index in range(first, last + 1)],
                )
                candidate, recovery, _ = replay_engine._historical_candidate(chunk)
                self.assertEqual(recovery["changed_files"], expected)
                self.assertNotIn("old contents", candidate.diff)
                if (first, last) == (2, 3):
                    self.assertEqual(chunk["replay"]["request"], "b.html\n\nc.html")
                    self.assertEqual(chunk["replay"]["observed_tools"], [])
                    self.assertEqual(chunk["replay"]["historical_usage"]["input_tokens"], 0)
                if first == 4:
                    self.assertEqual(chunk["replay"]["historical_usage"]["input_tokens"], 100)
                    workspace = self.root / "queued-carry"
                    workspace.mkdir()
                    replay_engine._file_selection().materialize_carried_forward_files(
                        chunk["file_selection"], workspace
                    )
                    self.assertEqual((workspace / "b.html").read_text(), "current whole b.html\n")

    def test_adjacent_normal_and_queued_prompts_match_explicit_absolute_and_relative_paths(
        self,
    ) -> None:
        self._queued_file_fixture(
            [
                (
                    ["Create {project}/left/name.txt.", "Create right/name.txt.", "Finish"],
                    ["left/name.txt", "right/name.txt"],
                ),
            ]
        )
        for index, expected in [(1, "left/name.txt"), (2, "right/name.txt")]:
            args = _args(self.root, kind="non_git")
            args.start_message_uuid = args.end_message_uuid = f"u{index}"
            selected = self._review_range(args)
            self.assertEqual(
                [item["path"] for item in selected["file_selection"]["claude_output_files"]],
                [expected],
            )

    def test_queued_file_attribution_keeps_ambiguity_and_excludes_failed_or_unobserved_writes(
        self,
    ) -> None:
        self._queued_file_fixture(
            [
                (
                    [
                        "Create b.html. Also failed.txt and missing.txt.",
                        "Create b.html.bak and shared.txt",
                        "Also shared.txt and dup.txt",
                        "Finish",
                    ],
                    [
                        "b.html",
                        "b.html.bak",
                        "shared.txt",
                        "left/dup.txt",
                        "right/dup.txt",
                        "failed.txt",
                    ],
                ),
            ],
            failed_paths=("failed.txt",),
        )
        (self.root / "non_git/missing.txt").write_text("present but never written in transcript")
        expected = {
            1: ["b.html"],
            2: ["b.html.bak"],
            3: [],
            4: ["left/dup.txt", "right/dup.txt", "shared.txt"],
        }
        for index, files in expected.items():
            with self.subTest(index=index):
                args = _args(self.root, kind="non_git")
                args.start_message_uuid = args.end_message_uuid = f"u{index}"
                replay = replay_engine._selected_replay(args)
                self.assertEqual(
                    replay["historical_changed_files"],
                    [str(self.root / "non_git" / path) for path in files],
                )
                self.assertNotIn(
                    str(self.root / "non_git/failed.txt"), replay["prior_historical_changed_files"]
                )
                self.assertNotIn(
                    str(self.root / "non_git/missing.txt"), replay["prior_historical_changed_files"]
                )

    def test_whole_and_chunked_git_use_the_same_commit_recovery(self) -> None:
        args, commits = self._range_fixture("git")
        whole = self._review_range(args)
        self.assertEqual(whole["replay"]["task_scope"], "whole_thread")
        self.assertEqual(whole["baseline"]["commit"], commits[0])
        self.assertEqual(whole["baseline"]["ending_commit"], commits[2])
        candidate, _, answer = replay_engine._historical_candidate(whole)
        self.assertIsNone(candidate.repository_state)
        self.assertIn("seed.txt", candidate.diff)
        self.assertIn("result.txt", candidate.diff)
        self.assertEqual(answer, "Finished result.txt.")
        for index, expected_path in enumerate(("seed.txt", "result.txt"), start=1):
            args = _args(self.root, kind="git")
            args.start_message_uuid = args.end_message_uuid = f"u{index}"
            chunk = self._review_range(args)
            self.assertEqual(chunk["baseline"]["commit"], commits[index - 1])
            self.assertEqual(chunk["baseline"]["ending_commit"], commits[index])
            candidate, recovery, answer = replay_engine._historical_candidate(chunk)
            self.assertEqual(candidate.repository_state["beginning"]["commit"], commits[index - 1])
            self.assertEqual(candidate.repository_state["ending"]["commit"], commits[index])
            self.assertEqual(candidate.repository_state["ending"]["basis"], "resolved_commit")
            self.assertEqual(candidate.repository_state["ending"]["working_tree"], "unknown")
            self.assertEqual(recovery["changed_files"], [expected_path])
            self.assertEqual(answer, f"Finished {expected_path}.")
            self.assertEqual(chunk["replay"]["message_uuids"], [f"u{index}"])
            self.assertEqual(len(chunk["replay"]["actionable_user_turns"]), 2)
            self.assertNotIn("before_files", chunk["file_selection"])

    def test_whole_non_git_and_later_only_chunk_use_live_whole_files(self) -> None:
        args, _ = self._range_fixture("non_git")
        whole = self._review_range(args)
        self.assertEqual(whole["baseline"]["kind"], "empty_directory")
        self.assertEqual(whole["file_selection"]["before_files"], [])
        candidate, recovery, _ = replay_engine._historical_candidate(whole)
        self.assertEqual(recovery["changed_files"], ["result.txt", "seed.txt"])
        self.assertIn("live seed.txt", candidate.diff)
        self.assertNotIn("recorded contents", candidate.diff)
        args = _args(self.root, kind="non_git")
        args.start_message_uuid = args.end_message_uuid = "u2"
        later = self._review_range(args)
        self.assertEqual(later["baseline"]["kind"], "empty_directory")
        self.assertEqual(later["file_selection"]["carried_forward_files"], ["seed.txt"])
        workspace = self.root / "workspace"
        workspace.mkdir()
        replay_engine._file_selection().materialize_carried_forward_files(
            later["file_selection"], workspace
        )
        self.assertEqual((workspace / "seed.txt").read_text(), "live seed.txt\n")
        candidate, recovery, _ = replay_engine._historical_candidate(later)
        self.assertEqual(recovery["changed_files"], ["result.txt"])
        self.assertNotIn("seed.txt", candidate.diff)
        (workspace / "result.txt").write_text("Codex result\n")
        _, _, changed = replay_engine._codex_candidate(later, {"worktree": str(workspace)})
        self.assertEqual(changed, ("result.txt",))

    def test_chunked_dirty_git_attributes_outputs_and_carries_earlier_inputs(self) -> None:
        args, commits = self._range_fixture("dirty_git")
        args.start_message_uuid = args.end_message_uuid = "u1"
        first = self._review_range(args)
        self.assertEqual(
            [item["path"] for item in first["file_selection"]["claude_output_changes"]],
            ["seed.txt"],
        )
        _, recovery, _ = replay_engine._historical_candidate(first)
        self.assertEqual(recovery["changed_files"], ["seed.txt"])
        args = _args(self.root, kind="git")
        args.start_message_uuid = args.end_message_uuid = "u2"
        later = self._review_range(args)
        self.assertEqual(later["baseline"]["commit"], commits[0])
        self.assertEqual(later["baseline"]["ending_commit"], commits[0])
        self.assertEqual(later["file_selection"]["carried_forward_files"], ["seed.txt"])
        candidate, recovery, _ = replay_engine._historical_candidate(later)
        self.assertEqual(recovery["changed_files"], ["result.txt"])
        self.assertNotIn("seed.txt", candidate.diff)
        workspace = self.root / "workspace"
        subprocess.run(
            ["git", "clone", "--quiet", later["baseline"]["repository"], str(workspace)], check=True
        )
        replay_engine._file_selection().materialize_carried_forward_files(
            later["file_selection"], workspace
        )
        (workspace / "result.txt").write_text("Codex result\n")
        _, _, changed = replay_engine._codex_candidate(later, {"worktree": str(workspace)})
        self.assertEqual(changed, ("result.txt",))

    def test_pre_init_non_git_chunk_uses_live_files_after_repository_is_created(self) -> None:
        args, _ = self._range_fixture("non_git")
        commit = self._initialize_git_after_non_git_turns()
        whole = self._review_range(args)
        self.assertEqual(whole["baseline"]["beginning_kind"], "non_git")
        self.assertEqual(whole["baseline"]["ending_kind"], "git")
        self.assertEqual(whole["baseline"]["ending_commit"], commit)
        self.assertEqual(whole["file_selection"]["source_kind"], "git")
        _, recovery, _ = replay_engine._historical_candidate(whole)
        self.assertEqual(recovery["changed_files"], ["result.txt", "seed.txt"])

        for reviewed in (False, True):
            with self.subTest(reviewed_states=reviewed):
                args = _args(self.root, kind="non_git")
                args.start_message_uuid = args.end_message_uuid = "u1"
                if reviewed:
                    args.beginning_kind = args.ending_kind = "non_git"
                first = self._review_range(args)
                self.assertEqual(first["baseline"]["ending_kind"], "non_git")
                self.assertEqual(first["file_selection"]["source_kind"], "non_git")
                self.assertEqual(
                    [item["path"] for item in first["file_selection"]["candidates"]],
                    ["result.txt", "seed.txt"],
                )
                self.assertEqual(
                    [item["path"] for item in first["file_selection"]["claude_output_files"]],
                    ["seed.txt"],
                )
                candidate, recovery, _ = replay_engine._historical_candidate(first)
                self.assertEqual(candidate.repository_state["ending"]["kind"], "non_git")
                self.assertIsNone(candidate.repository_state["ending"]["commit"])
                self.assertEqual(recovery["changed_files"], ["seed.txt"])
                self.assertIn("live seed.txt", candidate.diff)
                self.assertNotIn("recorded contents", candidate.diff)

    def test_non_git_to_git_chunk_carries_committed_prior_file_and_keeps_end_hash(self) -> None:
        args, _ = self._range_fixture("non_git")
        commit = self._initialize_git_after_non_git_turns()
        args.start_message_uuid = "u2"
        args.end_message_uuid = "u3"
        args.beginning_kind = "non_git"
        args.ending_kind = "git"
        args.ending_commit = commit
        later = self._review_range(args)
        self.assertEqual(later["baseline"]["kind"], "empty_directory")
        self.assertEqual(later["baseline"]["ending_commit"], commit)
        self.assertEqual(later["file_selection"]["source_kind"], "git")
        self.assertEqual(later["file_selection"]["candidates"], [])
        self.assertEqual(later["file_selection"]["carried_forward_files"], ["seed.txt"])
        candidate, recovery, _ = replay_engine._historical_candidate(later)
        self.assertEqual(candidate.repository_state["beginning"]["kind"], "non_git")
        self.assertEqual(candidate.repository_state["ending"]["commit"], commit)
        self.assertEqual(recovery["changed_files"], ["result.txt"])
        self.assertNotIn("seed.txt", candidate.diff)
        self.assertIn("live result.txt", candidate.diff)

        workspace = self.root / "workspace"
        workspace.mkdir()
        replay_engine._file_selection().materialize_carried_forward_files(
            later["file_selection"], workspace
        )
        self.assertEqual((workspace / "seed.txt").read_text(), "live seed.txt\n")
        (workspace / "result.txt").write_text("Codex result\n")
        subprocess.run(["git", "-C", str(workspace), "init", "--quiet"], check=True)
        _, _, changed = replay_engine._codex_candidate(later, {"worktree": str(workspace)})
        self.assertEqual(changed, ("result.txt",))

    def test_root_commit_does_not_attribute_prior_file_as_current_dirty_output(self) -> None:
        args, _ = self._range_fixture("non_git")
        commit = self._initialize_git_after_non_git_turns()
        (self.root / "non_git" / "seed.txt").write_text("current whole input\n")
        (self.root / "non_git" / "result.txt").write_text("unselected live result\n")
        args.start_message_uuid = "u2"
        args.end_message_uuid = "u3"
        replay = replay_engine._selected_replay(args)
        baseline = replay_engine._baseline(args, replay)
        self.assertEqual(baseline["ending_commit"], commit)
        _, selection = replay_engine._classified_baseline(args, baseline, replay)
        self.assertEqual(selection["carried_forward_files"], ["seed.txt"])
        self.assertEqual(selection["transcript_inferred_files"], ["result.txt"])
        self.assertEqual(
            [item["path"] for item in selection["claude_output_changes"]], ["result.txt"]
        )
        args.confirm_file_selection = args.confirm_empty_beginning = True
        baseline, selection = replay_engine._classified_baseline(args, baseline, replay)
        self.assertEqual(selection["claude_output_changes"], [])
        candidate, recovery, _ = replay_engine._historical_candidate(
            {"replay": replay, "baseline": baseline, "file_selection": selection}
        )
        self.assertEqual(recovery["changed_files"], ["result.txt"])
        self.assertIn("live result.txt", candidate.diff)
        self.assertNotIn("unselected live result", candidate.diff)
        self.assertNotIn("seed.txt", candidate.diff)

    def test_range_judge_receives_frozen_state_and_only_observes_its_workspace(self) -> None:
        args, _ = self._range_fixture("non_git")
        args.start_message_uuid = args.end_message_uuid = "u2"
        later = self._review_range(args)
        historical, recovery, final = replay_engine._historical_candidate(later)
        frozen = replay_engine._serialize_historical_candidate(historical, recovery, final)
        run_directory = self.root / "review-run"
        run_directory.mkdir()
        serialized = (json.dumps(frozen) + "\n").encode()
        (run_directory / "historical-result.json").write_bytes(serialized)
        restored, _, _ = replay_engine._historical_candidate_for_completion(
            {
                **later,
                "historical_result": {
                    "schema_version": 1,
                    "path": "historical-result.json",
                    "sha256": replay_engine.hashlib.sha256(serialized).hexdigest(),
                },
            },
            run_directory,
        )
        self.assertEqual(restored.repository_state, historical.repository_state)
        tampered = json.loads(serialized)
        tampered["candidate"]["repository_state"]["ending"]["kind"] = "git"
        (run_directory / "historical-result.json").write_text(json.dumps(tampered))
        with self.assertRaisesRegex(replay_engine.ReplayError, "digest does not match"):
            replay_engine._historical_candidate_for_completion(
                {
                    **later,
                    "historical_result": {
                        "schema_version": 1,
                        "path": "historical-result.json",
                        "sha256": replay_engine.hashlib.sha256(serialized).hexdigest(),
                    },
                },
                run_directory,
            )
        workspace = self.root / "workspace"
        workspace.mkdir()
        replay_engine._file_selection().materialize_carried_forward_files(
            later["file_selection"], workspace
        )
        (workspace / "result.txt").write_text("result\n")
        external = self.root / "external-repository"
        subprocess.run(["git", "init", "-q", str(external)], check=True)
        native, _, _ = replay_engine._codex_candidate(
            later, {"worktree": str(workspace), "final_output": f"Initialized Git in {external}."}
        )
        self.assertEqual(native.repository_state["ending"]["kind"], "non_git")
        self.assertIsNone(native.repository_state["ending"]["commit"])
        _, restored_native = replay_engine._report_candidates(
            {"candidates": {"codex": replay_engine._jsonable(native)}}
        )
        self.assertEqual(restored_native.repository_state, native.repository_state)
        requests = replay_engine._execution().prepare_review(
            run_directory=run_directory,
            original_request=later["replay"]["request"],
            candidates=(restored, restored_native),
            evaluators=({"id": "codex", "model": "gpt-test"},),
        )
        artifacts = [json.loads(Path(path).read_text()) for path in requests[0]["candidate_paths"]]
        self.assertEqual(artifacts[0]["repository_state"]["ending"]["kind"], "non_git")
        self.assertEqual(artifacts[1]["repository_state"]["ending"]["kind"], "non_git")
        self.assertNotIn(str(external), json.dumps(artifacts))

    def test_carried_inputs_bind_review_to_contents_and_honor_earlier_edits(self) -> None:
        args, _ = self._range_fixture("non_git")
        args.start_message_uuid = args.end_message_uuid = "u2"
        reviewed = self._review_range(args)
        before = reviewed["file_selection"]["before_files"][0]
        self.assertEqual(len(before["content_sha256"]), 64)
        Path(before["source_path"]).write_text("edit seed.txt\n", encoding="utf-8")
        workspace = self.root / "workspace"
        workspace.mkdir()
        with self.assertRaisesRegex(
            replay_engine._file_selection().FileSelectionError, "changed after review"
        ):
            replay_engine._file_selection().materialize_carried_forward_files(
                reviewed["file_selection"], workspace
            )
        _, changed = replay_engine._classified_baseline(
            args, reviewed["baseline"], reviewed["replay"]
        )
        self.assertNotEqual(changed["before_files"][0]["content_sha256"], before["content_sha256"])
        args = _args(self.root, kind="non_git")
        args.start_message_uuid = args.end_message_uuid = "u2"
        args.exclude_file = ["seed.txt"]
        replay = replay_engine._selected_replay(args)
        baseline = replay_engine._baseline(args, replay)
        _, selection = replay_engine._classified_baseline(args, baseline, replay)
        self.assertEqual(selection["before_files"], [])
        self.assertEqual(
            [item["path"] for item in selection["claude_output_files"]], ["result.txt"]
        )
        manual = Path(baseline["repository"]) / "manual.txt"
        manual.write_text("manually attributed\n", encoding="utf-8")
        args.carried_forward_file = ["manual.txt"]
        _, selection = replay_engine._classified_baseline(args, baseline, replay)
        self.assertEqual(selection["carried_forward_files"], ["manual.txt"])

    def test_range_boundaries_reject_invalid_end_and_override_legacy_message(self) -> None:
        args, _ = self._range_fixture("non_git")
        args.start_message_uuid = args.end_message_uuid = "u2"
        args.message_uuid = "u1"
        selected = replay_engine._selected_replay(args)
        self.assertEqual(selected["message_uuid"], "u2")
        self.assertEqual(selected["request"], "Create result.txt.")
        self.assertEqual(selected["prior_user_requests"], ["Create seed.txt."])
        self.assertEqual(
            selected["prompt_reconstruction_turns"],
            [{"role": "user", "text": "Create result.txt."}],
        )
        args.start_message_uuid = "u1"
        selected = replay_engine._selected_replay(args)
        self.assertEqual(selected["message_uuids"], ["u1", "u2"])
        self.assertEqual(selected["prior_user_requests"], [])
        args.start_message_uuid = "u2"
        args.end_message_uuid = "u1"
        with self.assertRaisesRegex(replay_engine.ReplayError, "ending user-message"):
            replay_engine._selected_replay(args)
