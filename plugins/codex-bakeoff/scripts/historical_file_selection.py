#!/usr/bin/env python3
"""Live file classification for historical Claude candidate reconstruction.

This module intentionally records paths and user decisions without hashing or
snapshotting their contents. Candidate patches are assembled from the live
sources when a run is completed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

MAX_CANDIDATE_FILES = 2_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PATCH_BYTES = 16 * 1024 * 1024
SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
GENERATED_PARTS = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
    }
)


class FileSelectionError(ValueError):
    """A live file selection cannot be represented safely."""


def _absolute_no_follow(raw: Path | str) -> Path:
    return Path(os.path.abspath(Path(raw).expanduser()))


def _canonical_directory(raw: Path | str, *, unavailable: str) -> Path:
    absolute = _absolute_no_follow(raw)
    directory = (
        absolute if absolute.parent == absolute else absolute.parent.resolve() / absolute.name
    )
    try:
        current = directory.lstat()
    except OSError as error:
        raise FileSelectionError(unavailable) from error
    if not stat.S_ISDIR(current.st_mode):
        raise FileSelectionError(unavailable)
    return directory


def _relative(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise FileSelectionError("Selected file paths must be non-empty text.")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        raise FileSelectionError("Selected file paths must be relative.")
    if any(part in {"", ".", "..", ".git"} for part in path.parts):
        raise FileSelectionError("Selected file paths contain an unsafe component.")
    return path.as_posix()


def _git(
    root: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=timeout,
        env={
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise FileSelectionError(detail or "Git could not inspect the selected files.")
    return result


def _decode_path(raw: bytes, *, label: str) -> str:
    try:
        return _relative(raw.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as error:
        raise FileSelectionError(f"A {label} path is not valid UTF-8.") from error


def _is_sensitive(relative: str) -> bool:
    path = PurePosixPath(relative)
    name = path.name.casefold()
    return (
        name in SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.endswith(SENSITIVE_SUFFIXES)
        or any(part.casefold() in {"secrets", ".ssh", ".aws"} for part in path.parts)
    )


def _metadata(root: Path, relative: str) -> dict[str, Any]:
    safe = _relative(relative)
    path = root / safe
    symlink_reason = None
    try:
        if path.is_symlink():
            kind = "symlink"
            link_target = os.readlink(path)
            size = len(link_target.encode("utf-8", errors="surrogateescape"))
            symlink_reason = "Symlinks cannot be included safely and must be excluded."
        elif path.is_file():
            kind = "regular"
            size = path.stat().st_size
        elif path.is_dir():
            kind = "directory"
            size = None
        elif os.path.lexists(path):
            kind = "special"
            size = None
        else:
            kind = "deleted"
            size = 0
    except OSError as error:
        raise FileSelectionError(f"Cannot inspect selected path {safe}: {error}") from error

    reason = None
    suggested = None
    selectable = kind in {"regular", "symlink", "deleted"}
    if isinstance(size, int) and size > MAX_FILE_BYTES:
        selectable = False
        reason = "File exceeds the per-file candidate limit."
    elif symlink_reason is not None:
        selectable = False
        suggested = "exclude"
        reason = symlink_reason
    elif kind in {"directory", "special"}:
        selectable = False
        reason = "This file type cannot be included in a candidate patch."
    elif _is_sensitive(safe):
        selectable = False
        suggested = "exclude"
        reason = "Potentially sensitive file; it cannot be included in a report."
    return {
        "path": safe,
        "kind": kind,
        "size": size,
        "selectable": selectable,
        "suggested_class": suggested,
        "reason": reason,
    }


def _is_gitlink(repository: Path, relative: str) -> bool:
    result = _git(
        repository,
        "ls-files",
        "--stage",
        "-z",
        "--",
        relative,
        check=False,
    )
    return result.returncode == 0 and result.stdout.startswith(b"160000 ")


def _bounded(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(entries) > MAX_CANDIDATE_FILES:
        raise FileSelectionError(
            f"More than {MAX_CANDIDATE_FILES:,} dirty files were found."
        )
    return entries


def _git_attribution_root(
    repository: Path,
    attribution_root: Path | str | None,
) -> tuple[Path, str | None]:
    if attribution_root is None:
        return repository, None
    selected = _canonical_directory(
        attribution_root,
        unavailable="The selected Git attribution directory is unavailable.",
    )
    try:
        relative = selected.relative_to(repository)
    except ValueError as error:
        raise FileSelectionError(
            "The selected Git attribution directory must be inside its repository."
        ) from error
    return selected, None if selected == repository else relative.as_posix()


def inspect_git(
    root: Path | str,
    *,
    attribution_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return staged, unstaged, and untracked changes as selectable units."""

    repository = _canonical_directory(
        root,
        unavailable="The selected Git repository is unavailable.",
    )
    top_level = _git(repository, "rev-parse", "--show-toplevel").stdout
    try:
        actual = Path(top_level.decode("utf-8", errors="strict").strip()).resolve()
    except UnicodeDecodeError as error:
        raise FileSelectionError("The Git repository path is not valid UTF-8.") from error
    if actual != repository:
        raise FileSelectionError("The selected Git source must be its repository root.")

    _, pathspec = _git_attribution_root(repository, attribution_root)
    arguments = [
        "--literal-pathspecs",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]
    if pathspec is not None:
        arguments.extend(("--", pathspec))
    raw = _git(repository, *arguments).stdout
    fields = raw.split(b"\x00")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4 or field[2:3] != b" ":
            raise FileSelectionError("Git returned an unrecognized working-tree status.")
        try:
            status = field[:2].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise FileSelectionError("Git returned an invalid status code.") from error
        path = _decode_path(field[3:], label="Git working-tree")
        original_path: str | None = None
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise FileSelectionError("Git returned an incomplete rename or copy status.")
            original_path = _decode_path(fields[index], label="Git original")
            index += 1
        metadata = _metadata(repository, path)
        if "D" in status and metadata["kind"] == "directory":
            metadata = {
                **metadata,
                "kind": "deleted",
                "size": 0,
                "selectable": not _is_sensitive(path),
                "suggested_class": "exclude" if _is_sensitive(path) else None,
                "reason": (
                    "Potentially sensitive path; it cannot be included in a report."
                    if _is_sensitive(path)
                    else None
                ),
                "current_kind": "untracked_directory",
            }
        elif metadata["kind"] == "directory" and _is_gitlink(repository, path):
            metadata = {
                **metadata,
                "kind": "submodule",
                "selectable": False,
                "reason": (
                    "Submodule changes are surfaced but cannot be included in "
                    "a recorded candidate patch."
                ),
            }
        if original_path is not None and _is_sensitive(original_path):
            metadata = {
                **metadata,
                "selectable": False,
                "suggested_class": "exclude",
                "reason": (
                    "The original path is potentially sensitive; this change "
                    "cannot be included in a report."
                ),
            }
        entry = {
            **metadata,
            "status": status,
            "staged": status[0] not in {" ", "?"},
            "unstaged": status[1] not in {" ", "?"},
            "untracked": status == "??",
            "original_path": original_path,
        }
        affected = [path]
        if original_path is not None and "R" in status:
            affected.insert(0, original_path)
        entry["affected_paths"] = affected
        entries.append(entry)
    return _bounded(sorted(entries, key=lambda item: str(item["path"])))


