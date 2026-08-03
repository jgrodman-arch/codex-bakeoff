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
        beginning_kind=None,
        ending_kind=None,
        baseline_commit=None,
        ending_commit=None,
        claude_output_file=None,
        created_by_claude=None,
        exclude_file=None,
        confirm_file_selection=False,
        confirm_empty_beginning=False,
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

    def test_manual_model_is_accepted_when_catalog_discovery_is_unavailable(self) -> None:
        self.assertEqual(
            bakeoff._selected_model("gpt-manually-reviewed", self.root / "missing.json"),
            "gpt-manually-reviewed",
        )
        with self.assertRaisesRegex(bakeoff.BakeoffError, "not locally available"):
            bakeoff._selected_model("gpt-manually-reviewed", self.root / "models.json")

    def test_model_catalog_includes_every_available_slug_and_defaults_to_sol(self) -> None:
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "lanturn-1000",
                            "visibility": "list",
                            "supported_in_api": True,
                            "is_default": True,
                        },
                        {
                            "slug": "gpt-5.6-sol",
                            "visibility": "list",
                            "supported_in_api": True,
                        },
                        {
                            "slug": "model-experimental",
                            "visibility": "list",
                            "supported_in_api": True,
                        },
                        {
                            "slug": "hidden-model",
                            "visibility": "hide",
                            "supported_in_api": True,
                        },
                        {
                            "slug": "unsupported-model",
                            "visibility": "list",
                            "supported_in_api": False,
                        },
                        {
                            "slug": "deprecated-model",
                            "visibility": "list",
                            "supported_in_api": True,
                            "upgrade": {"model": "gpt-5.6-sol"},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        catalog = bakeoff.discover_codex_models(catalog_path)

        self.assertEqual(
            [item["id"] for item in catalog["options"]],
            ["lanturn-1000", "gpt-5.6-sol", "model-experimental"],
        )
        self.assertEqual(
            [item["id"] for item in catalog["options"] if item["recommended"]],
            ["gpt-5.6-sol"],
        )
        self.assertEqual(
            bakeoff._selected_model("lanturn-1000", catalog_path),
            "lanturn-1000",
        )

        fallback = bakeoff.discover_codex_models(self.root / "models.json")
        self.assertTrue(fallback["options"][0]["recommended"])

    def _git_repository(self, name: str) -> Path:
        repository = self.root / name
        repository.mkdir()
        subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            check=True,
        )
        (repository / "README.md").write_text(f"{name}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "README.md"],
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
                "baseline",
            ],
            check=True,
        )
        return repository

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
            "source_path": str(self.root / "rollout.jsonl"),
            "message_uuid": "message-1",
        }
        baseline = {
            "kind": kind,
            "repository": str(repository),
            "commit": "a" * 40 if kind == "git_commit" else None,
            "beginning_kind": "git" if kind == "git_commit" else "non_git",
            "ending_kind": "git" if kind == "git_commit" else "non_git",
            "confidence": "verified" if kind == "git_commit" else "user_confirmed",
            "working_tree_state": ("verified_clean" if kind == "git_commit" else "unknown"),
            "source_kind": "git" if kind == "git_commit" else "non_git",
        }
        parity = {"items": []}
        historical_candidate = bakeoff._execution().CandidateSolution(
            provider="claude",
            diff="diff --git a/app.py b/app.py\n",
            model="claude-test",
            final_response="done",
        )
        historical_recovery = {
            "provenance": "test_fixture",
            "diff": historical_candidate.diff,
            "changed_files": ["app.py"],
            "limitations": [],
        }
        return (
            mock.patch.multiple(
                bakeoff,
                _selected_replay=mock.DEFAULT,
                _baseline=mock.DEFAULT,
                _capabilities=mock.DEFAULT,
                _historical_candidate=mock.Mock(
                    return_value=(
                        historical_candidate,
                        historical_recovery,
                        historical_candidate.final_response,
                    )
                ),
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
        self.assertEqual(
            {question["id"] for question in prepared["questions"]},
            {"classify_non_git_files", "confirm_empty_beginning"},
        )

        args.confirm_file_selection = True
        args.confirm_empty_beginning = True
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
        self.assertIn("Task prompt:\nbuild the thing", request["prompt"])
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

    def test_empty_git_recovery_stops_completion(self) -> None:
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
                return_value={"diff": ""},
            ),
            self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "comparison cannot be completed",
            ),
        ):
            bakeoff._historical_candidate(run)

    def test_selected_dirty_changes_can_supplement_an_empty_commit_range(self) -> None:
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
        captured = "diff --git a/changed.txt b/changed.txt\n"

        with (
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_solution",
                return_value={"diff": "", "provenance": "user_reviewed_git_commit"},
            ),
            mock.patch.object(
                bakeoff._file_selection(),
                "build_git_candidate_patch",
                return_value=(captured, ["changed.txt"]),
            ),
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_final_response",
                return_value="done",
            ),
        ):
            candidate, recovery, _ = bakeoff._historical_candidate(run)

        self.assertEqual(candidate.diff, captured)
        self.assertEqual(recovery["changed_files"], ["changed.txt"])

    def test_git_end_defaults_to_beginning_without_an_attributed_commit(self) -> None:
        beginning = "a" * 40
        baseline = {
            "kind": "git_commit",
            "repository": str(self.root),
            "commit": beginning,
            "beginning_kind": "git",
            "ending_kind": "git",
            "source_kind": "git",
        }

        with mock.patch.object(
            bakeoff._discovery(),
            "recover_historical_solution",
            side_effect=RuntimeError("no attributed commit was recoverable"),
        ):
            reviewed = bakeoff._with_historical_ending_commit({}, baseline, "")

        self.assertEqual(reviewed["ending_commit"], beginning)
        self.assertEqual(reviewed["ending_commit_confidence"], "inferred")
        self.assertTrue(reviewed["ending_commit_defaulted_to_beginning"])
        self.assertFalse(reviewed["ending_commit_reviewed_override"])
        self.assertIn(
            "no attributed commit was recoverable",
            reviewed["ending_commit_inference_error"],
        )

    def test_defaulted_git_end_includes_only_selected_untracked_files(self) -> None:
        repository = self._git_repository("unchanged-head")
        beginning = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        transcript = self.root / "unchanged-head.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "message-1",
                    "message": {"role": "user", "content": "Add hello world"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        replay = {
            "source_path": str(transcript),
            "message_uuid": "message-1",
            "task_scope": "whole_thread",
        }
        baseline = bakeoff._with_historical_ending_commit(
            replay,
            {
                "kind": "git_commit",
                "repository": str(repository),
                "commit": beginning,
                "beginning_kind": "git",
                "ending_kind": "git",
                "source_kind": "git",
            },
            "",
        )
        selected_paths = ("hello.py", "hello_test.py", "README.md")
        for path in selected_paths:
            (repository / path).write_text(f"{path}\n", encoding="utf-8")
        selection = bakeoff._file_selection().select_git(
            repository,
            claude_output_files=selected_paths,
            confirmed=True,
        )

        candidate, recovery, _ = bakeoff._historical_candidate(
            {"replay": replay, "baseline": baseline, "file_selection": selection}
        )

        self.assertEqual(baseline["ending_commit"], beginning)
        self.assertEqual(set(recovery["changed_files"]), set(selected_paths))
        self.assertEqual(recovery["attributed_dirty_file_count"], 3)
        self.assertFalse(recovery["no_changes_attributed"])
        for path in selected_paths:
            self.assertIn(f"b/{path}", candidate.diff)

    def test_reviewed_zero_dirty_file_attribution_allows_no_change(self) -> None:
        repository = self._git_repository("reviewed-no-change")
        beginning = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (repository / "unrelated.txt").write_text("local only\n", encoding="utf-8")
        selection = bakeoff._file_selection().select_git(repository, confirmed=True)
        transcript = self.root / "reviewed-no-change.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "message-1",
                    "message": {"role": "user", "content": "Inspect the project"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        replay = {
            "source_path": str(transcript),
            "message_uuid": "message-1",
            "task_scope": "whole_thread",
        }
        baseline = {
            "kind": "git_commit",
            "repository": str(repository),
            "commit": beginning,
            "ending_commit": beginning,
            "beginning_kind": "git",
            "ending_kind": "git",
            "source_kind": "git",
        }

        candidate, recovery, _ = bakeoff._historical_candidate(
            {"replay": replay, "baseline": baseline, "file_selection": selection}
        )

        self.assertEqual(candidate.diff, "")
        self.assertEqual(recovery["changed_files"], [])
        self.assertEqual(recovery["attributed_dirty_file_count"], 0)
        self.assertTrue(recovery["no_changes_attributed"])
        self.assertTrue(recovery["file_selection"]["confirmed"])
        self.assertEqual(recovery["file_selection"]["claude_output_changes"], [])
        self.assertEqual(
            [item["path"] for item in recovery["file_selection"]["unselected_changes"]],
            ["unrelated.txt"],
        )

    def test_unreviewed_zero_dirty_file_attribution_remains_blocked(self) -> None:
        repository = self._git_repository("unreviewed-no-change")
        beginning = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (repository / "unrelated.txt").write_text("local only\n", encoding="utf-8")
        selection = bakeoff._file_selection().select_git(repository)
        run = {
            "replay": {
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
            },
            "baseline": {
                "kind": "git_commit",
                "repository": str(repository),
                "commit": beginning,
                "ending_commit": beginning,
                "beginning_kind": "git",
                "ending_kind": "git",
            },
            "file_selection": selection,
        }

        with (
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_solution",
                return_value={"commit": beginning, "diff": "", "changed_files": []},
            ),
            self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "No attributable historical Claude patch could be captured",
            ),
        ):
            bakeoff._historical_candidate(run)

    def test_unchanged_inferred_ending_commit_is_not_recorded_as_an_override(self) -> None:
        inferred_commit = "b" * 40
        baseline = {
            "kind": "git_commit",
            "repository": str(self.root),
            "commit": "a" * 40,
        }
        recovery = {
            "commit": inferred_commit,
            "diff": "diff --git a/app.py b/app.py\n",
        }

        with mock.patch.object(
            bakeoff._discovery(),
            "recover_historical_solution",
            return_value=recovery,
        ) as recover:
            reviewed = bakeoff._with_historical_ending_commit(
                {},
                baseline,
                inferred_commit,
            )

        self.assertEqual(recover.call_count, 1)
        self.assertEqual(reviewed["ending_commit_confidence"], "inferred")
        self.assertFalse(reviewed["ending_commit_reviewed_override"])

    def test_cross_repository_changes_resolve_one_effective_repository(self) -> None:
        original = self._git_repository("original")
        effective = self._git_repository("effective")
        changed = effective / "src" / "app.py"
        changed.parent.mkdir()
        changed.write_text("print('result')\n", encoding="utf-8")
        replay = {
            "project_dir": str(original),
            "historical_changed_files": [str(changed)],
        }

        resolved, resolution, blockers = bakeoff._resolved_replay_repository(
            argparse.Namespace(repo=None),
            replay,
        )

        self.assertEqual(blockers, [])
        self.assertEqual(resolved["project_dir"], str(effective.resolve()))
        self.assertEqual(resolved["original_project_dir"], str(original.resolve()))
        self.assertEqual(resolution["source"], "historical_changed_files")
        self.assertEqual(
            resolution["effective_project_dir"],
            str(effective.resolve()),
        )
        _, explicit_resolution, explicit_blockers = bakeoff._resolved_replay_repository(
            argparse.Namespace(repo=effective),
            replay,
        )
        self.assertEqual(explicit_blockers, [])
        self.assertEqual(explicit_resolution["source"], "explicit_repo")

    def test_repository_resolution_preserves_original_nested_scope(self) -> None:
        repository = self._git_repository("monorepo")
        project = repository / "packages" / "selected"
        project.mkdir(parents=True)
        changed = project / "app.py"
        changed.write_text("print('result')\n", encoding="utf-8")

        resolved, resolution, blockers = bakeoff._resolved_replay_repository(
            argparse.Namespace(repo=None),
            {
                "project_dir": str(project),
                "historical_changed_files": [str(changed)],
            },
        )

        self.assertEqual(blockers, [])
        self.assertEqual(resolved["project_dir"], str(project.resolve()))
        self.assertEqual(resolution["source"], "original_project_dir")

    def test_repository_resolution_blocks_mixed_and_excluded_changes(self) -> None:
        first = self._git_repository("first")
        second = self._git_repository("second")
        first_changed = first / "first.py"
        second_changed = second / "second.py"
        first_changed.write_text("first\n", encoding="utf-8")
        second_changed.write_text("second\n", encoding="utf-8")

        _, _, mixed_blockers = bakeoff._resolved_replay_repository(
            argparse.Namespace(repo=None),
            {
                "project_dir": str(first),
                "historical_changed_files": [
                    str(first_changed),
                    str(second_changed),
                ],
            },
        )
        _, _, explicit_blockers = bakeoff._resolved_replay_repository(
            argparse.Namespace(repo=first),
            {
                "project_dir": str(first),
                "historical_changed_files": [str(second_changed)],
            },
        )

        self.assertIn("multiple Git repositories", mixed_blockers[0])
        self.assertIn(
            "The selected repository excludes task-attributed Claude changes.",
            explicit_blockers,
        )
        _, reviewed_resolution, reviewed_blockers = bakeoff._resolved_replay_repository(
            argparse.Namespace(
                repo=first,
                confirm_repository_selection=True,
            ),
            {
                "project_dir": str(first),
                "historical_changed_files": [str(first_changed), str(second_changed)],
            },
        )
        self.assertEqual(reviewed_blockers, [])
        self.assertTrue(reviewed_resolution["user_confirmed"])
        self.assertTrue(reviewed_resolution["overridden_blocking_reasons"])

        args = _args(self.root, kind="git_commit")
        replay = {
            "request": "build the thing",
            "project_dir": str(first),
            "historical_changed_files": [str(first_changed), str(second_changed)],
        }
        baseline = {
            "kind": "git_commit",
            "repository": str(first),
            "attribution_root": str(first),
            "commit": "a" * 40,
            "source_kind": "git",
        }
        with (
            mock.patch.object(bakeoff, "_selected_replay", return_value=replay),
            mock.patch.object(bakeoff, "_baseline", return_value=baseline),
            mock.patch.object(bakeoff, "_capabilities", return_value={"items": []}),
        ):
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "blocked")
        self.assertIn("multiple Git repositories", prepared["blocking_reasons"][0])

    def test_historical_recovery_uses_the_baseline_repository(self) -> None:
        original = self._git_repository("thread-cwd")
        effective = self._git_repository("result-repository")
        run = {
            "replay": {
                "project_dir": str(original),
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
            },
            "baseline": {
                "kind": "git_commit",
                "repository": str(effective),
                "commit": "a" * 40,
            },
            "file_selection": {"source_kind": "git"},
            "model": "gpt-test",
        }
        recovery = {
            "provenance": "attributed_git_commit",
            "diff": "diff --git a/app.py b/app.py\n",
            "limitations": [],
        }

        with (
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_solution",
                return_value=recovery,
            ) as recover,
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_final_response",
                return_value="done",
            ),
        ):
            bakeoff._historical_candidate(run)

        recovery_replay = recover.call_args.args[0]
        self.assertEqual(recovery_replay["project_dir"], str(effective))
        self.assertEqual(recovery_replay["project_dirs"], [str(effective)])

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
        prepare_args.confirm_empty_beginning = True
        classified, file_selection = bakeoff._classified_baseline(
            prepare_args,
            baseline,
        )
        self.assertTrue(file_selection["complete"])
        self.assertEqual(classified["kind"], "empty_directory")
        self.assertEqual(baseline["kind"], "unclassified_directory")

    def test_reviewed_git_commit_recovers_from_failed_baseline_discovery(self) -> None:
        repository = self._git_repository("reviewed-baseline")
        commit = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        args = _args(self.root, kind="git_commit")
        args.repo = repository
        args.beginning_kind = "git"
        args.ending_kind = "git"
        args.baseline_commit = commit
        args.ending_commit = commit

        with (
            mock.patch.object(
                bakeoff._discovery(),
                "inspect_baseline",
                side_effect=RuntimeError("ambiguous baseline"),
            ),
            mock.patch.object(
                bakeoff._discovery(),
                "recover_historical_solution",
                return_value={"commit": commit, "diff": ""},
            ),
        ):
            baseline = bakeoff._baseline(args, {"project_dir": str(repository)})

        self.assertEqual(baseline["kind"], "git_commit")
        self.assertEqual(baseline["commit"], commit)
        self.assertEqual(baseline["confidence"], "user_confirmed")
        self.assertTrue(baseline["reviewed_override"])

        args.baseline_commit = "deadbee"
        with (
            mock.patch.object(
                bakeoff._discovery(),
                "inspect_baseline",
                side_effect=RuntimeError("ambiguous baseline"),
            ),
            self.assertRaisesRegex(bakeoff.BakeoffError, "commit is unavailable"),
        ):
            bakeoff._baseline(args, {"project_dir": str(repository)})

    def test_git_beginning_rejects_non_git_end(self) -> None:
        args = _args(self.root, kind="git_commit")
        args.beginning_kind = "git"
        args.ending_kind = "non_git"
        args.baseline_commit = "a" * 40

        with self.assertRaisesRegex(
            bakeoff.BakeoffError,
            "Git beginning state requires a Git end state",
        ):
            bakeoff._baseline(args, {})

    def test_reviewed_non_git_beginning_to_git_end_is_runnable(self) -> None:
        repository = self._git_repository("non-git-to-git")
        ending_commit = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        transcript = self.root / "non-git-to-git.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "message-1",
                    "message": {"role": "user", "content": "Build it"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        replay = {
            "project_dir": str(repository),
            "source_path": str(transcript),
            "message_uuid": "message-1",
        }
        args = _args(self.root, kind="empty_directory")
        args.repo = repository
        args.beginning_kind = "non_git"
        args.ending_kind = "git"
        args.ending_commit = ending_commit
        args.confirm_empty_beginning = True

        baseline = bakeoff._baseline(args, replay)
        classified, file_selection = bakeoff._classified_baseline(args, baseline)

        self.assertEqual(classified["kind"], "empty_directory")
        self.assertEqual(classified["beginning_kind"], "non_git")
        self.assertEqual(classified["ending_kind"], "git")
        self.assertEqual(classified["ending_commit"], ending_commit)
        self.assertEqual(file_selection["source_kind"], "git")
        self.assertTrue(file_selection["complete"])
        target = bakeoff._target_for_baseline(
            classified,
            run_directory=self.root / "run-non-git-to-git",
        )
        self.assertEqual(target["type"], "projectless")

    def test_reviewed_request_recovers_from_failed_transcript_discovery(self) -> None:
        repository = self.root / "manual-request"
        repository.mkdir()
        args = _args(self.root, kind="empty_directory")
        args.repo = repository
        args.request = "Build the manually reviewed task."
        session = {
            "imported_thread_id": "thread-1",
            "session_id": "session-1",
            "project_dir": str(repository),
        }

        with (
            mock.patch.object(bakeoff, "_selected_session", return_value=session),
            mock.patch.object(
                bakeoff._discovery(),
                "build_thread_task",
                side_effect=RuntimeError("ambiguous transcript"),
            ),
        ):
            replay = bakeoff._selected_replay(args)

        self.assertEqual(replay["request"], args.request)
        self.assertEqual(replay["project_dir"], str(repository))

    def test_reviewed_transcript_identity_recovers_ambiguous_thread_discovery(self) -> None:
        repository = self.root / "manual-transcript"
        repository.mkdir()
        transcript = self.root / "manual-transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "message-1",
                    "sessionId": "session-1",
                    "cwd": str(repository),
                    "timestamp": "2026-07-31T12:00:00Z",
                    "message": {"role": "user", "content": "Original request"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        args = _args(self.root, kind="empty_directory")
        args.repo = repository
        args.source_path = transcript
        args.message_uuid = "message-1"
        args.request = "Build the manually reviewed task."
        session = {
            "imported_thread_id": "thread-1",
            "session_id": "session-1",
            "source_path": str(transcript),
            "project_dir": str(repository),
        }

        with mock.patch.object(bakeoff, "_selected_session", return_value=session):
            replay = bakeoff._selected_replay(args)

        self.assertEqual(replay["source_path"], str(transcript))
        self.assertEqual(replay["message_uuid"], "message-1")
        self.assertEqual(replay["request"], args.request)
        self.assertFalse(replay["review_decisions"]["transcript_overridden"])
        self.assertTrue(replay["review_decisions"]["request_overridden"])

        with mock.patch.object(
            bakeoff,
            "_selected_session",
            side_effect=bakeoff.BakeoffError("ledger unavailable"),
        ):
            replay_without_ledger = bakeoff._selected_replay(args)
        self.assertEqual(replay_without_ledger["source_path"], str(transcript))
        self.assertEqual(replay_without_ledger["message_uuid"], "message-1")
        self.assertEqual(replay_without_ledger["request"], args.request)
        self.assertTrue(
            replay_without_ledger["review_decisions"]["transcript_overridden"]
        )

        args.message_uuid = "missing-message"
        with (
            mock.patch.object(bakeoff, "_selected_session", return_value=session),
            self.assertRaisesRegex(bakeoff.BakeoffError, "not in the imported transcript"),
        ):
            bakeoff._selected_replay(args)

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
        prepare_args.confirm_empty_beginning = True
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
        prepare_args.confirm_empty_beginning = True
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

        ending_commit = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        recovered = {"commit": ending_commit, "diff": "diff --git a/created.html b/created.html\n"}
        with mock.patch.object(
            bakeoff._discovery(),
            "recover_historical_solution",
            return_value=recovered,
        ):
            baseline = bakeoff._baseline(args, replay)

        self.assertEqual(baseline["kind"], "unclassified_directory")
        self.assertEqual(baseline["beginning_kind"], "non_git")
        self.assertEqual(baseline["ending_kind"], "git")
        self.assertEqual(baseline["source_kind"], "git")
        self.assertEqual(
            baseline["post_task_git_history"]["timestamp"],
            "2026-01-02T00:00:00Z",
        )

        reviewed_args = _args(self.root, kind="empty_directory")
        reviewed_args.repo = repository
        reviewed_args.beginning_kind = "non_git"
        reviewed_args.ending_kind = "git"
        reviewed_args.ending_commit = ending_commit
        with mock.patch.object(
            bakeoff._discovery(),
            "recover_historical_solution",
            return_value=recovered,
        ):
            reviewed_baseline = bakeoff._baseline(reviewed_args, replay)
        self.assertEqual(reviewed_baseline["kind"], "unclassified_directory")
        self.assertEqual(reviewed_baseline["source_kind"], "git")
        self.assertIn("post_task_git_history", reviewed_baseline)
        self.assertFalse(reviewed_baseline["reviewed_override"])

        prepare_args = _args(self.root, kind="empty_directory")
        prepare_args.confirm_file_selection = True
        prepare_args.confirm_empty_beginning = True
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
        self.assertEqual(
            prepared["prepared_configuration_sha256"],
            bakeoff._canonical_json_sha256(prepared["configuration"]),
        )
        self.assertFalse((self.root / "runs").exists())

        with self.assertRaisesRegex(bakeoff.BakeoffError, "explicit user approval"):
            bakeoff._command_run(args)
        self.assertFalse((self.root / "runs").exists())

    def test_prepare_retains_capability_discovery_failure_as_a_limitation(self) -> None:
        patches, replay, baseline, _ = self._patch_context("git_commit")
        args = _args(self.root, kind="git_commit")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].side_effect = bakeoff.BakeoffError(
                "capability discovery unavailable"
            )
            prepared = bakeoff._command_prepare(args)

        self.assertEqual(prepared["status"], "ready_for_approval")
        self.assertEqual(
            prepared["capabilities"]["limitations"],
            ["capability discovery unavailable"],
        )

    def test_optional_capability_import_does_not_block_prepare(self) -> None:
        patches, replay, baseline, _ = self._patch_context("git_commit")
        parity = {
            "items": [{"name": "missing-skill", "status": "not_available"}],
            "unavailable_capabilities": [
                {"name": "missing-skill", "status": "not_available"}
            ],
            "resolution_actions": [
                {
                    "status": "optional",
                    "action": "import_from_claude",
                    "remediation_action": (
                        "Go to Settings > Import to review and import this Claude skill."
                    ),
                }
            ],
        }
        args = _args(self.root, kind="git_commit")
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)

        self.assertEqual(prepared["status"], "ready_for_approval")
        self.assertTrue(prepared["can_run"])
        self.assertEqual(prepared["blocking_reasons"], [])
        self.assertEqual(prepared["capabilities"], parity)

    def test_prepare_blocks_when_the_historical_patch_is_unavailable(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        replay.update(
            {
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
            }
        )
        args = _args(self.root, kind="git_commit")
        with (
            patches as values,
            mock.patch.object(
                bakeoff,
                "_historical_candidate",
                side_effect=bakeoff.BakeoffError(
                    "No attributable historical Claude patch could be captured; "
                    "the comparison cannot be completed."
                ),
            ),
        ):
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)

        self.assertEqual(prepared["status"], "blocked")
        self.assertIn(
            "No attributable historical Claude patch could be captured",
            prepared["blocking_reasons"][0],
        )
        self.assertFalse((self.root / "runs").exists())

    def test_prepare_blocks_when_transcript_identity_is_missing(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        replay.pop("source_path")
        replay.pop("message_uuid")
        args = _args(self.root, kind="git_commit")
        capture_historical = bakeoff._historical_candidate
        with (
            patches as values,
            mock.patch.object(
                bakeoff,
                "_historical_candidate",
                side_effect=capture_historical,
            ),
        ):
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "blocked")
        self.assertIn(
            "source transcript path and original user-message UUID",
            prepared["blocking_reasons"][0],
        )
        self.assertFalse((self.root / "runs").exists())

    def test_run_requires_a_frozen_candidate_before_allocating(self) -> None:
        with (
            mock.patch.object(
                bakeoff,
                "_prepare_context",
                return_value={
                    "status": "ready_for_approval",
                    "questions": [],
                    "historical_candidate": None,
                },
            ),
            mock.patch.object(bakeoff, "_new_run_directory") as allocate,
            self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "was not frozen",
            ),
        ):
            bakeoff._command_run(_args(self.root, kind="git_commit"))
        allocate.assert_not_called()

    def test_run_rejects_a_changed_prepared_candidate_before_allocating(self) -> None:
        candidate = bakeoff._execution().CandidateSolution(
            provider="claude",
            diff="prepared diff",
            model="claude-test",
        )
        frozen = bakeoff._serialize_historical_candidate(
            candidate,
            {"provenance": "test", "limitations": []},
            "",
        )
        args = _args(self.root, kind="git_commit")
        args.expected_historical_result_sha256 = "0" * 64
        with (
            mock.patch.object(
                bakeoff,
                "_prepare_context",
                return_value={
                    "status": "ready_for_approval",
                    "questions": [],
                    "historical_candidate": frozen,
                },
            ),
            mock.patch.object(bakeoff, "_new_run_directory") as allocate,
            self.assertRaisesRegex(
                bakeoff.BakeoffError,
                "changed after preparation",
            ),
        ):
            bakeoff._command_run(args)
        allocate.assert_not_called()

    def test_run_rejects_an_invalid_or_changed_configuration_before_allocating(self) -> None:
        candidate = bakeoff._execution().CandidateSolution(
            provider="claude",
            diff="prepared diff",
            model="claude-test",
        )
        frozen = bakeoff._serialize_historical_candidate(
            candidate,
            {"provenance": "test", "limitations": []},
            "",
        )
        prepared = {
            "status": "ready_for_approval",
            "questions": [],
            "configuration": {"model": "gpt-test"},
            "historical_candidate": frozen,
        }
        for expected_digest in ("not-a-digest", "0" * 64):
            with self.subTest(expected_digest=expected_digest):
                args = _args(self.root, kind="git_commit")
                args.expected_prepared_configuration_sha256 = expected_digest
                with (
                    mock.patch.object(
                        bakeoff,
                        "_prepare_context",
                        return_value=prepared,
                    ),
                    mock.patch.object(bakeoff, "_new_run_directory") as allocate,
                    self.assertRaisesRegex(
                        bakeoff.BakeoffError,
                        "run `prepare` again",
                    ),
                ):
                    bakeoff._command_run(args)
                allocate.assert_not_called()

    def test_run_freezes_the_historical_candidate_for_completion(self) -> None:
        patches, replay, baseline, parity = self._patch_context("git_commit")
        replay.update(
            {
                "source_path": str(self.root / "rollout.jsonl"),
                "message_uuid": "message-1",
            }
        )
        candidate = bakeoff._execution().CandidateSolution(
            provider="claude",
            diff="diff --git a/app.py b/app.py\n",
            model="claude-test",
            final_response="historical response",
        )
        recovery = {
            "provenance": "attributed_git_commit",
            "diff": candidate.diff,
            "changed_files": ["app.py"],
            "limitations": [],
        }
        args = _args(self.root, kind="git_commit")
        with (
            patches as values,
            mock.patch.object(
                bakeoff,
                "_historical_candidate",
                return_value=(candidate, recovery, candidate.final_response),
            ),
        ):
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertRegex(prepared["historical_result_sha256"], r"\A[a-f0-9]{64}\Z")
        args.expected_prepared_configuration_sha256 = prepared[
            "prepared_configuration_sha256"
        ]
        args.expected_historical_result_sha256 = prepared["historical_result_sha256"]
        with (
            patches as values,
            mock.patch.object(
                bakeoff,
                "_historical_candidate",
                return_value=(candidate, recovery, candidate.final_response),
            ),
        ):
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            started = bakeoff._command_run(args)

        recorded = json.loads(
            (Path(started["run_directory"]) / "run.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("historical_candidate", recorded)
        metadata = recorded["historical_result"]
        self.assertEqual(metadata["sha256"], prepared["historical_result_sha256"])
        self.assertEqual(
            metadata,
            {
                "schema_version": 1,
                "path": "historical-result.json",
                "sha256": metadata["sha256"],
            },
        )
        artifact_path = Path(started["run_directory"]) / metadata["path"]
        serialized = artifact_path.read_bytes()
        self.assertEqual(
            metadata["sha256"],
            bakeoff.hashlib.sha256(serialized).hexdigest(),
        )
        frozen = json.loads(serialized)
        self.assertEqual(frozen["candidate"]["diff"], candidate.diff)
        self.assertEqual(frozen["recovery"]["changed_files"], ["app.py"])
        Path(replay["source_path"]).write_text("source changed\n", encoding="utf-8")
        native_path = self.root / "native-result.json"
        bakeoff._write_json(native_path, {"model": "gpt-test"})
        codex_candidate = bakeoff._execution().CandidateSolution(
            provider="codex",
            diff="diff --git a/app.py b/app.py\n",
            model="gpt-test",
            final_response="done",
        )
        with (
            mock.patch.object(
                bakeoff,
                "_historical_candidate",
                side_effect=AssertionError("live source must not be reread"),
            ),
            mock.patch.object(
                bakeoff,
                "_codex_candidate",
                return_value=(codex_candidate, codex_candidate.diff, ("app.py",)),
            ),
            mock.patch.object(bakeoff, "_usage_records", return_value=((), ())),
            mock.patch.object(
                bakeoff._execution(),
                "generate_report",
                return_value={},
            ) as generate_report,
            mock.patch.object(bakeoff, "_write_report"),
        ):
            completed = bakeoff._command_complete_run(
                argparse.Namespace(
                    run_dir=Path(started["run_directory"]),
                    native_result=native_path,
                )
            )
        self.assertEqual(completed["status"], "completed")
        restored = generate_report.call_args.kwargs["claude_candidate"]
        self.assertEqual(restored.diff, candidate.diff)
        self.assertEqual(restored.final_response, "historical response")

    def test_completion_rejects_a_tampered_historical_artifact(self) -> None:
        run_directory = self.root / "tampered-run"
        run_directory.mkdir()
        frozen = {
            "schema_version": 1,
            "candidate": {
                "provider": "claude",
                "diff": "diff --git a/app.py b/app.py\n",
                "model": "claude-test",
                "final_response": "done",
            },
            "recovery": {"limitations": []},
            "final_response": "done",
        }
        artifact = run_directory / "historical-result.json"
        digest = bakeoff._write_canonical_json(artifact, frozen)
        artifact.write_bytes(artifact.read_bytes() + b" ")
        run = {
            "historical_result": {
                "schema_version": 1,
                "path": artifact.name,
                "sha256": digest,
            }
        }

        with self.assertRaisesRegex(
            bakeoff.BakeoffError,
            "artifact digest does not match",
        ):
            bakeoff._historical_candidate_for_completion(run, run_directory)

    def test_legacy_completion_falls_back_to_live_recovery(self) -> None:
        candidate = bakeoff._execution().CandidateSolution(
            provider="claude",
            diff="legacy diff",
            model="claude-test",
            final_response="legacy",
        )
        recovery = {"provenance": "legacy", "limitations": []}
        legacy_run = {"replay": {}, "baseline": {}}
        with mock.patch.object(
            bakeoff,
            "_historical_candidate",
            return_value=(candidate, recovery, "legacy"),
        ) as live_recovery:
            restored = bakeoff._historical_candidate_for_completion(
                legacy_run,
                self.root,
            )
        live_recovery.assert_called_once_with(legacy_run)
        self.assertEqual(restored, (candidate, recovery, "legacy"))

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
        args.confirm_empty_beginning = True
        with patches as values:
            values["_selected_replay"].return_value = replay
            values["_baseline"].return_value = baseline
            values["_capabilities"].return_value = parity
            prepared = bakeoff._command_prepare(args)
        self.assertEqual(prepared["status"], "ready_for_approval")
        self.assertEqual(prepared["configuration"]["baseline"]["kind"], "empty_directory")
        self.assertEqual(
            prepared["configuration"]["beginning_state"],
            {"kind": "non_git", "commit": None},
        )
        self.assertEqual(
            prepared["configuration"]["ending_state"],
            {"kind": "non_git", "commit": None},
        )
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
            "requires_empty_beginning_confirmation": True,
            "empty_starting_directory_confirmed": False,
            "candidates": [{"path": "existing.txt"}],
            "unclassified_files": ["existing.txt"],
            "classifications": {},
        }
        questions = bakeoff._selection_questions(selection)
        self.assertEqual(
            [question["id"] for question in questions],
            ["classify_non_git_files", "confirm_empty_beginning"],
        )
        self.assertIn("empty directory", questions[1]["question"])
        self.assertIn("stop", questions[1]["question"])
        self.assertNotIn("existed_before_claude", questions[0]["classification_flags"])

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
        self.assertIn("--beginning-kind", baseline_options)
        self.assertIn("--ending-kind", baseline_options)
        self.assertIn("--baseline-commit", baseline_options)
        self.assertIn("--ending-commit", baseline_options)
        self.assertIn("--confirm-empty-beginning", baseline_options)
        self.assertIn("--confirm-repository-selection", baseline_options)
        run_options = {
            option for action in choices["run"]._actions for option in action.option_strings
        }
        self.assertIn("--expected-historical-result-sha256", run_options)
        self.assertIn("--expected-prepared-configuration-sha256", run_options)
        self.assertIn("--request", run_options)
        self.assertIn("--request-stdin", run_options)
        self.assertIn("--source-path", run_options)
        self.assertIn("--message-uuid", run_options)
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
