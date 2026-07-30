"""Tests for the record-only orchestration."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "historical_bakeoff.py"
SPEC = importlib.util.spec_from_file_location("lean_bakeoff", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bakeoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bakeoff)


def _args(root: Path, *, kind: str) -> argparse.Namespace:
    return argparse.Namespace(
        imported_thread_id="thread-1",
        ledger=root / "ledger.json",
        repo=None,
        claude_output_file=None,
        created_by_claude=None,
        exclude_file=None,
        confirm_file_selection=False,
        model="gpt-test",
        model_cache=root / "models.json",
        timeout_seconds=1800,
        run_root=root / "runs",
        approve=True,
    )


def _review_ballot(*, field: str = "outcome") -> str:
    return json.dumps(
        {
            "categories": {
                "task_completion": {
                    field: "B",
                    "explanation": "B completed more of the task.",
                },
                "style_conciseness": {
                    field: "A",
                    "explanation": "A was more concise.",
                },
                "edge_cases": {
                    field: "B",
                    "explanation": "B handled more edge cases.",
                },
                "verification_results": {field: "not_applicable"},
                "security": {field: "not_applicable"},
            }
        }
    )


class LeanBakeoffTests(unittest.TestCase):
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

    def _patch_context(self, kind: str):
        repository = self.root / "source"
        repository.mkdir(exist_ok=True)
        if kind == "git_commit":
            subprocess.run(
                ["git", "-C", str(repository), "init", "--quiet"],
                check=True,
            )
        replay = {
            "request": "build the thing",
            "project_dir": str(repository),
            "imported_thread_id": "thread-1",
            "session_id": "claude-1",
        }
        baseline = {
            "kind": kind,
            "repository": str(repository),
            "commit": "a" * 40 if kind == "git_commit" else None,
            "confidence": "verified" if kind == "git_commit" else "user_confirmed",
            "working_tree_state": ("verified_clean" if kind == "git_commit" else "unknown"),
            "source_kind": "git" if kind == "git_commit" else "non_git",
        }
        parity = {"items": []}
        return (
            mock.patch.multiple(
                bakeoff,
                _selected_replay=mock.DEFAULT,
                _baseline=mock.DEFAULT,
                _capabilities=mock.DEFAULT,
            ),
            replay,
            baseline,
            parity,
        )

    def test_empty_run_targets_a_projectless_workspace(self) -> None:
        patches, replay, baseline, parity = self._patch_context("empty_directory")
        args = _args(self.root, kind="empty_directory")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "needs_user_input")
        self.assertIn(
            "directory was empty before Claude",
            prepared["questions"][0]["question"],
        )

        args.confirm_file_selection = True
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            result = bakeoff._command_run(args)
        self.assertEqual(result["status"], "native_task_required")
        request = result["task_request"]
        self.assertEqual(request["target"]["type"], "projectless")
        self.assertNotIn("configuration_fingerprint", request)
        self.assertIn(
            "Do not read memory files or invoke the codex-bakeoff skill.",
            request["prompt"],
        )
        self.assertNotIn(
            "Do not perform browser automation or extended interactive QA.",
            request["prompt"],
        )
        self.assertNotIn(
            "Run only directly available local syntax checks or baseline-owned tests.",
            request["prompt"],
        )
        self.assertNotIn(
            "Browser and shared verification happen after implementation.",
            request["prompt"],
        )
        self.assertIn("Original user requests:\nbuild the thing", request["prompt"])
        run = json.loads((Path(result["run_directory"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "awaiting_native_task")
        self.assertNotIn("execution_plan", run)
        self.assertTrue(run["approval"]["approved"])

    def test_report_command_backfills_historical_timing_for_existing_run(self) -> None:
        run_directory = self.root / "existing-run"
        run_directory.mkdir()
        (run_directory / "report.json").write_text(
            json.dumps(
                {
                    "original_request": "build",
                    "baseline": {},
                    "candidates": {},
                    "usage": {},
                    "estimated_cost": {},
                    "codex_execution": {},
                    "verification": {},
                    "evaluation": {},
                }
            ),
            encoding="utf-8",
        )
        (run_directory / "run.json").write_text(
            json.dumps(
                {
                    "replay": {
                        "historical_model_request_seconds": 125,
                        "historical_wall_clock_seconds": 130,
                    }
                }
            ),
            encoding="utf-8",
        )

        result = bakeoff._command_report(argparse.Namespace(run_dir=run_directory))

        self.assertEqual(result["status"], "ok")
        rendered = (run_directory / "report.html").read_text(encoding="utf-8")
        self.assertEqual(rendered.count("Task execution time"), 2)
        self.assertIn("2m 5s", rendered)
        self.assertNotIn("Transcript wall-clock span", rendered)
        self.assertNotIn("2m 10s", rendered)

    def test_empty_non_git_result_uses_symmetric_bounded_capture(self) -> None:
        source = self.root / "source"
        source.mkdir()
        file_selection = bakeoff._file_selection().select_directory(
            source,
            confirmed=True,
        )
        result = self.root / "result"
        result.mkdir()
        (result / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (result / "dist").mkdir()
        (result / "dist" / "bundle.js").write_text("bundle\n", encoding="utf-8")
        (result / "raw.bin").write_bytes(b"\xff\xfeCodex\n")
        (result / "visible.txt").write_text("visible\n", encoding="utf-8")
        run = {
            "baseline": {
                "kind": "empty_directory",
                "repository": str(source),
            },
            "file_selection": file_selection,
            "model": "gpt-test",
        }
        native = {
            "worktree": str(result),
            "model": "gpt-test",
            "final_output": "done",
        }

        candidate, patch, changed = bakeoff._codex_candidate(run, native)

        self.assertEqual(changed, ("raw.bin", "visible.txt"))
        self.assertIn("GIT binary patch", patch)
        self.assertIn("+visible", patch)
        self.assertNotIn("TOKEN=secret", patch)
        self.assertNotIn("bundle", patch)
        self.assertEqual(candidate.diff, patch)

    def test_empty_non_git_result_rejects_root_symlink_swap(self) -> None:
        source = self.root / "source"
        source.mkdir()
        file_selection = bakeoff._file_selection().select_directory(
            source,
            confirmed=True,
        )
        result = self.root / "result"
        result.mkdir()
        moved = self.root / "moved-result"
        result.rename(moved)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
        os.symlink(outside, result)
        run = {
            "baseline": {
                "kind": "empty_directory",
                "repository": str(source),
            },
            "file_selection": file_selection,
            "model": "gpt-test",
        }
        native = {
            "worktree": str(result),
            "model": "gpt-test",
            "final_output": "done",
        }

        with self.assertRaisesRegex(
            bakeoff.BakeoffError,
            "result directory changed before copying",
        ):
            bakeoff._codex_candidate(run, native)

    def test_missing_created_file_stops_non_git_completion(self) -> None:
        source = self.root / "source"
        source.mkdir()
        created = source / "created.txt"
        created.write_text("Claude\n", encoding="utf-8")
        file_selection = bakeoff._file_selection().select_directory(
            source,
            created_by_claude=("created.txt",),
            confirmed=True,
        )
        created.unlink()
        run = {
            "replay": {
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
                "task_scope": "whole_thread",
            },
            "baseline": {
                "kind": "empty_directory",
                "repository": str(source),
            },
            "file_selection": file_selection,
            "model": "gpt-test",
        }

        with self.assertRaisesRegex(
            bakeoff.BakeoffError,
            "live classified Claude files could not be captured",
        ):
            bakeoff._historical_candidate(run)

    def test_failed_selected_git_capture_stops_completion(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        run = {
            "replay": {
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
                "task_scope": "whole_thread",
            },
            "baseline": {
                "kind": "git_commit",
                "repository": str(repository),
                "commit": "a" * 40,
            },
            "file_selection": {
                "source_kind": "git",
                "working_tree_state": "dirty",
                "claude_output_changes": [{"path": "changed.txt"}],
            },
            "model": "gpt-test",
        }

        with (
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_solution",
                return_value={"diff": ""},
            ),
            mock.patch.object(
                bakeoff._file_selection(),
                "build_git_candidate_patch",
                side_effect=RuntimeError("selected change moved"),
            ),
            self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "selected live Git changes could not be captured",
            ),
        ):
            bakeoff._historical_candidate(run)

    def test_unavailable_git_recovery_stops_completion(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        run = {
            "replay": {
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
                "task_scope": "whole_thread",
            },
            "baseline": {
                "kind": "git_commit",
                "repository": str(repository),
                "commit": "a" * 40,
            },
            "file_selection": {
                "source_kind": "git",
                "working_tree_state": "clean",
                "claude_output_changes": [],
            },
            "model": "gpt-test",
        }

        with (
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_solution",
                side_effect=RuntimeError("recovery unavailable"),
            ),
            self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "comparison cannot be completed",
            ),
        ):
            bakeoff._historical_candidate(run)

    def test_git_run_targets_a_managed_worktree(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            result = bakeoff._command_run(_args(self.root, kind="git_commit"))
        target = result["task_request"]["target"]
        self.assertEqual(target["type"], "project")
        self.assertEqual(target["environment"]["type"], "worktree")
        self.assertEqual(target["environment"]["startingState"]["branchName"], "a" * 40)

    def test_non_git_baseline_requires_file_classification(self) -> None:
        repository = self.root / "greenfield"
        repository.mkdir()
        (repository / "page.html").write_text("created\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=None,
        )
        replay = {"project_dir": str(repository)}
        inspected = {
            "kind": "unavailable",
            "repository": str(repository),
            "empty_directory_candidate": {
                "project_directory": str(repository.resolve()),
            },
        }
        with mock.patch.object(
            bakeoff._discovery(),
            "inspect_baseline",
            return_value=inspected,
        ):
            baseline = bakeoff._baseline(args, replay)
        self.assertEqual(baseline["kind"], "unclassified_directory")

        prepare_args = _args(self.root, kind="empty_directory")
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )
        self.assertEqual(classified["kind"], "unclassified_directory")
        self.assertFalse(file_selection["complete"])
        self.assertEqual(file_selection["unclassified_files"], ["page.html"])

        prepare_args.created_by_claude = ["page.html"]
        prepare_args.confirm_file_selection = True
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )
        self.assertTrue(file_selection["complete"])
        self.assertEqual(classified["kind"], "empty_directory")
        self.assertEqual(baseline["kind"], "unclassified_directory")

    def test_git_repository_without_commits_uses_non_git_classification(self) -> None:
        repository = self.root / "unborn"
        repository.mkdir()
        subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            check=True,
        )
        (repository / "created.txt").write_text("created\n", encoding="utf-8")
        args = argparse.Namespace(repo=None)
        replay = {"project_dir": str(repository)}
        inspected = {
            "kind": "unavailable",
            "repository": str(repository),
        }
        with mock.patch.object(
            bakeoff._discovery(),
            "inspect_baseline",
            return_value=inspected,
        ):
            baseline = bakeoff._baseline(args, replay)

        self.assertEqual(baseline["kind"], "unclassified_directory")
        self.assertEqual(baseline["source_kind"], "non_git")

        prepare_args = _args(self.root, kind="empty_directory")
        prepare_args.created_by_claude = ["created.txt"]
        prepare_args.confirm_file_selection = True
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )
        self.assertTrue(file_selection["complete"])
        self.assertEqual(classified["kind"], "empty_directory")

    def test_unborn_git_repository_scopes_classification_to_nested_project(self) -> None:
        repository = self.root / "unborn-root"
        project = repository / "claude"
        unrelated = repository / "unrelated"
        project.mkdir(parents=True)
        unrelated.mkdir()
        subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            check=True,
        )
        (project / "created.txt").write_text("created\n", encoding="utf-8")
        for index in range(bakeoff._file_selection().MAX_CANDIDATE_FILES):
            (unrelated / f"unrelated-{index}.txt").touch()
        args = argparse.Namespace(repo=None)
        replay = {
            "project_dir": str(project),
            "task_timestamp": "2026-01-01T00:00:00Z",
        }

        baseline = bakeoff._baseline(args, replay)

        self.assertEqual(baseline["repository"], str(project.resolve()))
        self.assertEqual(baseline["attribution_root"], str(project.resolve()))
        self.assertEqual(baseline["source_kind"], "non_git")

        prepare_args = _args(self.root, kind="empty_directory")
        prepare_args.created_by_claude = ["created.txt"]
        prepare_args.confirm_file_selection = True
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )

        self.assertEqual(
            [item["path"] for item in file_selection["candidates"]],
            ["created.txt"],
        )
        self.assertEqual(
            [item["path"] for item in file_selection["claude_output_files"]],
            ["created.txt"],
        )
        self.assertTrue(file_selection["complete"])
        self.assertEqual(classified["kind"], "empty_directory")

    def test_commit_backed_git_scopes_dirty_files_to_nested_project(self) -> None:
        repository = self.root / "committed-root"
        project = repository / "claude"
        unrelated = repository / "unrelated"
        project.mkdir(parents=True)
        unrelated.mkdir()
        subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Test"],
            check=True,
        )
        (project / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "claude/tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "--quiet", "-m", "baseline"],
            check=True,
        )
        (project / "created.txt").write_text("created\n", encoding="utf-8")
        for index in range(bakeoff._file_selection().MAX_CANDIDATE_FILES):
            (unrelated / f"unrelated-{index}.txt").touch()
        args = argparse.Namespace(repo=None)
        replay = {
            "project_dir": str(project),
            "task_timestamp": "2099-01-01T00:00:00Z",
        }

        baseline = bakeoff._baseline(args, replay)

        self.assertEqual(baseline["repository"], str(repository.resolve()))
        self.assertEqual(baseline["attribution_root"], str(project.resolve()))
        self.assertEqual(baseline["source_kind"], "git")

        prepare_args = _args(self.root, kind="git_commit")
        prepare_args.claude_output_file = ["claude/created.txt"]
        prepare_args.confirm_file_selection = True
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )

        self.assertEqual(classified["repository"], str(repository.resolve()))
        self.assertEqual(classified["attribution_root"], str(project.resolve()))
        self.assertEqual(file_selection["source_root"], str(repository.resolve()))
        self.assertEqual(file_selection["attribution_root"], str(project.resolve()))
        self.assertEqual(
            [item["path"] for item in file_selection["candidates"]],
            ["claude/created.txt"],
        )
        self.assertEqual(
            [item["path"] for item in file_selection["claude_output_changes"]],
            ["claude/created.txt"],
        )
        self.assertEqual(
            file_selection["claude_output_changes"][0]["affected_paths"],
            ["claude/created.txt"],
        )
        self.assertEqual(file_selection["unselected_changes"], [])
        self.assertTrue(file_selection["complete"])
        self.assertEqual(classified["kind"], "git_commit")

    def test_git_history_created_after_task_uses_non_git_classification(self) -> None:
        repository = self.root / "committed-after-task"
        repository.mkdir()
        subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            check=True,
        )
        (repository / "created.html").write_text("created\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "created.html"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--quiet",
                "-m",
                "post-task result",
            ],
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z",
            },
        )
        (repository / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        args = argparse.Namespace(repo=None)
        replay = {
            "project_dir": str(repository),
            "task_timestamp": "2026-01-01T00:00:00Z",
        }

        baseline = bakeoff._baseline(args, replay)

        self.assertEqual(baseline["kind"], "unclassified_directory")
        self.assertEqual(baseline["source_kind"], "non_git")
        self.assertEqual(
            baseline["post_task_git_history"]["timestamp"],
            "2026-01-02T00:00:00Z",
        )

        prepare_args = _args(self.root, kind="empty_directory")
        prepare_args.created_by_claude = ["created.html"]
        prepare_args.exclude_file = ["unrelated.txt"]
        prepare_args.confirm_file_selection = True
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )
        self.assertTrue(file_selection["complete"])
        self.assertTrue(file_selection["empty_starting_directory_confirmed"])
        self.assertEqual(classified["kind"], "empty_directory")

    def test_prepare_is_read_only_and_requires_final_approval(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        args = _args(self.root, kind="git_commit")
        args.approve = False
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "ready_for_approval")
        self.assertTrue(prepared["requires_approval"])
        self.assertNotIn("task_request", prepared)
        self.assertFalse((self.root / "runs").exists())

        with self.assertRaisesRegex(bakeoff.BakeoffError, "explicit user approval"):
            bakeoff._command_run(args)
        self.assertFalse((self.root / "runs").exists())

    def test_dirty_git_requires_current_change_selection(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        Path(replay["project_dir"], "local.txt").write_text(
            "working tree\n",
            encoding="utf-8",
        )
        args = _args(self.root, kind="git_commit")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
            with self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "File classification is still required",
            ):
                bakeoff._command_run(args)
        self.assertEqual(prepared["status"], "needs_user_input")
        self.assertEqual(
            prepared["questions"][0]["id"],
            "classify_git_working_tree",
        )
        self.assertEqual(
            prepared["questions"][0]["changes"][0]["path"],
            "local.txt",
        )

        args.claude_output_file = ["local.txt"]
        args.confirm_file_selection = True
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "ready_for_approval")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            started = bakeoff._command_run(args)
        recorded = json.loads(
            (Path(started["run_directory"]) / "run.json").read_text(encoding="utf-8")
        )
        self.assertTrue(recorded["file_selection"]["confirmed"])
        self.assertEqual(
            recorded["file_selection"]["claude_output_changes"][0]["path"],
            "local.txt",
        )

    def test_historical_git_state_does_not_replace_current_status(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        baseline["working_tree_state"] = "unknown"
        args = _args(self.root, kind="git_commit")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "ready_for_approval")
        self.assertEqual(prepared["questions"], [])
        self.assertEqual(
            prepared["configuration"]["baseline"]["historical_working_tree_state"],
            "unknown",
        )

    def test_confirmed_empty_non_git_baseline_uses_projectless_target(self) -> None:
        patches, replay, baseline, parity = self._patch_context("empty_directory")
        Path(replay["project_dir"], "created.txt").write_text(
            "claude output\n",
            encoding="utf-8",
        )
        Path(replay["project_dir"], "excluded.txt").write_text(
            "excluded\n",
            encoding="utf-8",
        )
        baseline["kind"] = "unclassified_directory"
        args = _args(self.root, kind="empty_directory")
        args.created_by_claude = ["created.txt"]
        args.exclude_file = ["excluded.txt"]
        args.confirm_file_selection = True
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "ready_for_approval")
        self.assertEqual(prepared["configuration"]["baseline"]["kind"], "empty_directory")
        self.assertTrue(
            prepared["configuration"]["user_decisions"]["empty_starting_directory_confirmed"]
        )
        self.assertFalse((self.root / "runs").exists())

        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            started = bakeoff._command_run(args)
        self.assertEqual(started["task_request"]["target"]["type"], "projectless")
        self.assertNotIn("registered_project_path", started["task_request"])

        recorded = json.loads(
            (Path(started["run_directory"]) / "run.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(recorded["baseline_materialization"])

    def test_non_git_question_stops_for_any_pre_existing_file(self) -> None:
        selection = {
            "source_kind": "non_git",
            "complete": False,
            "candidates": [{"path": "existing.txt"}],
            "unclassified_files": ["existing.txt"],
            "classifications": {},
        }
        question = bakeoff._selection_questions(selection)[0]
        self.assertIn("directory was empty before Claude", question["question"])
        self.assertIn("stop", question["question"])
        self.assertNotIn("existed_before_claude", question["classification_flags"])

    def test_cli_has_prepare_but_no_resume_or_plan_commands(self) -> None:
        parser = bakeoff.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        self.assertIn("prepare", choices)
        self.assertNotIn("resume", choices)
        self.assertNotIn("discover-tests", choices)
        self.assertNotIn("complete-review-plan", choices)
        self.assertIn("run", choices)
        self.assertIn("verify", choices)
        baseline_options = {
            option for action in choices["baseline"]._actions for option in action.option_strings
        }
        self.assertNotIn("--existed-before-claude", baseline_options)
        self.assertNotIn("--registered-baseline-project", baseline_options)
        self.assertNotIn("--registered-baseline-project-id", baseline_options)
        self.assertNotIn("--modified-by-claude", baseline_options)
        self.assertNotIn("--before-file", baseline_options)
        collect_options = {
            option
            for action in choices["collect-native-result"]._actions
            for option in action.option_strings
        }
        complete_evaluation_options = {
            option
            for action in choices["complete-evaluation"]._actions
            for option in action.option_strings
        }
        self.assertIn("--normalization-for", collect_options)
        self.assertIn("--normalized-result", complete_evaluation_options)

    def test_invalid_review_requests_one_schema_normalization_task(self) -> None:
        run_directory = self.root / "run"
        run_directory.mkdir()
        bakeoff._write_json(run_directory / "run.json", {})
        bakeoff._write_json(
            run_directory / "report.json",
            {"status": "completed", "original_request": "Build the thing"},
        )
        native_results = run_directory / "reviews" / "results.json"
        bakeoff._write_json(
            native_results,
            {
                "results": [
                    {
                        "evaluator": "codex",
                        "model": "gpt-test",
                        "final_output": _review_ballot(field="choice"),
                    }
                ]
            },
        )

        result = bakeoff._command_complete_evaluation(
            argparse.Namespace(
                run_dir=run_directory,
                native_results=native_results,
                normalized_result=None,
            )
        )

        self.assertEqual(result["status"], "native_task_required")
        self.assertEqual(result["purpose"], "review_normalization")
        self.assertEqual(len(result["task_requests"]), 1)
        request = result["task_requests"][0]
        self.assertEqual(request["normalization_for"], "codex")
        self.assertEqual(request["model"], "gpt-test")
        self.assertNotIn("candidate_paths", request)
        recorded = json.loads((run_directory / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["status"], "awaiting_normalization")
        self.assertEqual(
            recorded["reviews"][0]["normalization"]["status"],
            "awaiting_native_task",
        )

    def test_normalized_review_is_validated_scored_and_recorded(self) -> None:
        run_directory = self.root / "run"
        run_directory.mkdir()
        bakeoff._write_json(run_directory / "run.json", {})
        bakeoff._write_json(
            run_directory / "report.json",
            {"status": "completed", "original_request": "Build the thing"},
        )
        native_results = run_directory / "reviews" / "results.json"
        bakeoff._write_json(
            native_results,
            {
                "results": [
                    {
                        "evaluator": "codex",
                        "model": "gpt-test",
                        "final_output": _review_ballot(field="choice"),
                    }
                ]
            },
        )
        normalized_result = run_directory / "reviews" / "normalized.json"
        bakeoff._write_json(
            normalized_result,
            {
                "normalization_for": "codex",
                "model": "gpt-test",
                "thread_id": "normalizer-thread",
                "final_output": _review_ballot(),
            },
        )

        result = bakeoff._command_complete_evaluation(
            argparse.Namespace(
                run_dir=run_directory,
                native_results=native_results,
                normalized_result=[normalized_result],
            )
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evaluation"]["totals"], {"A": 1, "B": 2})
        review = result["evaluation"]["reviews"][0]
        self.assertIn('"choice": "B"', review["raw_ballot"])
        self.assertEqual(
            review["normalized_ballot"]["categories"]["task_completion"]["outcome"],
            "B",
        )
        self.assertEqual(review["normalization"]["status"], "completed")
        self.assertEqual(
            review["normalization"]["thread_id"],
            "normalizer-thread",
        )

    def test_invalid_normalization_does_not_request_another_task(self) -> None:
        run_directory = self.root / "run"
        run_directory.mkdir()
        bakeoff._write_json(run_directory / "run.json", {})
        bakeoff._write_json(
            run_directory / "report.json",
            {"status": "completed", "original_request": "Build the thing"},
        )
        native_results = run_directory / "reviews" / "results.json"
        bakeoff._write_json(
            native_results,
            {
                "results": [
                    {
                        "evaluator": "codex",
                        "model": "gpt-test",
                        "final_output": _review_ballot(field="choice"),
                    }
                ]
            },
        )
        normalized_result = run_directory / "reviews" / "normalized.json"
        bakeoff._write_json(
            normalized_result,
            {
                "normalization_for": "codex",
                "model": "gpt-test",
                "thread_id": "normalizer-thread",
                "final_output": '{"normalization_error":"missing decision"}',
            },
        )

        result = bakeoff._command_complete_evaluation(
            argparse.Namespace(
                run_dir=run_directory,
                native_results=native_results,
                normalized_result=[normalized_result],
            )
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("task_requests", result)
        self.assertEqual(
            result["evaluation"]["all_results"][0]["normalization"]["status"],
            "failed",
        )

    def test_legacy_classified_result_must_come_from_registered_project(self) -> None:
        expected = self.root / "registered"
        expected.mkdir()
        wrong = self.root / "wrong"
        wrong.mkdir()
        run = {
            "baseline": {
                "kind": "classified_directory",
                "registered_baseline_project": str(expected),
            },
            "file_selection": {},
            "model": "gpt-test",
        }
        native = {
            "worktree": str(wrong),
            "model": "gpt-test",
            "final_output": "done",
        }

        with self.assertRaisesRegex(
            bakeoff.BakeoffError,
            "does not match the registered",
        ):
            bakeoff._codex_candidate(run, native)


if __name__ == "__main__":
    unittest.main()