def select_git(
    root: Path | str,
    *,
    attribution_root: Path | str | None = None,
    claude_output_files: Iterable[str] = (),
    confirmed: bool = False,
) -> dict[str, Any]:
    """Select which current Git change units supplement Claude's recovered result."""

    repository = _canonical_directory(
        root,
        unavailable="The selected Git repository is unavailable.",
    )
    selected_root, _ = _git_attribution_root(repository, attribution_root)
    candidates = inspect_git(repository, attribution_root=selected_root)
    by_path = {str(entry["path"]): entry for entry in candidates}
    aliases: dict[str, str] = {}
    for path, entry in by_path.items():
        aliases[path] = path
        original = entry.get("original_path")
        if isinstance(original, str) and original not in aliases:
            aliases[original] = path

    requested = {_relative(value) for value in claude_output_files}
    unknown = requested - set(aliases)
    if unknown:
        raise FileSelectionError(
            "Selected files are not current Git changes: " + ", ".join(sorted(unknown))
        )
    selected_ids = {aliases[path] for path in requested}
    selected = [entry for path, entry in by_path.items() if path in selected_ids]
    for entry in selected:
        if entry.get("selectable") is not True:
            raise FileSelectionError(
                f"Selected Git change cannot be captured safely: {entry['path']}"
            )
    selected_bytes = sum(
        int(entry["size"]) for entry in selected if isinstance(entry.get("size"), int)
    )
    if selected_bytes > MAX_TOTAL_BYTES:
        raise FileSelectionError("The selected Git changes exceed the candidate byte limit.")

    dirty = bool(candidates)
    confirmation_recorded = bool(confirmed)
    return {
        "schema_version": 1,
        "source_kind": "git",
        "source_root": str(repository),
        "attribution_root": str(selected_root),
        "working_tree_state": "dirty" if dirty else "clean",
        "requires_confirmation": dirty,
        "confirmed": confirmation_recorded,
        "complete": not dirty or confirmation_recorded,
        "candidates": candidates,
        "claude_output_changes": selected,
        "unselected_changes": [
            entry for path, entry in by_path.items() if path not in selected_ids
        ],
    }


def _raise_walk_error(error: OSError) -> None:
    raise error


def inspect_directory(
    root: Path | str,
    *,
    allow_git_with_commits: bool = False,
) -> list[dict[str, Any]]:
    """Return a bounded non-Git inventory, surfacing pruned generated trees."""

    directory = _canonical_directory(
        root,
        unavailable="The selected non-Git directory is unavailable.",
    )
    if (
        not allow_git_with_commits
        and _git(
            directory,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            check=False,
        ).returncode
        == 0
    ):
        raise FileSelectionError("A non-Git classification cannot point inside Git.")

    entries: list[dict[str, Any]] = []
    try:
        for current, raw_directories, raw_files in os.walk(
            directory,
            topdown=True,
            onerror=_raise_walk_error,
            followlinks=False,
        ):
            current_path = Path(current)
            kept: list[str] = []
            for name in sorted(raw_directories):
                if name.casefold() == ".git":
                    continue
                child = current_path / name
                relative = child.relative_to(directory).as_posix()
                if child.is_symlink():
                    entries.append(_metadata(directory, relative))
                elif name.casefold() in GENERATED_PARTS:
                    entries.append(
                        {
                            "path": _relative(relative),
                            "kind": "generated_tree",
                            "size": None,
                            "selectable": False,
                            "suggested_class": "exclude",
                            "reason": ("Generated or dependency tree; contents were not expanded."),
                        }
                    )
                else:
                    kept.append(name)
            raw_directories[:] = kept
            for name in sorted(raw_files):
                if name.casefold() == ".git":
                    continue
                entries.append(
                    _metadata(
                        directory,
                        (current_path / name).relative_to(directory).as_posix(),
                    )
                )
    except OSError as error:
        raise FileSelectionError(f"Cannot inventory the non-Git directory: {error}") from error
    return _bounded(sorted(entries, key=lambda item: str(item["path"])))


def inspect_result_directory(root: Path | str) -> list[dict[str, Any]]:
    """Inventory a native result with the same bounded exclusions as its source."""

    directory = _canonical_directory(
        root,
        unavailable="The selected non-Git directory is unavailable.",
    )
    try:
        root_stat = directory.lstat()
    except OSError as error:
        raise FileSelectionError("The recorded Codex project does not exist.") from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise FileSelectionError("The recorded Codex project does not exist.")

    entries: list[dict[str, Any]] = []
    try:
        for current, raw_directories, raw_files in os.walk(
            directory,
            topdown=True,
            onerror=_raise_walk_error,
            followlinks=False,
        ):
            current_path = Path(current)
            kept: list[str] = []
            for name in sorted(raw_directories):
                if name.casefold() == ".git":
                    continue
                child = current_path / name
                relative = child.relative_to(directory).as_posix()
                if child.is_symlink():
                    entries.append(_metadata(directory, relative))
                elif name.casefold() in GENERATED_PARTS:
                    entries.append(
                        {
                            "path": _relative(relative),
                            "kind": "generated_tree",
                            "size": None,
                            "selectable": False,
                            "suggested_class": "exclude",
                            "reason": ("Generated or dependency tree; contents were not expanded."),
                        }
                    )
                else:
                    kept.append(name)
            raw_directories[:] = kept
            for name in sorted(raw_files):
                relative = (current_path / name).relative_to(directory).as_posix()
                if PurePosixPath(relative).name.casefold() == ".git":
                    continue
                entries.append(_metadata(directory, relative))
    except OSError as error:
        raise FileSelectionError(f"Cannot inventory the Codex result directory: {error}") from error
    return _bounded(sorted(entries, key=lambda item: str(item["path"])))


