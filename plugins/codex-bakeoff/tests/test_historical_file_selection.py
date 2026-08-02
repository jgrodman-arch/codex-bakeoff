"""Tests for live Git and non-Git file classification."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "historical_file_selection.py"
SPEC = importlib.util.spec_from_file_location("historical_file_selection", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
selection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selection)


def _git(root: Path, *arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _repository(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root.as_posix()


class HistoricalFileSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clean_git_needs_no_file_confirmation(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        _git(repository, "add", "tracked.txt")
        _git(repository, "commit", "--quiet", "-m", "baseline")

        result = selection.select_git(repository)

        self.assertEqual(result["working_tree_state"], "clean")
        self.assertTrue(result["complete"])
        self.assertEqual(result["candidates"], [])

    def test_git_inventory_coalesces_and_selects_current_changes(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        for name in ("staged.txt", "unstaged.txt", "deleted.txt"):
            (repository / name).write_text("baseline\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        (repository / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(repository, "add", "staged.txt")
        (repository / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
        (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        (repository / "deleted.txt").unlink()

        pending = selection.select_git(
            repository,
            claude_output_files=("staged.txt", "untracked.txt"),
        )
        confirmed = selection.select_git(
            repository,
            claude_output_files=("staged.txt", "untracked.txt"),
            confirmed=True,
        )

        self.assertFalse(pending["complete"])
        self.assertEqual(
            {item["path"] for item in pending["candidates"]},
            {"deleted.txt", "staged.txt", "unstaged.txt", "untracked.txt"},
        )
        self.assertTrue(confirmed["complete"])
        self.assertEqual(
            {item["path"] for item in confirmed["claude_output_changes"]},
            {"staged.txt", "untracked.txt"},
        )

    def test_git_inventory_is_scoped_to_literal_selected_project(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        project = repository / ":!project"
        project.mkdir()
        (project / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        (project / "created.txt").write_text("created\n", encoding="utf-8")
        (repository / "outside.txt").write_text("outside\n", encoding="utf-8")

        result = selection.select_git(
            repository,
            attribution_root=project,
            claude_output_files=(":!project/created.txt",),
            confirmed=True,
        )

        self.assertEqual(result["source_root"], str(repository.resolve()))
        self.assertEqual(result["attribution_root"], str(project.resolve()))
        self.assertEqual(
            [item["path"] for item in result["candidates"]],
            [":!project/created.txt"],
        )
        self.assertEqual(
            [item["path"] for item in result["claude_output_changes"]],
            [":!project/created.txt"],
        )
        self.assertEqual(result["unselected_changes"], [])
        self.assertTrue(result["complete"])

    def test_selected_git_files_override_recovered_result(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        (repository / "kept.txt").write_text("old kept\n", encoding="utf-8")
        (repository / "override.txt").write_text("old override\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        baseline = _git(repository, "rev-parse", "HEAD").strip()
        (repository / "kept.txt").write_text("recovered kept\n", encoding="utf-8")
        (repository / "override.txt").write_text("recovered override\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "result")
        result_commit = _git(repository, "rev-parse", "HEAD").strip()
        recovered = _git(
            repository,
            "diff",
            "--binary",
            f"{baseline}..{result_commit}",
        )
        _git(repository, "checkout", "--quiet", "--detach", baseline)
        (repository / "override.txt").write_text("selected override\n", encoding="utf-8")
        (repository / "created.txt").write_text("selected new\n", encoding="utf-8")
        chosen = selection.select_git(
            repository,
            claude_output_files=("override.txt", "created.txt"),
            confirmed=True,
        )

        patch, changed = selection.build_git_candidate_patch(
            repository=repository,
            baseline_commit=baseline,
            recovered_patch=recovered,
            selection=chosen,
        )
        candidate = self.root / "candidate"
        _git(self.root, "clone", "--quiet", str(repository), str(candidate))
        _git(candidate, "checkout", "--quiet", "--detach", baseline)
        _git(candidate, "apply", "--binary", "-", input_text=patch)

        self.assertEqual(
            (candidate / "kept.txt").read_text(encoding="utf-8"),
            "recovered kept\n",
        )
        self.assertEqual(
            (candidate / "override.txt").read_text(encoding="utf-8"),
            "selected override\n",
        )
        self.assertEqual(
            (candidate / "created.txt").read_text(encoding="utf-8"),
            "selected new\n",
        )
        self.assertEqual(set(changed), {"created.txt", "kept.txt", "override.txt"})

    def test_selected_git_files_overlay_non_git_beginning(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        (repository / "kept.txt").write_text("committed kept\n", encoding="utf-8")
        (repository / "override.txt").write_text("committed override\n", encoding="utf-8")
        (repository / "deleted.txt").write_text("committed delete\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "historical result")
        ending = _git(repository, "rev-parse", "HEAD").strip()
        empty_tree = _git(repository, "mktree", input_text="").strip()
        recovered = _git(
            repository,
            "diff",
            "--binary",
            f"{empty_tree}..{ending}",
        )
        (repository / "override.txt").write_text("selected override\n", encoding="utf-8")
        (repository / "created.txt").write_text("selected new\n", encoding="utf-8")
        (repository / "unselected.txt").write_text("local only\n", encoding="utf-8")
        (repository / "deleted.txt").unlink()
        chosen = selection.select_git(
            repository,
            claude_output_files=("override.txt", "created.txt", "deleted.txt"),
            confirmed=True,
        )

        patch, changed = selection.build_git_candidate_patch(
            repository=repository,
            baseline_commit=None,
            baseline_kind="empty_directory",
            recovered_patch=recovered,
            selection=chosen,
        )
        candidate = self.root / "candidate"
        _repository(candidate)
        _git(candidate, "commit", "--quiet", "--allow-empty", "-m", "empty")
        _git(candidate, "apply", "--binary", "-", input_text=patch)

        self.assertEqual(
            (candidate / "kept.txt").read_text(encoding="utf-8"),
            "committed kept\n",
        )
        self.assertEqual(
            (candidate / "override.txt").read_text(encoding="utf-8"),
            "selected override\n",
        )
        self.assertEqual(
            (candidate / "created.txt").read_text(encoding="utf-8"),
            "selected new\n",
        )
        self.assertFalse((candidate / "deleted.txt").exists())
        self.assertFalse((candidate / "unselected.txt").exists())
        self.assertEqual(set(changed), {"created.txt", "kept.txt", "override.txt"})

    def test_selected_git_rename_is_one_change_unit(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        (repository / "old name.txt").write_text("contents\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        _git(repository, "mv", "old name.txt", "new name.txt")

        entries = selection.inspect_git(repository)
        chosen = selection.select_git(
            repository,
            claude_output_files=("new name.txt",),
            confirmed=True,
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["original_path"], "old name.txt")
        self.assertEqual(
            entries[0]["affected_paths"],
            ["old name.txt", "new name.txt"],
        )
        self.assertEqual(len(chosen["claude_output_changes"]), 1)

    def test_selected_rename_preserves_recreated_original_path(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        (repository / "a.txt").write_text("baseline\n", encoding="utf-8")
        _git(repository, "add", "a.txt")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        baseline = _git(repository, "rev-parse", "HEAD").strip()
        _git(repository, "mv", "a.txt", "z.txt")
        (repository / "a.txt").write_text("recreated\n", encoding="utf-8")
        chosen = selection.select_git(
            repository,
            claude_output_files=("z.txt", "a.txt"),
            confirmed=True,
        )

        patch, changed = selection.build_git_candidate_patch(
            repository=repository,
            baseline_commit=baseline,
            recovered_patch="",
            selection=chosen,
        )
        candidate = self.root / "candidate"
        _git(self.root, "clone", "--quiet", str(repository), str(candidate))
        _git(candidate, "checkout", "--quiet", "--detach", baseline)
        _git(candidate, "apply", "--binary", "-", input_text=patch)

        self.assertEqual(
            (candidate / "a.txt").read_text(encoding="utf-8"),
            "recreated\n",
        )
        self.assertEqual(
            (candidate / "z.txt").read_text(encoding="utf-8"),
            "baseline\n",
        )
        self.assertEqual(set(changed), {"a.txt", "z.txt"})

    def test_git_copy_never_traverses_an_unselected_baseline_symlink(self) -> None:
        repository = self.root / "repo"
        outside = self.root / "outside"
        outside.mkdir()
        _repository(repository)
        os.symlink(outside, repository / "link")
        _git(repository, "add", "link")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        baseline = _git(repository, "rev-parse", "HEAD").strip()
        (repository / "link").unlink()
        (repository / "link").mkdir()
        (repository / "link" / "payload").write_text("selected\n", encoding="utf-8")
        unsafe = selection.select_git(
            repository,
            claude_output_files=("link/payload",),
            confirmed=True,
        )

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "symlinked parent",
        ):
            selection.build_git_candidate_patch(
                repository=repository,
                baseline_commit=baseline,
                recovered_patch="",
                selection=unsafe,
            )
        self.assertFalse((outside / "payload").exists())

        safe = selection.select_git(
            repository,
            claude_output_files=("link", "link/payload"),
            confirmed=True,
        )
        patch, changed = selection.build_git_candidate_patch(
            repository=repository,
            baseline_commit=baseline,
            recovered_patch="",
            selection=safe,
        )
        self.assertIn("link/payload", patch)
        self.assertIn("link", changed)
        self.assertFalse((outside / "payload").exists())

    def test_dash_prefixed_paths_are_classifiable(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "-dash").write_text("contents\n", encoding="utf-8")

        result = selection.select_directory(
            directory,
            created_by_claude=("-dash",),
            confirmed=True,
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["claude_output_files"][0]["path"], "-dash")

    def test_non_git_requires_exact_classification_and_confirmation(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "created.txt").write_text("created\n", encoding="utf-8")
        (directory / "ignored.txt").write_text("ignored\n", encoding="utf-8")

        partial = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
        )
        complete = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
            exclude_files=("ignored.txt",),
            confirmed=True,
        )

        self.assertFalse(partial["complete"])
        self.assertEqual(partial["unclassified_files"], ["ignored.txt"])
        self.assertTrue(complete["complete"])
        self.assertTrue(complete["empty_starting_directory_confirmed"])
        self.assertEqual(complete["baseline_kind"], "empty_directory")
        self.assertEqual(
            set(complete["classifications"]),
            {"created_by_claude", "exclude"},
        )

    def test_non_git_empty_beginning_confirmation_is_independent(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "created.txt").write_text("created\n", encoding="utf-8")

        unconfirmed = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
            confirmed=True,
            empty_starting_directory_confirmed=False,
        )
        confirmed = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
            confirmed=True,
            empty_starting_directory_confirmed=True,
        )

        self.assertFalse(unconfirmed["complete"])
        self.assertFalse(unconfirmed["empty_starting_directory_confirmed"])
        self.assertTrue(confirmed["complete"])
        self.assertTrue(confirmed["empty_starting_directory_confirmed"])

    def test_git_repository_without_commits_can_use_directory_classification(
        self,
    ) -> None:
        directory = self.root / "unborn"
        _repository(directory)
        (directory / "created.txt").write_text("created\n", encoding="utf-8")

        result = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
            confirmed=True,
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["source_kind"], "non_git")
        self.assertEqual(result["baseline_kind"], "empty_directory")

    def test_git_repository_with_a_commit_rejects_directory_classification(
        self,
    ) -> None:
        directory = self.root / "committed"
        _repository(directory)
        (directory / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        _git(directory, "add", "tracked.txt")
        _git(directory, "commit", "--quiet", "-m", "baseline")

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "cannot point inside Git",
        ):
            selection.select_directory(directory)

    def test_post_task_git_repository_can_use_directory_classification(
        self,
    ) -> None:
        directory = self.root / "post-task"
        _repository(directory)
        (directory / "created.txt").write_text("created\n", encoding="utf-8")
        _git(directory, "add", "created.txt")
        _git(directory, "commit", "--quiet", "-m", "post-task result")

        result = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
            confirmed=True,
            allow_git_with_commits=True,
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["source_kind"], "non_git")
        self.assertEqual(result["baseline_kind"], "empty_directory")

    def test_existed_file_is_baseline_and_created_file_is_output(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        (directory / "created.txt").write_text("created\n", encoding="utf-8")

        complete = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            created_by_claude=("created.txt",),
            confirmed=True,
        )
        patch, changed = selection.build_directory_candidate_patch(complete)

        self.assertTrue(complete["complete"])
        self.assertEqual(complete["baseline_kind"], "classified_directory")
        self.assertEqual(
            [item["path"] for item in complete["before_files"]],
            ["existing.txt"],
        )
        self.assertIn("+created", patch)
        self.assertNotIn("existing.txt", patch)
        self.assertEqual(changed, ("created.txt",))

    def test_generated_tree_is_surfaced_and_can_only_be_excluded(self) -> None:
        directory = self.root / "directory"
        (directory / "node_modules" / "package").mkdir(parents=True)
        (directory / "node_modules" / "package" / "index.js").write_text(
            "generated\n",
            encoding="utf-8",
        )
        entries = selection.inspect_directory(directory)

        self.assertEqual(
            [(item["path"], item["kind"]) for item in entries],
            [("node_modules", "generated_tree")],
        )
        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "only be classified as Exclude",
        ):
            selection.select_directory(
                directory,
                created_by_claude=("node_modules",),
                confirmed=True,
            )
        result = selection.select_directory(
            directory,
            exclude_files=("node_modules",),
            confirmed=True,
        )
        self.assertTrue(result["complete"])

    def test_sensitive_file_can_only_be_excluded(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "only be classified as Exclude",
        ):
            selection.select_directory(
                directory,
                created_by_claude=(".env",),
                confirmed=True,
            )
        result = selection.select_directory(
            directory,
            exclude_files=(".env",),
            confirmed=True,
        )
        self.assertTrue(result["complete"])

    def test_rename_from_sensitive_path_cannot_be_selected(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        (repository / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        _git(repository, "add", ".env")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        _git(repository, "mv", ".env", "safe.txt")

        entries = selection.inspect_git(repository)

        self.assertEqual(entries[0]["original_path"], ".env")
        self.assertFalse(entries[0]["selectable"])
        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "cannot be captured safely",
        ):
            selection.select_git(
                repository,
                claude_output_files=("safe.txt",),
                confirmed=True,
            )

    def test_live_source_size_is_rechecked_when_building_patch(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        source = directory / "created.bin"
        source.write_bytes(b"small")
        result = selection.select_directory(
            directory,
            created_by_claude=("created.bin",),
            confirmed=True,
        )
        source.write_bytes(b"x" * (selection.MAX_FILE_BYTES + 1))

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "now exceeds the per-file byte limit",
        ):
            selection.build_directory_candidate_patch(result)

    def test_large_selected_deletion_is_rejected_before_diff_capture(self) -> None:
        repository = self.root / "repo"
        _repository(repository)
        tracked = repository / "large.txt"
        tracked.write_text("x" * 64, encoding="utf-8")
        _git(repository, "add", "large.txt")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        baseline = _git(repository, "rev-parse", "HEAD").strip()
        tracked.unlink()
        chosen = selection.select_git(
            repository,
            claude_output_files=("large.txt",),
            confirmed=True,
        )

        with (
            mock.patch.object(selection, "MAX_FILE_BYTES", 32),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "Selected baseline file exceeds",
            ),
        ):
            selection.build_git_candidate_patch(
                repository=repository,
                baseline_commit=baseline,
                recovered_patch="",
                selection=chosen,
            )

    def test_materialization_copies_only_existed_files(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        (directory / "created.txt").write_text("created\n", encoding="utf-8")
        (directory / "excluded.txt").write_text("excluded\n", encoding="utf-8")
        target = self.root / "registered"
        target.mkdir()

        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            created_by_claude=("created.txt",),
            exclude_files=("excluded.txt",),
            confirmed=True,
        )
        materialized = selection.materialize_directory_baseline(result, target)

        self.assertEqual(materialized["copied_files"], ["existing.txt"])
        self.assertEqual(
            (target / "existing.txt").read_text(encoding="utf-8"),
            "baseline\n",
        )
        self.assertFalse((target / "created.txt").exists())
        self.assertFalse((target / "excluded.txt").exists())

    def test_materialization_requires_empty_non_git_disjoint_target(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )

        nonempty = self.root / "nonempty"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaisesRegex(selection.FileSelectionError, "completely empty"):
            selection.materialize_directory_baseline(result, nonempty)
        self.assertEqual(
            (nonempty / "keep.txt").read_text(encoding="utf-8"),
            "keep\n",
        )

        nested = directory / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(selection.FileSelectionError, "must not overlap"):
            selection.materialize_directory_baseline(result, nested)

        git_target = self.root / "git-target"
        _repository(git_target)
        with self.assertRaisesRegex(selection.FileSelectionError, "must be non-Git"):
            selection.materialize_directory_baseline(result, git_target)

        real_target = self.root / "real-target"
        real_target.mkdir()
        linked_target = self.root / "linked-target"
        os.symlink(real_target, linked_target)
        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "changed or is unavailable",
        ):
            selection.validate_directory_baseline_target(directory, linked_target)
        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "changed or is unavailable",
        ):
            selection.materialize_directory_baseline(result, linked_target)
        self.assertEqual(list(real_target.iterdir()), [])

    def test_external_symlink_can_only_be_excluded(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        os.symlink(outside, directory / "link.txt")

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "only be classified as Exclude",
        ):
            selection.select_directory(
                directory,
                existed_before_claude=("link.txt",),
                confirmed=True,
            )

        result = selection.select_directory(
            directory,
            exclude_files=("link.txt",),
            confirmed=True,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["baseline_kind"], "empty_directory")

    def test_internal_symlink_can_only_be_excluded(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "target.txt").write_text("baseline\n", encoding="utf-8")
        os.symlink("target.txt", directory / "link.txt")

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "only be classified as Exclude",
        ):
            selection.select_directory(
                directory,
                existed_before_claude=("link.txt", "target.txt"),
                confirmed=True,
            )

        result = selection.select_directory(
            directory,
            existed_before_claude=("target.txt",),
            exclude_files=("link.txt",),
            confirmed=True,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["baseline_kind"], "classified_directory")

    def test_directory_result_patch_uses_classified_baseline(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        for name in ("unchanged.txt", "changed.txt", "deleted.txt"):
            (directory / name).write_text(f"baseline {name}\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=(
                "unchanged.txt",
                "changed.txt",
                "deleted.txt",
            ),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        selection.materialize_directory_baseline(result, target)
        _git(target, "init", "--quiet")
        (target / "changed.txt").write_text("codex change\n", encoding="utf-8")
        (target / "deleted.txt").unlink()
        (target / "created.txt").write_text("codex created\n", encoding="utf-8")
        (target / "dist").mkdir()
        (target / "dist" / "bundle.js").write_text(
            "codex bundle\n",
            encoding="utf-8",
        )

        patch, changed = selection.build_directory_result_patch(result, target)

        self.assertEqual(
            set(changed),
            {"changed.txt", "created.txt", "deleted.txt"},
        )
        self.assertNotIn("unchanged.txt", patch)
        self.assertIn("+codex change", patch)
        self.assertIn("+codex created", patch)
        self.assertNotIn("codex bundle", patch)

    def test_directory_patches_include_files_ignored_by_baseline_rules(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (directory / "baseline.log").write_text("baseline\n", encoding="utf-8")
        (directory / "claude.log").write_text("claude\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=(".gitignore", "baseline.log"),
            created_by_claude=("claude.log",),
            confirmed=True,
        )

        claude_patch, claude_changed = selection.build_directory_candidate_patch(result)
        self.assertEqual(claude_changed, ("claude.log",))
        self.assertIn("+claude", claude_patch)

        target = self.root / "registered"
        target.mkdir()
        selection.materialize_directory_baseline(result, target)
        (target / "baseline.log").write_text("codex modified\n", encoding="utf-8")
        (target / "codex.log").write_text("codex created\n", encoding="utf-8")
        codex_patch, codex_changed = selection.build_directory_result_patch(
            result,
            target,
        )
        self.assertEqual(
            set(codex_changed),
            {"baseline.log", "codex.log"},
        )
        self.assertIn("+codex modified", codex_patch)
        self.assertIn("+codex created", codex_patch)

    def test_directory_result_ignores_candidate_git_normalization_rules(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / ".gitattributes").write_text(
            "*.txt text eol=lf\n",
            encoding="utf-8",
        )
        (directory / "base.txt").write_bytes(b"same\n")
        result = selection.select_directory(
            directory,
            existed_before_claude=(".gitattributes", "base.txt"),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        selection.materialize_directory_baseline(result, target)
        (target / "base.txt").write_bytes(b"same\r\n")

        patch, changed = selection.build_directory_result_patch(result, target)

        self.assertEqual(changed, ("base.txt",))
        self.assertIn("-same", patch)
        self.assertIn("+same", patch)

    def test_non_utf8_files_use_binary_patches_for_both_candidates(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        raw = b"\xff\xfehello\n"
        (directory / "raw.bin").write_bytes(raw)
        result = selection.select_directory(
            directory,
            created_by_claude=("raw.bin",),
            confirmed=True,
        )

        claude_patch, claude_changed = selection.build_directory_candidate_patch(result)
        self.assertEqual(claude_changed, ("raw.bin",))
        self.assertIn("GIT binary patch", claude_patch)

        target = self.root / "codex-result"
        target.mkdir()
        (target / "raw.bin").write_bytes(raw)
        codex_patch, codex_changed = selection.build_directory_result_patch(
            result,
            target,
        )
        self.assertEqual(codex_changed, ("raw.bin",))
        self.assertIn("GIT binary patch", codex_patch)

    def test_non_utf8_file_does_not_hide_readable_text_diffs(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        raw = b"\xff\xfehello\n"
        (directory / "raw.bin").write_bytes(raw)
        (directory / "readable.txt").write_text(
            "Claude text\n",
            encoding="utf-8",
        )
        result = selection.select_directory(
            directory,
            created_by_claude=("raw.bin", "readable.txt"),
            confirmed=True,
        )

        claude_patch, claude_changed = selection.build_directory_candidate_patch(result)
        self.assertEqual(claude_changed, ("raw.bin", "readable.txt"))
        self.assertIn("GIT binary patch", claude_patch)
        self.assertIn("+Claude text", claude_patch)

        target = self.root / "codex-result"
        target.mkdir()
        (target / "raw.bin").write_bytes(raw)
        (target / "readable.txt").write_text(
            "Codex text\n",
            encoding="utf-8",
        )
        codex_patch, codex_changed = selection.build_directory_result_patch(
            result,
            target,
        )
        self.assertEqual(codex_changed, ("raw.bin", "readable.txt"))
        self.assertIn("GIT binary patch", codex_patch)
        self.assertIn("+Codex text", codex_patch)

    def test_non_utf8_symlink_targets_are_excluded_symmetrically(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        os.symlink(b"\xfftarget", os.fsencode(directory / "bad-link"))
        inventory = selection.inspect_directory(directory)
        self.assertFalse(inventory[0]["selectable"])
        self.assertIn("must be excluded", inventory[0]["reason"])
        result = selection.select_directory(
            directory,
            exclude_files=("bad-link",),
            confirmed=True,
        )

        claude_patch, claude_changed = selection.build_directory_candidate_patch(result)
        self.assertEqual(claude_patch, "")
        self.assertEqual(claude_changed, ())

        target = self.root / "codex-result"
        target.mkdir()
        os.symlink(b"\xfftarget", os.fsencode(target / "bad-link"))
        codex_patch, codex_changed = selection.build_directory_result_patch(
            result,
            target,
        )
        self.assertEqual(codex_patch, "")
        self.assertEqual(codex_changed, ())

    def test_directory_result_excludes_new_sensitive_files_symmetrically(
        self,
    ) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        selection.materialize_directory_baseline(result, target)
        (target / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

        patch, changed = selection.build_directory_result_patch(result, target)

        self.assertEqual(patch, "")
        self.assertEqual(changed, ())

    def test_generated_trees_are_excluded_from_both_candidates(self) -> None:
        directory = self.root / "directory"
        (directory / "dist").mkdir(parents=True)
        (directory / "dist" / "claude.js").write_text(
            "claude\n",
            encoding="utf-8",
        )
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            exclude_files=("dist",),
            confirmed=True,
        )

        claude_patch, claude_changed = selection.build_directory_candidate_patch(result)
        self.assertEqual(claude_patch, "")
        self.assertEqual(claude_changed, ())

        target = self.root / "registered"
        target.mkdir()
        selection.materialize_directory_baseline(result, target)
        (target / "dist").mkdir()
        (target / "dist" / "codex.js").write_text("codex\n", encoding="utf-8")
        codex_patch, codex_changed = selection.build_directory_result_patch(
            result,
            target,
        )
        self.assertEqual(codex_patch, "")
        self.assertEqual(codex_changed, ())

    def test_directory_result_applies_size_limit_per_tree(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        for name in ("one.txt", "two.txt"):
            (directory / name).write_bytes(b"abc")
        with mock.patch.object(selection, "MAX_TOTAL_BYTES", 10):
            result = selection.select_directory(
                directory,
                existed_before_claude=("one.txt", "two.txt"),
                confirmed=True,
            )
            target = self.root / "registered"
            target.mkdir()
            selection.materialize_directory_baseline(result, target)
            patch, changed = selection.build_directory_result_patch(result, target)

        self.assertEqual(patch, "")
        self.assertEqual(changed, ())

    def test_baseline_parent_symlink_drift_is_rejected(self) -> None:
        directory = self.root / "directory"
        (directory / "nested").mkdir(parents=True)
        (directory / "nested" / "existing.txt").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        result = selection.select_directory(
            directory,
            existed_before_claude=("nested/existing.txt",),
            confirmed=True,
        )
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "existing.txt").write_text("outside\n", encoding="utf-8")
        (directory / "nested" / "existing.txt").unlink()
        (directory / "nested").rmdir()
        os.symlink(outside, directory / "nested")
        target = self.root / "registered"
        target.mkdir()

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "symlinked parent",
        ):
            selection.materialize_directory_baseline(result, target)
        self.assertEqual(list(target.iterdir()), [])

    def test_candidate_capture_rejects_source_parent_symlink_swap(self) -> None:
        directory = self.root / "directory"
        (directory / "nested").mkdir(parents=True)
        (directory / "nested" / "created.txt").write_text(
            "claude\n",
            encoding="utf-8",
        )
        result = selection.select_directory(
            directory,
            created_by_claude=("nested/created.txt",),
            confirmed=True,
        )
        moved = self.root / "moved"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "created.txt").write_text("outside secret\n", encoding="utf-8")
        original_copy = selection._copy_pinned_entry_to_path
        swapped = False

        def swap_source(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                (directory / "nested").rename(moved)
                os.symlink(outside, directory / "nested")
                swapped = True
            return original_copy(*args, **kwargs)

        with (
            mock.patch.object(
                selection,
                "_copy_pinned_entry_to_path",
                side_effect=swap_source,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "path changed before copying",
            ),
        ):
            selection.build_directory_candidate_patch(result)

    def test_candidate_capture_rejects_source_root_symlink_swap(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "created.txt").write_text("Claude\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            created_by_claude=("created.txt",),
            confirmed=True,
        )
        moved = self.root / "moved-directory"
        directory.rename(moved)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "created.txt").write_text("outside secret\n", encoding="utf-8")
        os.symlink(outside, directory)

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "source directory changed before copying",
        ):
            selection.build_directory_candidate_patch(result)

    def test_selection_persists_canonical_parent_path(self) -> None:
        real_parent = self.root / "real-parent"
        directory = real_parent / "directory"
        directory.mkdir(parents=True)
        (directory / "created.txt").write_text("Claude\n", encoding="utf-8")
        alias = self.root / "alias"
        os.symlink(real_parent, alias)
        result = selection.select_directory(
            alias / "directory",
            created_by_claude=("created.txt",),
            confirmed=True,
        )
        self.assertEqual(result["source_root"], str(directory.resolve()))

        alias.unlink()
        outside_parent = self.root / "outside-parent"
        outside = outside_parent / "directory"
        outside.mkdir(parents=True)
        (outside / "created.txt").write_text("outside secret\n", encoding="utf-8")
        os.symlink(outside_parent, alias)

        patch, changed = selection.build_directory_candidate_patch(result)
        self.assertEqual(changed, ("created.txt",))
        self.assertIn("+Claude", patch)
        self.assertNotIn("outside secret", patch)

    def test_result_capture_rejects_result_parent_symlink_swap(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        selection.materialize_directory_baseline(result, target)
        (target / "nested").mkdir()
        (target / "nested" / "created.txt").write_text(
            "codex\n",
            encoding="utf-8",
        )
        moved = self.root / "moved-result"
        outside = self.root / "outside-result"
        outside.mkdir()
        (outside / "created.txt").write_text("outside secret\n", encoding="utf-8")
        original_copy = selection._copy_pinned_entry_to_path
        swapped = False

        def swap_result(*args, **kwargs):
            nonlocal swapped
            if kwargs.get("label") == "Codex result" and not swapped:
                (target / "nested").rename(moved)
                os.symlink(outside, target / "nested")
                swapped = True
            return original_copy(*args, **kwargs)

        with (
            mock.patch.object(
                selection,
                "_copy_pinned_entry_to_path",
                side_effect=swap_result,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "path changed before copying",
            ),
        ):
            selection.build_directory_result_patch(result, target)

    def test_result_capture_rejects_result_root_symlink_swap(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        result = selection.select_directory(directory, confirmed=True)
        target = self.root / "registered"
        target.mkdir()
        moved = self.root / "moved-registered"
        target.rename(moved)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "codex.txt").write_text("outside secret\n", encoding="utf-8")
        os.symlink(outside, target)

        with self.assertRaisesRegex(
            selection.FileSelectionError,
            "result directory changed before copying",
        ):
            selection.build_directory_result_patch(result, target)

    def test_materialization_rollback_preserves_concurrent_files(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        for name in ("one.txt", "two.txt"):
            (directory / name).write_text(name, encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("one.txt", "two.txt"),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        original_copy = selection._copy_source_exclusive
        injected = False

        def inject_unrelated(*args, **kwargs):
            nonlocal injected
            copied = original_copy(*args, **kwargs)
            if not injected:
                (target / "unrelated.txt").write_text(
                    "keep me\n",
                    encoding="utf-8",
                )
                injected = True
            return copied

        with (
            mock.patch.object(
                selection,
                "_copy_source_exclusive",
                side_effect=inject_unrelated,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "Unexpected files appeared",
            ),
        ):
            selection.materialize_directory_baseline(result, target)

        self.assertEqual(
            (target / "unrelated.txt").read_text(encoding="utf-8"),
            "keep me\n",
        )
        self.assertFalse((target / "one.txt").exists())
        self.assertFalse((target / "two.txt").exists())

    def test_materialization_never_overwrites_a_concurrent_destination(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        original_copy = selection._copy_source_exclusive

        def inject_collision(*args, **kwargs):
            (target / "existing.txt").write_text(
                "concurrent\n",
                encoding="utf-8",
            )
            original_copy(*args, **kwargs)

        with (
            mock.patch.object(
                selection,
                "_copy_source_exclusive",
                side_effect=inject_collision,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "unexpected path appeared",
            ),
        ):
            selection.materialize_directory_baseline(result, target)

        self.assertEqual(
            (target / "existing.txt").read_text(encoding="utf-8"),
            "concurrent\n",
        )

    def test_materialization_rejects_replaced_copied_leaf(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        original_copy = selection._copy_source_exclusive

        def replace_copied_leaf(*args, **kwargs):
            copied = original_copy(*args, **kwargs)
            (target / "existing.txt").unlink()
            (target / "existing.txt").write_text(
                "concurrent replacement\n",
                encoding="utf-8",
            )
            return copied

        with (
            mock.patch.object(
                selection,
                "_copy_source_exclusive",
                side_effect=replace_copied_leaf,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "copied baseline path changed during copying",
            ),
        ):
            selection.materialize_directory_baseline(result, target)

        self.assertEqual(
            (target / "existing.txt").read_text(encoding="utf-8"),
            "concurrent replacement\n",
        )

    def test_materialization_pins_target_across_root_path_swap(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        moved_target = self.root / "moved-registered"
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        original_copy = selection._copy_source_exclusive
        swapped = False

        def swap_target(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                target.rename(moved_target)
                os.symlink(unrelated, target)
                swapped = True
            return original_copy(*args, **kwargs)

        with (
            mock.patch.object(
                selection,
                "_copy_source_exclusive",
                side_effect=swap_target,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "changed during copying",
            ),
        ):
            selection.materialize_directory_baseline(result, target)

        self.assertEqual(list(unrelated.iterdir()), [])
        self.assertEqual(list(moved_target.iterdir()), [])

    def test_materialization_rejects_real_target_replacement_after_pinning(
        self,
    ) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "existing.txt").write_text("baseline\n", encoding="utf-8")
        result = selection.select_directory(
            directory,
            existed_before_claude=("existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        moved_target = self.root / "moved-registered"
        original_validate = selection._validate_pinned_baseline_target

        def replace_target(*args, **kwargs):
            target.rename(moved_target)
            target.mkdir()
            return original_validate(*args, **kwargs)

        with (
            mock.patch.object(
                selection,
                "_validate_pinned_baseline_target",
                side_effect=replace_target,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "changed during copying",
            ),
        ):
            selection.materialize_directory_baseline(result, target)

        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list(moved_target.iterdir()), [])

    def test_materialization_rejects_source_parent_symlink_swap(self) -> None:
        directory = self.root / "directory"
        (directory / "nested").mkdir(parents=True)
        (directory / "nested" / "existing.txt").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        result = selection.select_directory(
            directory,
            existed_before_claude=("nested/existing.txt",),
            confirmed=True,
        )
        target = self.root / "registered"
        target.mkdir()
        moved_nested = self.root / "moved-nested"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "existing.txt").write_text("outside secret\n", encoding="utf-8")
        original_copy = selection._copy_source_exclusive
        swapped = False

        def swap_source_parent(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                (directory / "nested").rename(moved_nested)
                os.symlink(outside, directory / "nested")
                swapped = True
            return original_copy(*args, **kwargs)

        with (
            mock.patch.object(
                selection,
                "_copy_source_exclusive",
                side_effect=swap_source_parent,
            ),
            self.assertRaisesRegex(
                selection.FileSelectionError,
                "path changed before copying",
            ),
        ):
            selection.materialize_directory_baseline(result, target)

        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(
            (outside / "existing.txt").read_text(encoding="utf-8"),
            "outside secret\n",
        )


if __name__ == "__main__":
    unittest.main()