def _source_metadata(path: Path) -> tuple[str, int]:
    try:
        if path.is_symlink():
            return (
                "symlink",
                len(os.readlink(path).encode("utf-8", errors="surrogateescape")),
            )
        if path.is_file():
            return ("regular", path.stat().st_size)
    except OSError as error:
        raise FileSelectionError(f"Cannot inspect selected source {path}: {error}") from error
    raise FileSelectionError(f"Selected source must be a file or symlink: {path}")


def select_directory(
    root: Path | str,
    *,
    created_by_claude: Iterable[str] = (),
    existed_before_claude: Iterable[str] = (),
    exclude_files: Iterable[str] = (),
    confirmed: bool = False,
    empty_starting_directory_confirmed: bool | None = None,
    allow_git_with_commits: bool = False,
) -> dict[str, Any]:
    """Require one classification for every bounded non-Git inventory entry."""

    directory = _canonical_directory(
        root,
        unavailable="The selected non-Git directory is unavailable.",
    )
    candidates = inspect_directory(
        directory,
        allow_git_with_commits=allow_git_with_commits,
    )
    by_path = {str(entry["path"]): entry for entry in candidates}
    legacy_baseline_paths = {_relative(value) for value in existed_before_claude}
    classes = {
        "created_by_claude": {_relative(value) for value in created_by_claude},
        "exclude": {_relative(value) for value in exclude_files},
    }
    if legacy_baseline_paths:
        # Backward compatibility for completing run artifacts created before
        # non-empty non-Git baselines were removed from the public workflow.
        classes["existed_before_claude"] = legacy_baseline_paths
    all_selected = set().union(*classes.values())
    unknown = all_selected - set(by_path)
    if unknown:
        raise FileSelectionError(
            "Classified files are not in the current non-Git inventory: "
            + ", ".join(sorted(unknown))
        )
    owners: dict[str, list[str]] = {}
    for classification, paths in classes.items():
        for path in paths:
            owners.setdefault(path, []).append(classification)
    overlaps = {path: values for path, values in owners.items() if len(values) > 1}
    if overlaps:
        raise FileSelectionError(
            "A file cannot have more than one classification: " + ", ".join(sorted(overlaps))
        )
    for classification in ("created_by_claude", "existed_before_claude"):
        for path in classes.get(classification, set()):
            if by_path[path].get("selectable") is not True:
                raise FileSelectionError(f"{path} can only be classified as Exclude.")

    baseline_entries: list[dict[str, Any]] = []
    for path in sorted(legacy_baseline_paths):
        entry = by_path[path]
        baseline_entries.append(
            {
                "path": path,
                "source_path": str(directory / path),
                "source_kind": entry["kind"],
                "size": entry["size"],
                "classification": "existed_before_claude",
            }
        )

    unclassified = sorted(set(by_path) - all_selected)
    confirmation_recorded = bool(confirmed)
    empty_confirmed = (
        confirmation_recorded
        if empty_starting_directory_confirmed is None
        else bool(empty_starting_directory_confirmed)
    )
    complete = confirmation_recorded and empty_confirmed and not unclassified
    output_paths = classes["created_by_claude"]
    selected_bytes = sum(
        int(entry["size"]) for entry in baseline_entries if isinstance(entry.get("size"), int)
    ) + sum(
        int(by_path[path]["size"])
        for path in output_paths
        if isinstance(by_path[path].get("size"), int)
    )
    if selected_bytes > MAX_TOTAL_BYTES:
        raise FileSelectionError("The classified baseline and Claude output exceed the byte limit.")
    baseline_kind = "classified_directory" if baseline_entries else "empty_directory"
    return {
        "schema_version": 1,
        "source_kind": "non_git",
        "source_root": str(directory),
        "requires_confirmation": True,
        "confirmed": confirmation_recorded,
        "requires_empty_beginning_confirmation": not baseline_entries,
        "empty_starting_directory_confirmed": empty_confirmed and not baseline_entries,
        "complete": complete,
        "candidates": candidates,
        "unclassified_files": unclassified,
        "classifications": {
            classification: [by_path[path] for path in sorted(paths)]
            for classification, paths in classes.items()
        },
        "before_files": baseline_entries,
        "claude_output_files": [
            {
                **by_path[path],
                "classification": "created_by_claude",
            }
            for path in sorted(output_paths)
        ],
        "baseline_kind": baseline_kind,
    }


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _safe_member(root: Path, relative: str) -> Path:
    """Return one lexical member after rejecting symlinked parent components."""

    safe = _relative(relative)
    current = root
    for part in PurePosixPath(safe).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise FileSelectionError(f"Selected path traverses a symlinked parent: {safe}")
        if os.path.lexists(current) and not current.is_dir():
            raise FileSelectionError(f"Selected path has a non-directory parent: {safe}")
    return root / safe


def _copy_source(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        _remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise FileSelectionError(f"Selected source is no longer a file: {source}")


def _check_live_sources(sources: Iterable[Path]) -> int:
    total = 0
    for source in sources:
        _, size = _source_metadata(source)
        if size > MAX_FILE_BYTES:
            raise FileSelectionError(
                f"Selected source now exceeds the per-file byte limit: {source}"
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise FileSelectionError("The live classified sources exceed the candidate byte limit.")
    return total


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_directory_baseline_target(
    source_root: Path | str,
    target_root: Path | str,
) -> Path:
    """Validate an empty local project before copying a non-Git baseline."""

    source = _canonical_directory(
        source_root,
        unavailable="The Claude source directory is unavailable.",
    )
    target = _canonical_directory(
        target_root,
        unavailable="The registered baseline project changed or is unavailable.",
    )
    target_fd, target_stat = _open_pinned_directory(
        target,
        label="registered baseline project",
    )
    try:
        _validate_pinned_baseline_target(
            source,
            target,
            target_fd,
            target_stat,
        )
    finally:
        os.close(target_fd)
    return target


def _validate_copied_symlinks(target: Path, relatives: Iterable[str]) -> None:
    for relative in relatives:
        destination = _safe_member(target, relative)
        if not destination.is_symlink():
            continue
        try:
            resolved = destination.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise FileSelectionError(
                f"Cannot safely resolve baseline symlink {relative}: {error}"
            ) from error
        if not _is_within(resolved, target):
            raise FileSelectionError(
                f"Baseline symlink points outside the registered project: {relative}"
            )


def _directory_baseline_sources(
    selection: Mapping[str, Any],
) -> tuple[Path, list[Mapping[str, Any]], list[tuple[str, Path]]]:
    source = _absolute_no_follow(str(selection.get("source_root", "")))
    raw_files = selection.get("before_files")
    if not isinstance(raw_files, list):
        raise FileSelectionError("The non-Git file classification is incomplete.")

    files: list[tuple[str, Path]] = []
    checked: list[Mapping[str, Any]] = []
    for raw in raw_files:
        if not isinstance(raw, Mapping) or raw.get("classification") != "existed_before_claude":
            raise FileSelectionError("A non-Git baseline classification is invalid.")
        relative = _relative(str(raw.get("path", "")))
        current = _safe_member(source, relative)
        recorded = Path(os.path.abspath(Path(str(raw.get("source_path", ""))).expanduser()))
        if recorded != Path(os.path.abspath(current)):
            raise FileSelectionError(
                f"The baseline source does not match the Claude directory: {relative}"
            )
        checked.append(raw)
        files.append((relative, current))
    return source, checked, files


CreatedPath = tuple[int, str, int, int, int]
ReadPath = tuple[int, str, int, int, int]
DirectoryKey = tuple[str, ...]


def _identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _open_pinned_directory(
    directory: Path,
    *,
    label: str,
) -> tuple[int, os.stat_result]:
    before = directory.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise FileSelectionError(f"The {label} changed before copying.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise FileSelectionError(f"The {label} changed before copying.") from error
    opened = os.fstat(directory_fd)
    try:
        after = directory.lstat()
    except OSError:
        os.close(directory_fd)
        raise
    if not _same_identity(before, opened) or not _same_identity(opened, after):
        os.close(directory_fd)
        raise FileSelectionError(f"The {label} changed before copying.")
    return directory_fd, opened


def _created_path(
    parent_fd: int,
    name: str,
    current: os.stat_result,
) -> CreatedPath:
    return (
        parent_fd,
        name,
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
    )


def _create_exclusive_parents(
    relative: str,
    created: list[CreatedPath],
    directories: dict[DirectoryKey, int],
) -> tuple[int, str]:
    parts = PurePosixPath(_relative(relative)).parts
    parent_key: DirectoryKey = ()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for part in parts[:-1]:
        key = (*parent_key, part)
        if key in directories:
            parent_key = key
            continue
        parent_fd = directories[parent_key]
        try:
            os.mkdir(part, dir_fd=parent_fd)
        except FileExistsError as error:
            raise FileSelectionError(
                f"An unexpected path appeared in the baseline project: {relative}"
            ) from error
        try:
            directory_fd = os.open(part, flags, dir_fd=parent_fd)
        except OSError as error:
            raise FileSelectionError(
                f"Cannot pin a baseline destination directory: {relative}"
            ) from error
        opened = os.fstat(directory_fd)
        linked = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or not _same_identity(opened, linked):
            os.close(directory_fd)
            raise FileSelectionError(
                f"A baseline destination directory changed during copying: {relative}"
            )
        created.append(_created_path(parent_fd, part, opened))
        directories[key] = directory_fd
        parent_key = key
    return directories[parent_key], parts[-1]


def _open_existing_parent(
    relative: str,
    directories: dict[DirectoryKey, int],
    *,
    label: str,
) -> tuple[int, str]:
    parts = PurePosixPath(_relative(relative)).parts
    parent_key: DirectoryKey = ()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for part in parts[:-1]:
        key = (*parent_key, part)
        if key not in directories:
            try:
                directories[key] = os.open(
                    part,
                    flags,
                    dir_fd=directories[parent_key],
                )
            except OSError as error:
                raise FileSelectionError(
                    f"A {label} path changed before copying: {relative}"
                ) from error
        parent_key = key
    return directories[parent_key], parts[-1]


def _open_pinned_entry(
    relative: str,
    directories: dict[DirectoryKey, int],
    read_paths: list[ReadPath],
    *,
    label: str,
) -> tuple[str, int | None, str | None, os.stat_result, int]:
    parent_fd, name = _open_existing_parent(
        relative,
        directories,
        label=label,
    )
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise FileSelectionError(f"A {label} path changed before copying: {relative}") from error
    record = (
        parent_fd,
        name,
        linked.st_dev,
        linked.st_ino,
        stat.S_IFMT(linked.st_mode),
    )
    if stat.S_ISLNK(linked.st_mode):
        link_target = os.readlink(name, dir_fd=parent_fd)
        size = len(link_target.encode("utf-8", errors="surrogateescape"))
        read_paths.append(record)
        return ("symlink", None, link_target, linked, size)
    if not stat.S_ISREG(linked.st_mode):
        raise FileSelectionError(f"A {label} source is no longer a regular file: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        source_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise FileSelectionError(f"A {label} source changed before copying: {relative}") from error
    opened = os.fstat(source_fd)
    if not stat.S_ISREG(opened.st_mode) or not _same_identity(linked, opened):
        os.close(source_fd)
        raise FileSelectionError(f"A {label} source changed before copying: {relative}")
    read_paths.append(record)
    return ("regular", source_fd, None, opened, opened.st_size)


def _verify_read_paths(read_paths: Iterable[ReadPath], *, label: str) -> None:
    for parent_fd, name, device, inode, file_type in read_paths:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise FileSelectionError(f"A {label} source changed during copying: {name}") from error
        if (
            current.st_dev != device
            or current.st_ino != inode
            or stat.S_IFMT(current.st_mode) != file_type
        ):
            raise FileSelectionError(f"A {label} source changed during copying: {name}")


def _copy_fd_contents(
    source_fd: int,
    destination_fd: int,
    *,
    relative: str,
    mode: int,
) -> int:
    with (
        os.fdopen(source_fd, "rb", closefd=False) as source_stream,
        os.fdopen(destination_fd, "wb", closefd=False) as destination_stream,
    ):
        copied = 0
        while chunk := source_stream.read(1024 * 1024):
            copied += len(chunk)
            if copied > MAX_FILE_BYTES:
                raise FileSelectionError(
                    f"Selected source now exceeds the per-file byte limit: {relative}"
                )
            destination_stream.write(chunk)
    os.fchmod(destination_fd, stat.S_IMODE(mode))
    return copied


def _copy_pinned_entry_to_path(
    relative: str,
    source_directories: dict[DirectoryKey, int],
    read_paths: list[ReadPath],
    destination: Path,
    *,
    label: str,
) -> int:
    kind, source_fd, _link_target, source_stat, source_size = _open_pinned_entry(
        relative,
        source_directories,
        read_paths,
        label=label,
    )
    if source_size > MAX_FILE_BYTES:
        if source_fd is not None:
            os.close(source_fd)
        raise FileSelectionError(f"Selected source now exceeds the per-file byte limit: {relative}")
    if destination.exists() or destination.is_symlink():
        _remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        raise FileSelectionError(f"A {label} symlink must be excluded: {relative}")

    assert source_fd is not None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        destination_fd = os.open(destination, flags, 0o600)
    except Exception:
        os.close(source_fd)
        raise
    try:
        return _copy_fd_contents(
            source_fd,
            destination_fd,
            relative=relative,
            mode=source_stat.st_mode,
        )
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def _copy_source_exclusive(
    relative: str,
    created: list[CreatedPath],
    target_directories: dict[DirectoryKey, int],
    source_directories: dict[DirectoryKey, int],
    read_paths: list[ReadPath],
) -> int:
    parent_fd, destination_name = _create_exclusive_parents(
        relative,
        created,
        target_directories,
    )
    kind, source_fd, _link_target, source_stat, source_size = _open_pinned_entry(
        relative,
        source_directories,
        read_paths,
        label="Claude baseline",
    )
    if source_size > MAX_FILE_BYTES:
        if source_fd is not None:
            os.close(source_fd)
        raise FileSelectionError(f"Selected source now exceeds the per-file byte limit: {relative}")
    if kind == "symlink":
        raise FileSelectionError(f"A Claude baseline symlink must be excluded: {relative}")

    assert source_fd is not None
    try:
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            destination_fd = os.open(
                destination_name,
                write_flags,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as error:
            raise FileSelectionError(
                f"An unexpected path appeared in the baseline project: {relative}"
            ) from error
        remembered_stat = os.fstat(destination_fd)
        created.append(
            (
                parent_fd,
                destination_name,
                remembered_stat.st_dev,
                remembered_stat.st_ino,
                stat.S_IFMT(remembered_stat.st_mode),
            )
        )
        try:
            copied = _copy_fd_contents(
                source_fd,
                destination_fd,
                relative=relative,
                mode=source_stat.st_mode,
            )
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    return copied


def _rollback_created(created: Iterable[CreatedPath]) -> None:
    for parent_fd, name, device, inode, file_type in reversed(tuple(created)):
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            current.st_dev != device
            or current.st_ino != inode
            or stat.S_IFMT(current.st_mode) != file_type
        ):
            continue
        try:
            if stat.S_ISDIR(current.st_mode):
                os.rmdir(name, dir_fd=parent_fd)
            else:
                os.unlink(name, dir_fd=parent_fd)
        except (FileNotFoundError, OSError):
            continue


def _verify_created(created: Iterable[CreatedPath]) -> None:
    for parent_fd, name, device, inode, file_type in created:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise FileSelectionError(
                f"A copied baseline path changed during copying: {name}"
            ) from error
        if (
            current.st_dev != device
            or current.st_ino != inode
            or stat.S_IFMT(current.st_mode) != file_type
        ):
            raise FileSelectionError(f"A copied baseline path changed during copying: {name}")


def _verify_pinned_directory(
    directory: Path,
    directory_stat: os.stat_result,
    directories: Mapping[DirectoryKey, int],
    *,
    label: str,
) -> None:
    try:
        current_root = directory.lstat()
    except OSError as error:
        raise FileSelectionError(f"The {label} changed during copying.") from error
    if not _same_identity(directory_stat, current_root):
        raise FileSelectionError(f"The {label} changed during copying.")
    for key, directory_fd in sorted(
        directories.items(),
        key=lambda item: len(item[0]),
    ):
        opened = os.fstat(directory_fd)
        if not key:
            linked = current_root
        else:
            try:
                linked = os.stat(
                    key[-1],
                    dir_fd=directories[key[:-1]],
                    follow_symlinks=False,
                )
            except OSError as error:
                raise FileSelectionError(
                    f"A {label} directory changed during copying: {PurePosixPath(*key).as_posix()}"
                ) from error
        if not _same_identity(opened, linked):
            raise FileSelectionError(
                f"A {label} directory changed during copying: "
                f"{PurePosixPath(*key).as_posix() if key else '.'}"
            )


def _validate_pinned_baseline_target(
    source: Path,
    target: Path,
    target_fd: int,
    target_stat: os.stat_result,
) -> None:
    if target == source or _is_within(target, source) or _is_within(source, target):
        raise FileSelectionError(
            "The registered baseline project must not overlap the Claude directory."
        )
    if not _git(target, "rev-parse", "--show-toplevel", check=False).returncode:
        raise FileSelectionError("The registered baseline project must be non-Git.")
    try:
        if os.listdir(target_fd):
            raise FileSelectionError("The registered baseline project must be completely empty.")
    except OSError as error:
        raise FileSelectionError(
            f"Cannot inspect the registered baseline project: {error}"
        ) from error
    _verify_pinned_directory(
        target,
        target_stat,
        {(): target_fd},
        label="registered baseline project",
    )


def _target_inventory(target: Path) -> set[str]:
    paths: set[str] = set()
    for current, raw_directories, raw_files in os.walk(
        target,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        kept: list[str] = []
        for name in raw_directories:
            child = current_path / name
            paths.add(child.relative_to(target).as_posix())
            if not child.is_symlink():
                kept.append(name)
        raw_directories[:] = kept
        for name in raw_files:
            paths.add((current_path / name).relative_to(target).as_posix())
    return paths


def _expected_target_paths(relatives: Iterable[str]) -> set[str]:
    paths: set[str] = set()
    for relative in relatives:
        parts = PurePosixPath(_relative(relative)).parts
        for index in range(1, len(parts) + 1):
            paths.add(PurePosixPath(*parts[:index]).as_posix())
    return paths


def materialize_directory_baseline(
    selection: Mapping[str, Any],
    target_root: Path | str,
) -> dict[str, Any]:
    """Copy only user-classified unchanged baseline files into an empty project."""

    source, _, files = _directory_baseline_sources(selection)
    if not files:
        raise FileSelectionError("The classified non-Git baseline has no files.")

    source = _canonical_directory(
        source,
        unavailable="The Claude baseline directory changed or is unavailable.",
    )
    target = _canonical_directory(
        target_root,
        unavailable="The registered baseline project changed or is unavailable.",
    )
    target_fd, target_stat = _open_pinned_directory(
        target,
        label="registered baseline project",
    )
    try:
        _validate_pinned_baseline_target(
            source,
            target,
            target_fd,
            target_stat,
        )
    except Exception:
        os.close(target_fd)
        raise
    target_directories: dict[DirectoryKey, int] = {(): target_fd}

    try:
        source_fd, source_stat = _open_pinned_directory(
            source,
            label="Claude baseline directory",
        )
    except Exception:
        os.close(target_fd)
        raise
    source_directories: dict[DirectoryKey, int] = {(): source_fd}
    created: list[CreatedPath] = []
    read_paths: list[ReadPath] = []
    try:
        copied_bytes = 0
        for relative, _ in files:
            copied_bytes += _copy_source_exclusive(
                relative,
                created,
                target_directories,
                source_directories,
                read_paths,
            )
            if copied_bytes > MAX_TOTAL_BYTES:
                raise FileSelectionError(
                    "The live classified sources exceed the candidate byte limit."
                )
        _verify_pinned_directory(
            source,
            source_stat,
            source_directories,
            label="Claude baseline directory",
        )
        _verify_read_paths(read_paths, label="Claude baseline")
        _verify_created(created)
        _verify_pinned_directory(
            target,
            target_stat,
            target_directories,
            label="registered baseline project",
        )
        _validate_copied_symlinks(target, (relative for relative, _ in files))
        expected = _expected_target_paths(relative for relative, _ in files)
        if _target_inventory(target) != expected:
            raise FileSelectionError(
                "Unexpected files appeared in the registered baseline project."
            )
        _verify_created(created)
        _verify_pinned_directory(
            target,
            target_stat,
            target_directories,
            label="registered baseline project",
        )
    except Exception:
        _rollback_created(created)
        raise
    finally:
        for directory_fd in reversed(tuple(target_directories.values())):
            os.close(directory_fd)
        for directory_fd in reversed(tuple(source_directories.values())):
            os.close(directory_fd)
    return {
        "source": str(source),
        "target": str(target),
        "copied_files": [relative for relative, _ in files],
    }


def _check_git_baseline_sources(
    repository: Path,
    baseline_commit: str,
    paths: Iterable[str],
) -> int:
    total = 0
    for relative in sorted({_relative(path) for path in paths}):
        result = _git(
            repository,
            "cat-file",
            "-s",
            f"{baseline_commit}:{relative}",
            check=False,
        )
        if result.returncode:
            continue
        try:
            size = int(result.stdout.decode("ascii", errors="strict").strip())
        except (UnicodeDecodeError, ValueError) as error:
            raise FileSelectionError(
                f"Git returned an invalid baseline size for {relative}."
            ) from error
        if size > MAX_FILE_BYTES:
            raise FileSelectionError(
                f"Selected baseline file exceeds the per-file byte limit: {relative}"
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise FileSelectionError(
                "The selected Git baseline files exceed the candidate byte limit."
            )
    return total


def _git_apply_ready(patch: str) -> str:
    """Add mode metadata omitted by difflib-style new/deleted file patches."""

    chunks = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    normalized: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        lines = chunk.splitlines(keepends=True)
        if lines and lines[0].startswith("diff --git "):
            has_mode = any(
                line.startswith(("new file mode ", "deleted file mode ")) for line in lines[1:4]
            )
            if not has_mode:
                if len(lines) > 1 and lines[1].startswith("--- /dev/null"):
                    lines.insert(1, "new file mode 100644\n")
                elif any(line.startswith("+++ /dev/null") for line in lines[1:4]):
                    lines.insert(1, "deleted file mode 100644\n")
        normalized.append("".join(lines))
    return "".join(normalized)


def _candidate_patch_bytes(root: Path) -> bytes:
    return _git(
        root,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--no-renames",
        "HEAD",
        "--",
    ).stdout


def _patch_chunks(patch: bytes) -> list[bytes]:
    chunks = re.split(rb"(?=^diff --git )", patch, flags=re.MULTILINE)
    if chunks and not chunks[0]:
        chunks.pop(0)
    if any(not chunk.startswith(b"diff --git ") for chunk in chunks):
        raise FileSelectionError("Git returned an unrecognized candidate patch.")
    return chunks


def _decode_candidate_patch(root: Path, patch_bytes: bytes) -> str:
    try:
        return patch_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        info = root / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        with (info / "attributes").open("a", encoding="utf-8") as handle:
            handle.write("* -diff\n")
        binary_bytes = _candidate_patch_bytes(root)

    original_chunks = _patch_chunks(patch_bytes)
    binary_chunks = _patch_chunks(binary_bytes)
    if len(original_chunks) != len(binary_chunks):
        raise FileSelectionError("Git returned inconsistent candidate patches.")

    decoded: list[str] = []
    for original, binary in zip(original_chunks, binary_chunks):
        if original.partition(b"\n")[0] != binary.partition(b"\n")[0]:
            raise FileSelectionError("Git returned inconsistent candidate patches.")
        try:
            decoded.append(original.decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            try:
                decoded.append(binary.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as error:
                raise FileSelectionError("A candidate path is not valid UTF-8.") from error
    return "".join(decoded)


def _candidate_diff(root: Path) -> tuple[str, tuple[str, ...]]:
    _git(root, "add", "-f", "-A")
    patch = _decode_candidate_patch(root, _candidate_patch_bytes(root))
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise FileSelectionError("The classified Claude patch exceeds its byte limit.")
    names = _git(
        root,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "HEAD",
        "--",
    ).stdout
    try:
        changed = tuple(
            sorted(part.decode("utf-8", errors="strict") for part in names.split(b"\x00") if part)
        )
    except UnicodeDecodeError as error:
        raise FileSelectionError("A candidate path is not valid UTF-8.") from error
    return patch, changed


def _neutralize_temp_git_attributes(root: Path) -> None:
    """Make a temporary non-Git capture preserve worktree bytes exactly."""

    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.safecrlf", "false")
    info = root / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text(
        "* -filter -ident -text -working-tree-encoding\n",
        encoding="utf-8",
    )


def _checkout_empty_baseline(root: Path) -> None:
    tree = _git(root, "mktree", input_bytes=b"").stdout.decode("ascii", errors="strict").strip()
    if re.fullmatch(r"[a-fA-F0-9]{40,64}", tree) is None:
        raise FileSelectionError("Git could not create the empty candidate baseline.")
    commit = (
        _git(
            root,
            "-c",
            "user.name=Codex Bakeoff",
            "-c",
            "user.email=bakeoff@localhost",
            "commit-tree",
            tree,
            "-m",
            "empty baseline",
        )
        .stdout.decode("ascii", errors="strict")
        .strip()
    )
    if re.fullmatch(r"[a-fA-F0-9]{40,64}", commit) is None:
        raise FileSelectionError("Git could not create the empty candidate baseline.")
    _git(root, "checkout", "--quiet", "--detach", commit)


def build_git_candidate_patch(
    *,
    repository: Path | str,
    baseline_commit: str | None,
    baseline_kind: str = "git_commit",
    recovered_patch: str | None,
    selection: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """Overlay selected live Git changes onto the recovered historical result."""

    if baseline_kind not in {"git_commit", "empty_directory"}:
        raise FileSelectionError("The Git candidate baseline kind is unsupported.")
    if baseline_kind == "git_commit" and not baseline_commit:
        raise FileSelectionError("The Git candidate baseline commit is required.")
    if baseline_kind == "empty_directory" and baseline_commit:
        raise FileSelectionError("An empty candidate baseline cannot have a Git commit.")
    source = _absolute_no_follow(repository)
    selected = selection.get("claude_output_changes")
    if not isinstance(selected, list):
        raise FileSelectionError("The Git file selection is incomplete.")
    live_sources: list[Path] = []
    affected_paths: list[str] = []
    for raw in selected:
        if not isinstance(raw, Mapping):
            raise FileSelectionError("The Git file selection entry is invalid.")
        path = _relative(str(raw.get("path", "")))
        raw_affected = raw.get("affected_paths")
        if isinstance(raw_affected, list):
            affected_paths.extend(
                _relative(value) for value in raw_affected if isinstance(value, str)
            )
        else:
            affected_paths.append(path)
        current = _safe_member(source, path)
        if current.is_dir() and raw.get("current_kind") == "untracked_directory":
            continue
        if os.path.lexists(current):
            live_sources.append(current)
    live_bytes = _check_live_sources(live_sources)
    baseline_bytes = (
        _check_git_baseline_sources(source, baseline_commit, affected_paths)
        if baseline_kind == "git_commit" and baseline_commit is not None
        else 0
    )
    if live_bytes + baseline_bytes > MAX_TOTAL_BYTES:
        raise FileSelectionError(
            "The selected live and baseline Git files exceed the candidate byte limit."
        )
    if isinstance(recovered_patch, str) and len(recovered_patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise FileSelectionError("The recovered Claude patch exceeds its byte limit.")

    with tempfile.TemporaryDirectory(prefix="claude-git-selection-") as temporary:
        parent = Path(temporary)
        candidate = parent / "candidate"
        template = parent / "template"
        template.mkdir()
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                f"--template={template}",
                "--",
                str(source),
                str(candidate),
            ],
            capture_output=True,
            check=False,
            timeout=120,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        if clone.returncode:
            detail = clone.stderr.decode("utf-8", errors="replace").strip()
            raise FileSelectionError(detail or "Cannot clone the Git baseline.")
        if baseline_kind == "git_commit" and baseline_commit is not None:
            _git(candidate, "checkout", "--quiet", "--detach", baseline_commit)
        else:
            _checkout_empty_baseline(candidate)
        if isinstance(recovered_patch, str) and recovered_patch.strip():
            applied = _git(
                candidate,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_bytes=_git_apply_ready(recovered_patch).encode("utf-8"),
                check=False,
            )
            if applied.returncode:
                detail = applied.stderr.decode("utf-8", errors="replace").strip()
                raise FileSelectionError(
                    "The recovered Claude result cannot be combined with the "
                    f"selected working-tree changes: {detail}"
                )
        ordered_selected = sorted(
            selected,
            key=lambda raw: (
                (
                    len(PurePosixPath(str(raw.get("path", ""))).parts)
                    if isinstance(raw, Mapping)
                    else 0
                ),
                str(raw.get("path", "")) if isinstance(raw, Mapping) else "",
            ),
        )
        for raw in ordered_selected:
            if not isinstance(raw, Mapping):
                raise FileSelectionError("The Git file selection entry is invalid.")
            status = str(raw.get("status") or "")
            original = raw.get("original_path")
            if "R" in status and isinstance(original, str):
                _remove_path(_safe_member(candidate, original))
        for raw in ordered_selected:
            if not isinstance(raw, Mapping):
                raise FileSelectionError("The Git file selection entry is invalid.")
            path = _relative(str(raw.get("path", "")))
            current = _safe_member(source, path)
            target = _safe_member(candidate, path)
            if current.is_dir() and raw.get("current_kind") == "untracked_directory":
                _remove_path(target)
            elif os.path.lexists(current):
                _copy_source(current, target)
            else:
                _remove_path(target)
        return _candidate_diff(candidate)


def build_directory_candidate_patch(
    selection: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """Build a classified non-Git result against its classified baseline."""

    source = _absolute_no_follow(str(selection.get("source_root", "")))
    _, _, baseline_sources = _directory_baseline_sources(selection)
    output = selection.get("claude_output_files")
    if not isinstance(output, list):
        raise FileSelectionError("The non-Git file classification is incomplete.")
    output_paths: list[str] = []
    for raw in output:
        if not isinstance(raw, Mapping):
            raise FileSelectionError("A Claude output classification is invalid.")
        relative = _relative(str(raw.get("path", "")))
        _safe_member(source, relative)
        output_paths.append(relative)

    source_fd, source_stat = _open_pinned_directory(
        source,
        label="Claude source directory",
    )
    source_directories: dict[DirectoryKey, int] = {(): source_fd}
    read_paths: list[ReadPath] = []
    try:
        with tempfile.TemporaryDirectory(prefix="claude-directory-selection-") as temporary:
            candidate = Path(temporary) / "candidate"
            candidate.mkdir()
            copied_bytes = 0
            for relative, _ in baseline_sources:
                copied_bytes += _copy_pinned_entry_to_path(
                    relative,
                    source_directories,
                    read_paths,
                    _safe_member(candidate, relative),
                    label="Claude baseline",
                )
            _git(candidate, "init", "--quiet")
            _git(candidate, "config", "user.email", "bakeoff@localhost")
            _git(candidate, "config", "user.name", "Codex Bakeoff")
            _neutralize_temp_git_attributes(candidate)
            _git(candidate, "add", "-f", "-A")
            _git(candidate, "commit", "--quiet", "--allow-empty", "-m", "baseline")
            for relative in output_paths:
                copied_bytes += _copy_pinned_entry_to_path(
                    relative,
                    source_directories,
                    read_paths,
                    _safe_member(candidate, relative),
                    label="Claude output",
                )
            if copied_bytes > MAX_TOTAL_BYTES:
                raise FileSelectionError(
                    "The live classified sources exceed the candidate byte limit."
                )
            _verify_pinned_directory(
                source,
                source_stat,
                source_directories,
                label="Claude source directory",
            )
            _verify_read_paths(read_paths, label="Claude")
            return _candidate_diff(candidate)
    finally:
        for directory_fd in reversed(tuple(source_directories.values())):
            os.close(directory_fd)


def build_directory_result_patch(
    selection: Mapping[str, Any],
    result_root: Path | str,
) -> tuple[str, tuple[str, ...]]:
    """Diff a native non-Git result against the classified copied baseline."""

    result = _absolute_no_follow(result_root)
    source, _, before_sources = _directory_baseline_sources(selection)
    source_fd, source_stat = _open_pinned_directory(
        source,
        label="Claude baseline directory",
    )
    source_directories: dict[DirectoryKey, int] = {(): source_fd}
    source_reads: list[ReadPath] = []
    try:
        result_fd, result_stat = _open_pinned_directory(
            result,
            label="Codex result directory",
        )
    except Exception:
        os.close(source_fd)
        raise
    result_directories: dict[DirectoryKey, int] = {(): result_fd}
    result_reads: list[ReadPath] = []
    try:
        inventory = inspect_result_directory(result)
        _verify_pinned_directory(
            result,
            result_stat,
            result_directories,
            label="Codex result directory",
        )
        by_path = {str(entry["path"]): entry for entry in inventory}
        before_paths = {relative for relative, _ in before_sources}
        unsupported_baseline = [
            entry
            for entry in inventory
            if entry.get("selectable") is not True and str(entry.get("path")) in before_paths
        ]
        if unsupported_baseline:
            raise FileSelectionError(
                "Codex changed baseline files into unsupported results: "
                + ", ".join(str(entry["path"]) for entry in unsupported_baseline)
            )
        result_files = [entry for entry in inventory if entry.get("selectable") is True]
        result_paths = [_relative(str(entry["path"])) for entry in result_files]
        for relative in result_paths:
            _safe_member(result, relative)

        with tempfile.TemporaryDirectory(prefix="codex-directory-result-") as temporary:
            candidate = Path(temporary) / "candidate"
            candidate.mkdir()
            baseline_bytes = 0
            for relative, _ in before_sources:
                baseline_bytes += _copy_pinned_entry_to_path(
                    relative,
                    source_directories,
                    source_reads,
                    _safe_member(candidate, relative),
                    label="Claude baseline",
                )
            if baseline_bytes > MAX_TOTAL_BYTES:
                raise FileSelectionError(
                    "The live classified sources exceed the candidate byte limit."
                )
            _git(candidate, "init", "--quiet")
            _git(candidate, "config", "user.email", "bakeoff@localhost")
            _git(candidate, "config", "user.name", "Codex Bakeoff")
            _neutralize_temp_git_attributes(candidate)
            _git(candidate, "add", "-f", "-A")
            _git(candidate, "commit", "--quiet", "--allow-empty", "-m", "baseline")

            for relative, _ in before_sources:
                if relative not in by_path:
                    _remove_path(_safe_member(candidate, relative))
            result_bytes = 0
            for relative in result_paths:
                result_bytes += _copy_pinned_entry_to_path(
                    relative,
                    result_directories,
                    result_reads,
                    _safe_member(candidate, relative),
                    label="Codex result",
                )
            if result_bytes > MAX_TOTAL_BYTES:
                raise FileSelectionError("The live Codex result exceeds the candidate byte limit.")
            _verify_pinned_directory(
                source,
                source_stat,
                source_directories,
                label="Claude baseline directory",
            )
            _verify_read_paths(source_reads, label="Claude baseline")
            _verify_pinned_directory(
                result,
                result_stat,
                result_directories,
                label="Codex result directory",
            )
            _verify_read_paths(result_reads, label="Codex result")
            return _candidate_diff(candidate)
    finally:
        for directory_fd in reversed(tuple(result_directories.values())):
            os.close(directory_fd)
        for directory_fd in reversed(tuple(source_directories.values())):
            os.close(directory_fd)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
