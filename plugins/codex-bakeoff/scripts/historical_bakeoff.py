#!/usr/bin/env python3
"""Lean, record-only Codex Bakeoff orchestration."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_ROOT.parent
DEFAULT_LEDGER = Path.home() / ".codex" / "external_agent_session_imports.json"
DEFAULT_MODEL_CACHE = Path.home() / ".codex" / "models_cache.json"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_RUN_ROOT = Path.home() / ".cache" / "codex-bakeoff" / "runs"
PRICING_PATH = PLUGIN_ROOT / "assets" / "model-pricing.json"
MAX_TIMEOUT_SECONDS = 14_400
THREAD_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}\Z")
COMMIT_PATTERN = re.compile(r"\A[0-9a-fA-F]{7,64}\Z")

_SECRET_PATTERNS = (
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b"), "[REDACTED_TOKEN]"),
    (
        re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)\S+"),
        r"\1[REDACTED]",
    ),
)


class BakeoffError(ValueError):
    """The requested bakeoff cannot continue."""


def _absolute_no_follow(raw: Path | str) -> Path:
    return Path(os.path.abspath(Path(raw).expanduser()))


def _canonical_parent_path(raw: Path | str) -> Path:
    absolute = _absolute_no_follow(raw)
    return absolute if absolute.parent == absolute else absolute.parent.resolve() / absolute.name


def _redact(value: object) -> str:
    result = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _jsonable(asdict(value))
    return value


def _module(name: str) -> Any:
    if str(SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(SCRIPT_ROOT))
    return importlib.import_module(name)


def _discovery() -> Any:
    return _module("historical_discovery")


def _execution() -> Any:
    return _module("historical_execution")


def _file_selection() -> Any:
    return _module("historical_file_selection")


def _verification() -> Any:
    return _module("historical_verification")


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BakeoffError(f"Cannot read {label}: {_redact(error)}") from error
    if not isinstance(loaded, dict):
        raise BakeoffError(f"{label} must contain a JSON object.")
    return loaded


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            _jsonable(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _write_canonical_json(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = _canonical_json_bytes(payload)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_bytes(serialized)
    temporary.replace(path)
    return hashlib.sha256(serialized).hexdigest()


def _write_report_html(run_directory: Path, report: Mapping[str, Any]) -> None:
    html = _execution().render_report_html(report)
    temporary = run_directory / f".report.{secrets.token_hex(6)}.tmp"
    temporary.write_text(html if html.endswith("\n") else f"{html}\n", encoding="utf-8")
    temporary.replace(run_directory / "report.html")


def _write_report(run_directory: Path, report: Mapping[str, Any]) -> None:
    _write_json(run_directory / "report.json", report)
    _write_report_html(run_directory, report)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def discover_codex_models(model_cache: str | Path | None = None) -> dict[str, Any]:
    path = Path(model_cache or DEFAULT_MODEL_CACHE).expanduser()
    try:
        payload = _load_json(path, label="the local Codex model catalog")
    except BakeoffError as error:
        return {
            "status": "unavailable",
            "source": str(path),
            "options": [],
            "limitations": [str(error)],
        }
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return {
            "status": "unavailable",
            "source": str(path),
            "options": [],
            "limitations": ["The model catalog has no model list."],
        }
    options: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, Mapping):
            continue
        slug = item.get("slug")
        if (
            not isinstance(slug, str)
            or item.get("visibility") != "list"
            or item.get("supported_in_api") is not True
            or item.get("upgrade")
        ):
            continue
        options.append(
            {
                "id": slug,
                "label": str(item.get("display_name") or item.get("name") or slug),
                "description": str(item.get("description") or ""),
                "recommended": slug == DEFAULT_CODEX_MODEL,
            }
        )
    if options and not any(item["recommended"] for item in options):
        options[0]["recommended"] = True
    return {
        "status": "available" if options else "unavailable",
        "source": str(path),
        "catalog_fetched_at": payload.get("fetched_at"),
        "options": options,
        "limitations": ([] if options else ["No visible API-supported Codex model is available."]),
    }


def _selected_model(model: str | None, model_cache: Path | None) -> str:
    if not isinstance(model, str) or not model.strip() or "\x00" in model:
        raise BakeoffError("Choose a Codex model before starting the comparison.")
    selected = model.strip()
    catalog = discover_codex_models(model_cache)
    choices = {item["id"] for item in catalog["options"]}
    if catalog.get("status") != "available" or not choices:
        return selected
    if selected not in choices:
        raise BakeoffError("The selected Codex model is not locally available.")
    return selected


def _sessions(ledger: Path) -> list[dict[str, Any]]:
    try:
        return _discovery().list_imported_sessions(ledger)
    except Exception as error:
        raise BakeoffError(_redact(error)) from error


def _selected_session(args: argparse.Namespace) -> dict[str, Any]:
    sessions = _sessions(args.ledger)
    selected = [
        item for item in sessions if item.get("imported_thread_id") == args.imported_thread_id
    ]
    if len(selected) != 1:
        raise BakeoffError("The selected imported Claude thread is unavailable.")
    return selected[0]


def _selected_replay(args: argparse.Namespace) -> dict[str, Any]:
    request_from_stdin = bool(getattr(args, "request_stdin", False))
    raw_request = sys.stdin.read() if request_from_stdin else getattr(args, "request", "")
    manual_request = str(raw_request or "").strip()
    if request_from_stdin and not manual_request:
        raise BakeoffError("The reviewed request is blank.")
    if "\x00" in manual_request:
        raise BakeoffError("The reviewed request is invalid.")
    raw_source_path = getattr(args, "source_path", None)
    raw_message_uuid = getattr(args, "message_uuid", None)
    source_path = str(raw_source_path or "").strip()
    message_uuid = str(raw_message_uuid or "").strip()
    if bool(source_path) != bool(message_uuid):
        raise BakeoffError(
            "Enter both the source transcript path and original user-message UUID."
        )
    session_recovered = True
    try:
        session = _selected_session(args)
    except Exception:
        if not (source_path and message_uuid and manual_request):
            raise
        session_recovered = False
        session = {
            "imported_thread_id": args.imported_thread_id,
            "source_path": source_path,
            "project_dir": str(getattr(args, "repo", None) or "") or None,
        }
    transcript_overridden = False
    request_discovery_failed = False
    try:
        if source_path and message_uuid:
            reviewed_source = Path(source_path).expanduser()
            if not reviewed_source.is_absolute():
                raise BakeoffError("The reviewed source transcript path must be absolute.")
            discovered_task: Mapping[str, Any] | None = None
            if session_recovered:
                try:
                    discovered_task = _discovery().build_thread_task(session)
                except Exception:
                    discovered_task = None
            original_source = session.get("source_path")
            transcript_overridden = (
                not session_recovered
                or not isinstance(original_source, str)
                or reviewed_source.resolve()
                != Path(original_source).expanduser().resolve()
                or discovered_task is None
                or discovered_task.get("message_uuid") != message_uuid
            )
            replay = _discovery().build_replay_spec(
                {**session, "source_path": str(reviewed_source)},
                {
                    "message_uuid": message_uuid,
                    "task_scope": "whole_thread",
                    "project_dir": getattr(args, "repo", None) or session.get("project_dir"),
                },
            )
        else:
            task = _discovery().build_thread_task(session)
            replay = _discovery().build_replay_spec(session, task)
    except Exception as error:
        if source_path or message_uuid:
            raise BakeoffError(_redact(error)) from error
        if not manual_request:
            raise BakeoffError(_redact(error)) from error
        request_discovery_failed = True
        project_dir = getattr(args, "repo", None) or session.get("project_dir")
        replay = {
            **session,
            "imported_thread_id": args.imported_thread_id,
            "request": manual_request,
            "project_dir": str(project_dir) if project_dir else None,
            "project_dirs": [str(project_dir)] if project_dir else [],
            "task_scope": "whole_thread",
            "user_message_count": 1,
        }
    discovered_request = str(replay.get("request") or "").strip()
    if manual_request:
        replay["request"] = manual_request
    replay["review_decisions"] = {
        "transcript_overridden": transcript_overridden,
        "request_overridden": bool(
            manual_request
            and (request_discovery_failed or manual_request != discovered_request)
        ),
    }
    return replay


def _command_sessions(args: argparse.Namespace) -> dict[str, Any]:
    sessions = _sessions(args.ledger)
    start = args.offset
    page = sessions[start : start + args.limit]
    return {
        "status": "ok",
        "source": str(args.ledger),
        "ordering": "original_claude_creation_descending",
        "offset": start,
        "has_more": start + len(page) < len(sessions),
        "total": len(sessions),
        "sessions": page,
    }


def _git_root(repository: Path) -> Path | None:
    try:
        root = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        head = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        root.returncode != 0
        or not root.stdout.strip()
        or head.returncode != 0
        or not head.stdout.strip()
    ):
        return None
    try:
        return Path(root.stdout.strip()).resolve()
    except OSError:
        return None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _git_root_for_changed_file(path: Path) -> Path | None:
    directory = path if path.is_dir() else path.parent
    while not directory.is_dir() and directory.parent != directory:
        directory = directory.parent
    return _git_root(directory)


def _resolved_replay_repository(
    args: argparse.Namespace,
    replay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    resolved = dict(replay)
    raw_original = replay.get("project_dir")
    original = (
        _canonical_parent_path(raw_original)
        if isinstance(raw_original, (str, Path)) and str(raw_original).strip()
        else None
    )
    if original is not None:
        resolved["original_project_dir"] = str(original)

    raw_changed_files = replay.get("historical_changed_files")
    raw_changed_files = raw_changed_files if isinstance(raw_changed_files, list) else []
    changed_files: list[Path] = []
    blocking_reasons: list[str] = []
    for raw_path in raw_changed_files:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        expanded = Path(raw_path).expanduser()
        if not expanded.is_absolute():
            blocking_reasons.append(
                "A task-attributed Claude file path is not absolute, so one replay "
                "repository cannot be resolved."
            )
            continue
        changed_files.append(_canonical_parent_path(expanded))

    changed_roots = [_git_root_for_changed_file(path) for path in changed_files]
    known_roots = {root for root in changed_roots if root is not None}
    suggested = original
    if changed_files and not blocking_reasons:
        contained_by_original = (
            original is not None
            and all(_path_is_within(path, original) for path in changed_files)
        )
        original_root = _git_root(original) if original is not None else None
        if (
            contained_by_original
            and original_root is not None
            and all(root == original_root for root in changed_roots)
        ):
            suggested = original
        elif contained_by_original and all(root is None for root in changed_roots):
            suggested = original
        elif len(known_roots) == 1 and all(root is not None for root in changed_roots):
            suggested = next(iter(known_roots))
        elif len(known_roots) > 1:
            blocking_reasons.append(
                "Claude changed files in multiple Git repositories; one bakeoff "
                "requires one repository."
            )
        else:
            blocking_reasons.append(
                "A task-attributed Claude file is outside an accessible Git repository, "
                "so one replay repository cannot be resolved."
            )

    raw_explicit = getattr(args, "repo", None)
    explicit = (
        _canonical_parent_path(raw_explicit)
        if isinstance(raw_explicit, (str, Path)) and str(raw_explicit).strip()
        else None
    )
    if explicit is not None and changed_files and not all(
        _path_is_within(path, explicit) for path in changed_files
    ):
        blocking_reasons.append(
            "The selected repository excludes task-attributed Claude changes."
        )
    overridden_blockers: list[str] = []
    if explicit is not None and bool(
        getattr(args, "confirm_repository_selection", False)
    ):
        overridden_blockers = list(dict.fromkeys(blocking_reasons))
        blocking_reasons = []
    effective = explicit or suggested
    if effective is not None:
        resolved["project_dir"] = str(effective)
        resolved["project_dirs"] = [str(effective)]

    source = "explicit_repo" if explicit is not None else "original_project_dir"
    if (
        explicit is None
        and changed_files
        and suggested is not None
        and original is not None
        and suggested != original
    ):
        source = "historical_changed_files"
    resolution = {
        "source": source,
        "original_project_dir": str(original) if original is not None else None,
        "effective_project_dir": str(effective) if effective is not None else None,
        "user_confirmed": bool(
            explicit is not None
            and getattr(args, "confirm_repository_selection", False)
        ),
        "overridden_blocking_reasons": overridden_blockers,
    }
    resolved["repository_resolution"] = resolution
    return resolved, resolution, list(dict.fromkeys(blocking_reasons))


def _reviewed_git_baseline(
    repository: Path,
    attribution_root: Path,
    commit: str,
) -> dict[str, Any]:
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise BakeoffError("Enter a valid historical Git commit.")
    git_root = _git_root(repository)
    if git_root is None:
        raise BakeoffError("The reviewed repository is not an accessible Git repository.")
    try:
        completed = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--verify", f"{commit}^{{commit}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BakeoffError("The reviewed historical Git commit could not be verified.") from error
    resolved = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40,64}", resolved) is None:
        raise BakeoffError("The reviewed historical Git commit is unavailable.")
    return {
        "kind": "git_commit",
        "proposed_kind": "git_commit",
        "repository": str(git_root),
        "attribution_root": str(attribution_root),
        "source_kind": "git",
        "commit": resolved.lower(),
        "confidence": "user_confirmed",
        "working_tree_state": "user_reviewed",
    }


def _with_historical_ending_commit(
    replay: Mapping[str, Any],
    baseline: Mapping[str, Any],
    reviewed_ending_commit: str,
) -> dict[str, Any]:
    result = dict(baseline)
    beginning_kind = result.get("beginning_kind")
    if beginning_kind not in {"git", "non_git"}:
        beginning_kind = "git" if result.get("kind") == "git_commit" else "non_git"
    ending_kind = result.get("ending_kind")
    if ending_kind not in {"git", "non_git"}:
        ending_kind = (
            "git"
            if result.get("source_kind") == "git" or result.get("kind") == "git_commit"
            else "non_git"
        )
    result.update({"beginning_kind": beginning_kind, "ending_kind": ending_kind})
    if ending_kind != "git":
        return result
    repository = result.get("repository")
    starting_commit = result.get("commit")
    if not isinstance(repository, str) or (
        beginning_kind == "git" and not isinstance(starting_commit, str)
    ):
        return result
    baseline_kind = "git_commit" if beginning_kind == "git" else "empty_directory"
    recovery_replay = {
        **replay,
        "project_dir": repository,
        "project_dirs": [repository],
    }
    try:
        inferred = _discovery().recover_historical_solution(
            recovery_replay,
            starting_commit if isinstance(starting_commit, str) else None,
            baseline_kind=baseline_kind,
        )
    except Exception:
        if not reviewed_ending_commit:
            return {
                **result,
                "ending_commit": None,
                "ending_commit_confidence": "unavailable",
            }
        inferred = {}

    inferred_commit = inferred.get("commit")
    reviewed_matches_inferred = (
        re.fullmatch(r"[0-9a-fA-F]{7,64}", reviewed_ending_commit) is not None
        and isinstance(inferred_commit, str)
        and inferred_commit.lower().startswith(reviewed_ending_commit.lower())
        and isinstance(inferred.get("diff"), str)
    )
    reviewed_override = bool(reviewed_ending_commit) and not reviewed_matches_inferred
    if reviewed_override:
        try:
            recovery = _discovery().recover_historical_solution(
                recovery_replay,
                starting_commit if isinstance(starting_commit, str) else None,
                baseline_kind=baseline_kind,
                ending_commit=reviewed_ending_commit,
            )
        except Exception as error:
            raise BakeoffError(_redact(error)) from error
    else:
        recovery = inferred
    ending_commit = recovery.get("commit")
    if reviewed_override and (
        not isinstance(ending_commit, str)
        or not isinstance(recovery.get("diff"), str)
    ):
        limitations = recovery.get("limitations")
        detail = (
            str(limitations[0])
            if isinstance(limitations, list) and limitations
            else "The reviewed historical ending commit is invalid."
        )
        raise BakeoffError(detail)
    return {
        **result,
        "ending_commit": ending_commit if isinstance(ending_commit, str) else None,
        "ending_commit_confidence": (
            "user_confirmed"
            if reviewed_override
            else "inferred"
            if isinstance(ending_commit, str)
            else "unavailable"
        ),
        "ending_commit_reviewed_override": reviewed_override,
    }


def _baseline(args: argparse.Namespace, replay: Mapping[str, Any]) -> dict[str, Any]:
    inspected_replay = dict(replay)
    raw_selected_project = args.repo or replay.get("project_dir")
    selected_project: Path | None = None
    if isinstance(raw_selected_project, (str, Path)) and str(raw_selected_project).strip():
        selected_project = _canonical_parent_path(raw_selected_project)
        try:
            selected_stat = selected_project.lstat()
        except OSError:
            selected_stat = None
        if selected_stat is not None and not stat.S_ISDIR(selected_stat.st_mode):
            raise BakeoffError("The selected project root changed or is not a real directory.")
        inspected_replay["project_dir"] = str(selected_project)
        inspected_replay["project_dirs"] = [inspected_replay["project_dir"]]
    beginning_kind = getattr(args, "beginning_kind", None)
    ending_kind = getattr(args, "ending_kind", None)
    manual_commit = str(getattr(args, "baseline_commit", "") or "").strip()
    manual_ending_commit = str(getattr(args, "ending_commit", "") or "").strip()
    if (beginning_kind is None) != (ending_kind is None):
        raise BakeoffError("Choose both the beginning state and end state.")
    if beginning_kind not in {None, "git", "non_git"}:
        raise BakeoffError("Choose a Git or Non-Git beginning state.")
    if ending_kind not in {None, "git", "non_git"}:
        raise BakeoffError("Choose a Git or Non-Git end state.")
    if beginning_kind == "git" and ending_kind == "non_git":
        raise BakeoffError("A Git beginning state requires a Git end state.")
    if beginning_kind == "git" and not manual_commit:
        raise BakeoffError("Enter a valid historical Git commit.")
    if beginning_kind == "non_git" and manual_commit:
        raise BakeoffError("A Non-Git beginning state cannot have a Git commit.")
    if manual_commit and beginning_kind != "git":
        raise BakeoffError("Choose a Git beginning state for the reviewed commit.")
    if ending_kind == "git" and not manual_ending_commit:
        raise BakeoffError("Enter a valid historical ending Git commit.")
    if ending_kind == "non_git" and manual_ending_commit:
        raise BakeoffError("A Non-Git end state cannot have a Git commit.")
    if manual_ending_commit and ending_kind != "git":
        raise BakeoffError("Choose a Git end state for the reviewed ending commit.")
    inspection_failed = False
    try:
        inspected = _discovery().inspect_baseline(inspected_replay)
    except Exception as error:
        if beginning_kind is None:
            raise BakeoffError(_redact(error)) from error
        inspection_failed = True
        inspected = {}

    if beginning_kind == "git":
        if selected_project is None:
            raise BakeoffError("Enter a replay repository for the reviewed Git beginning state.")
        reviewed = _reviewed_git_baseline(
            selected_project,
            selected_project,
            manual_commit,
        )
        if str(inspected.get("commit") or "").lower() == reviewed["commit"]:
            reviewed["confidence"] = inspected.get("confidence", reviewed["confidence"])
            reviewed["working_tree_state"] = inspected.get(
                "working_tree_state",
                reviewed["working_tree_state"],
            )
        baseline = {
            **inspected,
            **reviewed,
            "beginning_kind": "git",
            "ending_kind": "git",
            "reviewed_override": inspection_failed or not (
                inspected.get("kind") == "git_commit"
                and str(inspected.get("commit") or "").lower() == reviewed["commit"]
            ),
        }
        return _with_historical_ending_commit(
            inspected_replay,
            baseline,
            manual_ending_commit,
        )

    if beginning_kind == "non_git":
        if selected_project is None:
            raise BakeoffError("Enter a replay repository for the Non-Git beginning state.")
        git_root = _git_root(selected_project)
        if ending_kind == "git" and git_root is None:
            raise BakeoffError("A Git end state requires an accessible Git repository.")
        if ending_kind == "non_git" and git_root is not None:
            raise BakeoffError("A Non-Git end state cannot point inside Git.")
        baseline = {
            **inspected,
            "kind": "unclassified_directory",
            "proposed_kind": "empty_directory",
            "repository": str(git_root or selected_project),
            "attribution_root": str(selected_project),
            "source_kind": ending_kind,
            "beginning_kind": "non_git",
            "ending_kind": ending_kind,
            "commit": None,
            "confidence": "requires_user_classification",
            "reviewed_override": inspection_failed
            or not (
                (ending_kind == "git" and inspected.get("post_task_git_history"))
                or (ending_kind == "non_git" and _git_root(selected_project) is None)
            ),
        }
        return (
            _with_historical_ending_commit(
                inspected_replay,
                baseline,
                manual_ending_commit,
            )
            if ending_kind == "git"
            else baseline
        )

    raw_repository = args.repo or inspected.get("repository") or replay.get("project_dir")
    if not isinstance(raw_repository, (str, Path)) or not str(raw_repository).strip():
        return inspected
    repository = _canonical_parent_path(raw_repository)
    git_root = _git_root(repository)
    if git_root is not None:
        if inspected.get("post_task_git_history") and selected_project is not None:
            baseline = {
                **inspected,
                "kind": "unclassified_directory",
                "commit": None,
                "repository": str(git_root),
                "attribution_root": str(selected_project),
                "source_kind": "git",
                "beginning_kind": "non_git",
                "ending_kind": "git",
                "confidence": "requires_user_classification",
                "proposed_kind": "empty_directory",
            }
            return _with_historical_ending_commit(inspected_replay, baseline, "")
        attribution_root = selected_project or git_root
        if inspected.get("kind") != "git_commit" or not isinstance(inspected.get("commit"), str):
            return {
                **inspected,
                "repository": str(git_root),
                "attribution_root": str(attribution_root),
                "source_kind": "git",
                "beginning_kind": "git",
                "ending_kind": "git",
            }
        baseline = {
            **inspected,
            "repository": str(git_root),
            "attribution_root": str(attribution_root),
            "source_kind": "git",
            "beginning_kind": "git",
            "ending_kind": "git",
        }
        return _with_historical_ending_commit(inspected_replay, baseline, "")

    if selected_project is not None:
        repository = selected_project
    if not repository.is_dir():
        return {
            **inspected,
            "repository": str(repository),
            "attribution_root": str(repository),
            "source_kind": "non_git",
            "beginning_kind": "non_git",
            "ending_kind": "non_git",
        }
    return {
        **inspected,
        "kind": "unclassified_directory",
        "commit": None,
        "repository": str(repository),
        "attribution_root": str(repository),
        "source_kind": "non_git",
        "beginning_kind": "non_git",
        "ending_kind": "non_git",
        "confidence": "requires_user_classification",
        "proposed_kind": "empty_directory",
    }


def _capabilities(replay: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _discovery().inspect_capabilities(dict(replay))
    except Exception as error:
        raise BakeoffError(_redact(error)) from error


def _prompt(replay: Mapping[str, Any]) -> str:
    request = str(replay.get("request") or "").strip()
    if not request:
        raise BakeoffError("The selected Claude thread has no replayable request.")
    return (
        "Implement the reconstructed historical task as one task. "
        "Do not inspect or modify the original Claude output directory. "
        "Ignore the contents of memory_summary.md. "
        "Do not read memory files or invoke the codex-bakeoff skill. "
        "Do not run browser-control or browser-based end-to-end tests. Use non-browser validation only. "
        "Implement only from the task prompt and current workspace. "
        f"Task prompt:\n{request}"
    )


def _new_run_directory(parent: Path | None) -> Path:
    root = (parent or DEFAULT_RUN_ROOT).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for _ in range(100):
        path = root / f"{stamp}-{secrets.token_hex(5)}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise BakeoffError("Cannot allocate a bakeoff run directory.")


def _target_for_baseline(
    baseline: Mapping[str, Any],
    *,
    run_directory: Path,
) -> dict[str, Any]:
    if baseline.get("kind") == "git_commit":
        return {
            "type": "project",
            "project": baseline.get("repository"),
            "environment": {
                "type": "worktree",
                "startingState": {
                    "type": "branch",
                    "branchName": baseline.get("commit"),
                },
            },
        }
    if baseline.get("kind") == "empty_directory":
        return {
            "type": "projectless",
            "directoryName": f"codex-bakeoff-{run_directory.name[-10:]}",
        }
    # Retain legacy classified-directory targeting so already-started runs can
    # still be completed. New runs no longer produce this baseline kind.
    if baseline.get("kind") == "classified_directory":
        project = baseline.get("registered_baseline_project")
        project_id = baseline.get("registered_baseline_project_id")
        if (
            not isinstance(project, str)
            or not project
            or not isinstance(project_id, str)
            or not project_id
        ):
            raise BakeoffError(
                "A registered baseline project path and project ID are required "
                "for this non-Git baseline."
            )
        return {
            "type": "project",
            "projectId": project_id,
            "environment": {"type": "local"},
        }
    raise BakeoffError("The selected historical baseline is not runnable.")


def _classified_baseline(
    args: argparse.Namespace,
    baseline: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = _absolute_no_follow(str(baseline.get("repository") or ""))
    attribution_root = _absolute_no_follow(str(baseline.get("attribution_root") or repository))
    source_kind = baseline.get("source_kind")
    try:
        if source_kind == "git":
            non_git_values = (
                *(getattr(args, "created_by_claude", None) or ()),
                *(getattr(args, "exclude_file", None) or ()),
            )
            if non_git_values:
                raise BakeoffError("Non-Git classification flags cannot be used for a Git project.")
            selection = _file_selection().select_git(
                repository,
                attribution_root=attribution_root,
                claude_output_files=(getattr(args, "claude_output_file", None) or ()),
                confirmed=bool(getattr(args, "confirm_file_selection", False)),
            )
            empty_beginning_required = baseline.get("beginning_kind") == "non_git"
            empty_beginning_confirmed = bool(
                getattr(args, "confirm_empty_beginning", False)
            )
            selection = {
                **selection,
                "requires_empty_beginning_confirmation": empty_beginning_required,
                "empty_starting_directory_confirmed": (
                    empty_beginning_required and empty_beginning_confirmed
                ),
                "complete": selection["complete"]
                and (not empty_beginning_required or empty_beginning_confirmed),
            }
            classified_kind = (
                "empty_directory"
                if empty_beginning_required and empty_beginning_confirmed
                else baseline.get("kind")
            )
            return (
                {
                    **baseline,
                    "repository": selection["source_root"],
                    "attribution_root": selection["attribution_root"],
                    "kind": classified_kind,
                    "current_working_tree_state": selection["working_tree_state"],
                    "confidence": (
                        "user_confirmed_empty"
                        if classified_kind == "empty_directory"
                        else baseline.get("confidence")
                    ),
                },
                selection,
            )

        if source_kind == "non_git":
            if getattr(args, "claude_output_file", None):
                raise BakeoffError("--claude-output-file applies only to a Git working tree.")
            selection = _file_selection().select_directory(
                repository,
                created_by_claude=(getattr(args, "created_by_claude", None) or ()),
                exclude_files=(getattr(args, "exclude_file", None) or ()),
                confirmed=bool(getattr(args, "confirm_file_selection", False)),
                empty_starting_directory_confirmed=bool(
                    getattr(args, "confirm_empty_beginning", False)
                ),
            )
            classified_kind = (
                "empty_directory" if selection["complete"] else "unclassified_directory"
            )
            return (
                {
                    **baseline,
                    "repository": selection["source_root"],
                    "attribution_root": selection["source_root"],
                    "kind": classified_kind,
                    "proposed_kind": selection["baseline_kind"],
                    "confidence": (
                        "user_confirmed_empty"
                        if selection["complete"]
                        else "requires_user_classification"
                    ),
                },
                selection,
            )
    except BakeoffError:
        raise
    except Exception as error:
        raise BakeoffError(_redact(error)) from error

    return (
        dict(baseline),
        {
            "schema_version": 1,
            "source_kind": source_kind or "unknown",
            "source_root": str(repository),
            "requires_confirmation": False,
            "confirmed": False,
            "complete": False,
            "candidates": [],
        },
    )


def _selection_questions(
    selection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    source_kind = selection.get("source_kind")
    if (
        source_kind == "git"
        and selection.get("requires_confirmation") is True
        and selection.get("confirmed") is not True
    ):
        questions.append(
            {
                "id": "classify_git_working_tree",
                "question": (
                    "Which staged, unstaged, or untracked changes belong to "
                    "Claude's output? Pass --claude-output-file PATH for each "
                    "selected change, then --confirm-file-selection. Confirming "
                    "with no selected paths deliberately credits none of them."
                ),
                "selection_flag": "--claude-output-file",
                "confirmation_flag": "--confirm-file-selection",
                "changes": list(selection.get("candidates") or []),
                "selected_changes": list(selection.get("claude_output_changes") or []),
            }
        )
    if source_kind == "non_git" and (
        selection.get("confirmed") is not True
        or bool(selection.get("unclassified_files"))
    ):
        questions.append(
            {
                "id": "classify_non_git_files",
                "question": (
                    "Classify every current file as Created by Claude or Exclude, "
                    "then confirm the complete end-state selection."
                ),
                "classification_flags": {
                    "created_by_claude": "--created-by-claude",
                    "exclude": "--exclude-file",
                    "confirmation": "--confirm-file-selection",
                },
                "files": list(selection.get("candidates") or []),
                "unclassified_files": list(selection.get("unclassified_files") or []),
                "classifications": dict(selection.get("classifications") or {}),
            }
        )
    if (
        selection.get("requires_empty_beginning_confirmation") is True
        and selection.get("empty_starting_directory_confirmed") is not True
    ):
        questions.append(
            {
                "id": "confirm_empty_beginning",
                "question": (
                    "Confirm that the Non-Git beginning state was an empty directory. "
                    "If any file existed before Claude, stop: non-empty Non-Git "
                    "beginning states are unsupported."
                ),
                "confirmation_flag": "--confirm-empty-beginning",
            }
        )
    return questions


def _blocking_reasons(
    baseline: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> list[str]:
    if selection.get("complete") is not True:
        return []
    if baseline.get("kind") not in {"git_commit", "empty_directory"}:
        return ["The selected historical baseline is not runnable."]
    return []


def _configuration(
    context: Mapping[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    replay = context["replay"]
    baseline = context["baseline"]
    capabilities = context["capabilities"]
    review_decisions = replay.get("review_decisions")
    review_decisions = review_decisions if isinstance(review_decisions, Mapping) else {}
    return {
        "task": {
            "imported_thread_id": replay.get("imported_thread_id"),
            "original_session_id": replay.get("session_id"),
            "task_scope": replay.get("task_scope", "whole_thread"),
            "user_message_count": replay.get("user_message_count", 1),
            "request": replay.get("request"),
            "project": replay.get("project_dir"),
            "original_project": replay.get("original_project_dir"),
        },
        "beginning_state": {
            "kind": baseline.get("beginning_kind"),
            "commit": (
                baseline.get("commit") if baseline.get("beginning_kind") == "git" else None
            ),
        },
        "ending_state": {
            "kind": baseline.get("ending_kind"),
            "commit": (
                baseline.get("ending_commit")
                if baseline.get("ending_kind") == "git"
                else None
            ),
        },
        "baseline": {
            "kind": baseline.get("kind"),
            "beginning_kind": baseline.get("beginning_kind"),
            "ending_kind": baseline.get("ending_kind"),
            "proposed_kind": baseline.get("proposed_kind"),
            "repository": baseline.get("repository"),
            "attribution_root": baseline.get("attribution_root"),
            "repository_resolution": baseline.get("repository_resolution"),
            "commit": baseline.get("commit"),
            "ending_commit": baseline.get("ending_commit"),
            "ending_commit_confidence": baseline.get("ending_commit_confidence"),
            "confidence": baseline.get("confidence"),
            "historical_working_tree_state": baseline.get("working_tree_state"),
            "current_working_tree_state": baseline.get("current_working_tree_state"),
            "affected_files": list((baseline.get("working_tree") or {}).get("affected_files", [])),
        },
        "file_selection": context["file_selection"],
        "model": context["model"],
        "runtime": {
            "timeout_seconds": context["timeout_seconds"],
            "sandbox_policy": context["sandbox_policy"],
        },
        "capabilities": {
            "observed_count": len(capabilities.get("items") or []),
            "guidance_actions": list(capabilities.get("resolution_actions") or []),
        },
        "user_decisions": {
            "file_selection_confirmed": bool(context["file_selection"].get("confirmed")),
            "repository_selection_confirmed": bool(
                getattr(args, "confirm_repository_selection", False)
            ),
            "transcript_overridden": bool(
                review_decisions.get("transcript_overridden")
            ),
            "baseline_overridden": bool(baseline.get("reviewed_override")),
            "ending_commit_overridden": bool(
                baseline.get("ending_commit_reviewed_override")
            ),
            "request_overridden": bool(review_decisions.get("request_overridden")),
            "empty_starting_directory_confirmed": bool(
                context["file_selection"].get("empty_starting_directory_confirmed")
            ),
        },
    }


def _prepare_context(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise BakeoffError("The execution timeout cannot exceed 14,400 seconds.")
    replay, repository_resolution, repository_blockers = _resolved_replay_repository(
        args,
        _selected_replay(args),
    )
    baseline, file_selection = _classified_baseline(
        args,
        {
            **_baseline(args, replay),
            "repository_resolution": repository_resolution,
        },
    )
    model = _selected_model(args.model, args.model_cache)
    try:
        capabilities = _capabilities(replay)
    except BakeoffError as error:
        capabilities = {
            "items": [],
            "resolution_actions": [],
            "limitations": [str(error)],
        }
    context = {
        "replay": replay,
        "baseline": baseline,
        "file_selection": file_selection,
        "model": model,
        "capabilities": capabilities,
        "prompt": _prompt(replay),
        "timeout_seconds": args.timeout_seconds,
        "sandbox_policy": {"type": "workspaceWrite", "networkAccess": True},
    }
    questions = _selection_questions(file_selection)
    blocking_reasons = [
        *repository_blockers,
        *_blocking_reasons(baseline, file_selection),
    ]
    runnable = baseline.get("kind") in {"git_commit", "empty_directory"}
    historical_candidate: dict[str, Any] | None = None
    if not questions and runnable and not blocking_reasons:
        try:
            historical_candidate = _serialize_historical_candidate(
                *_historical_candidate(context)
            )
        except BakeoffError as error:
            blocking_reasons.append(str(error))
    context["historical_candidate"] = historical_candidate
    if repository_blockers:
        status = "blocked"
    elif questions:
        status = "needs_user_input"
    elif runnable and not blocking_reasons:
        status = "ready_for_approval"
    else:
        status = "blocked"
    return {
        "status": status,
        "requires_approval": True,
        "can_run": status == "ready_for_approval",
        "questions": questions,
        "blocking_reasons": blocking_reasons,
        "configuration": _configuration(context, args),
        "approval_prompt": (
            "Approve one native Codex implementation using this configuration?"
            if status == "ready_for_approval"
            else None
        ),
        **context,
    }


def _command_prepare(args: argparse.Namespace) -> dict[str, Any]:
    prepared = _prepare_context(args)
    result = {
        key: value
        for key, value in prepared.items()
        if key != "historical_candidate"
    }
    result["prepared_configuration_sha256"] = _canonical_json_sha256(
        prepared["configuration"]
    )
    historical_candidate = prepared.get("historical_candidate")
    result["historical_result_sha256"] = (
        _canonical_json_sha256(historical_candidate)
        if isinstance(historical_candidate, Mapping)
        else None
    )
    return result


def _command_run(args: argparse.Namespace) -> dict[str, Any]:
    if not bool(getattr(args, "approve", False)):
        raise BakeoffError(
            "Run `prepare`, review its configuration, obtain explicit user approval, "
            "then rerun with --approve. No Codex task was requested."
        )
    prepared = _prepare_context(args)
    if prepared["questions"]:
        pending = " ".join(
            str(item.get("question")) for item in prepared["questions"] if isinstance(item, Mapping)
        )
        raise BakeoffError(f"File classification is still required before approval: {pending}")
    if prepared["status"] != "ready_for_approval":
        detail = " ".join(prepared.get("blocking_reasons") or [])
        raise BakeoffError(
            "The prepared configuration is not runnable; no Codex task was "
            f"requested. {detail}".strip()
        )
    expected_prepared_configuration_sha256 = getattr(
        args,
        "expected_prepared_configuration_sha256",
        None,
    )
    if expected_prepared_configuration_sha256 is not None:
        prepared_configuration_sha256 = _canonical_json_sha256(
            prepared["configuration"]
        )
        if (
            not isinstance(expected_prepared_configuration_sha256, str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                expected_prepared_configuration_sha256,
            )
            is None
            or not secrets.compare_digest(
                prepared_configuration_sha256,
                expected_prepared_configuration_sha256,
            )
        ):
            raise BakeoffError(
                "The prepared configuration changed or its digest is invalid; run "
                "`prepare` again, review the current configuration, and approve it "
                "again. No Codex task was requested."
            )
    historical_candidate = prepared.get("historical_candidate")
    if not isinstance(historical_candidate, Mapping):
        raise BakeoffError(
            "The historical Claude candidate was not frozen; no Codex task was requested."
        )
    historical_result_sha256 = _canonical_json_sha256(historical_candidate)
    expected_historical_result_sha256 = getattr(
        args,
        "expected_historical_result_sha256",
        None,
    )
    if expected_historical_result_sha256 is not None and (
        not isinstance(expected_historical_result_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected_historical_result_sha256) is None
        or not secrets.compare_digest(
            historical_result_sha256,
            expected_historical_result_sha256,
        )
    ):
        raise BakeoffError(
            "The historical Claude result changed after preparation; prepare and "
            "approve it again. No Codex task was requested."
        )
    run_directory = _new_run_directory(args.run_root)
    target = _target_for_baseline(
        prepared["baseline"],
        run_directory=run_directory,
    )
    context = {
        key: prepared[key]
        for key in (
            "replay",
            "baseline",
            "file_selection",
            "model",
            "capabilities",
            "prompt",
            "timeout_seconds",
            "sandbox_policy",
        )
    }
    historical_result_path = run_directory / "historical-result.json"
    _write_canonical_json(
        historical_result_path,
        historical_candidate,
    )
    record = {
        "schema_version": 1,
        "status": "awaiting_native_task",
        "run_directory": str(run_directory),
        "configuration": prepared["configuration"],
        "approval": {
            "approved": True,
            "approved_at": datetime.now().astimezone().isoformat(),
        },
        **context,
        "target": target,
        "baseline_materialization": None,
        "historical_result": {
            "schema_version": 1,
            "path": historical_result_path.name,
            "sha256": historical_result_sha256,
        },
    }
    _write_json(run_directory / "run.json", record)
    return {
        "status": "native_task_required",
        "run_directory": str(run_directory),
        "task_request": {
            "purpose": "implementation",
            "run_directory": str(run_directory),
            "model": context["model"],
            "prompt": context["prompt"],
            "target": target,
            "timeout_seconds": context["timeout_seconds"],
            "baseline_materialization": None,
        },
        "review_opened": False,
    }


def _run_directory(raw: Path) -> Path:
    path = raw.expanduser().resolve()
    if not path.is_dir() or not (path / "run.json").is_file():
        raise BakeoffError("The bakeoff run directory is unavailable.")
    return path


def _command_collect_native_result(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    evaluator = getattr(args, "evaluator", None)
    normalization_for = getattr(args, "normalization_for", None)
    if evaluator and normalization_for:
        raise BakeoffError("--evaluator and --normalization-for cannot be used together.")
    try:
        result = _execution().collect_native_task_result(
            thread_id=args.thread_id,
            worktree=args.worktree,
            rollout_path=args.rollout,
            evaluator=evaluator,
        )
    except Exception as error:
        raise BakeoffError(_redact(error)) from error
    if normalization_for:
        result["normalization_for"] = normalization_for
        path = run_directory / "reviews" / f"normalize-{normalization_for}-{args.thread_id}.json"
    elif evaluator:
        path = run_directory / "reviews" / f"{evaluator}-{args.thread_id}.json"
    else:
        path = run_directory / "native-result.json"
    _write_json(path, result)
    return {
        "status": "collected",
        "native_result_path": str(path),
        "thread_id": args.thread_id,
        "worktree": result.get("worktree"),
        "model": result.get("model"),
        "normalization_for": normalization_for,
    }


def _historical_candidate(
    run: Mapping[str, Any],
) -> tuple[Any | None, dict[str, Any], str]:
    replay = run["replay"]
    if (
        not isinstance(replay.get("source_path"), str)
        or not replay["source_path"].strip()
        or not isinstance(replay.get("message_uuid"), str)
        or not replay["message_uuid"].strip()
    ):
        raise BakeoffError(
            "The source transcript path and original user-message UUID must be "
            "reviewed before capturing the historical Claude result."
        )
    baseline = run["baseline"]
    selection = run.get("file_selection")
    selection = selection if isinstance(selection, Mapping) else {}
    repository = Path(str(baseline["repository"]))
    if selection.get("source_kind") == "non_git":
        recovery = {
            "provenance": "user_classification_pending_capture",
            "baseline_commit": None,
            "baseline_kind": baseline.get("kind"),
            "commit": None,
            "diff": None,
            "changed_files": [],
            "evidence": [],
            "limitations": [],
        }
    else:
        try:
            recovery_replay = {
                **replay,
                "project_dir": str(repository),
                "project_dirs": [str(repository)],
            }
            recovery = _discovery().recover_historical_solution(
                recovery_replay,
                baseline.get("commit"),
                baseline_kind=baseline.get("kind", "git_commit"),
                ending_commit=(
                    baseline.get("ending_commit")
                    if baseline.get("ending_kind") == "git"
                    else None
                ),
            )
        except Exception as error:
            recovery = {
                "provenance": "unavailable",
                "diff": None,
                "limitations": [_redact(error)],
            }
    diff = recovery.get("diff")
    if selection.get("source_kind") == "non_git":
        try:
            diff, changed = _file_selection().build_directory_candidate_patch(selection)
            recovery = {
                "provenance": "user_classified_current_files",
                "baseline_commit": None,
                "baseline_kind": baseline.get("kind"),
                "commit": None,
                "diff": diff,
                "changed_files": list(changed),
                "evidence": [
                    {
                        "source": "explicit_user_file_classification",
                        "classification_confirmed": bool(selection.get("confirmed")),
                    }
                ],
                "limitations": [],
                "file_selection": dict(selection),
            }
        except Exception as error:
            raise BakeoffError(
                f"The live classified Claude files could not be captured: {_redact(error)}"
            ) from error
    elif selection.get("source_kind") == "git" and selection.get("working_tree_state") == "dirty":
        selected_changes = selection.get("claude_output_changes")
        selected_changes = selected_changes if isinstance(selected_changes, list) else []
        if selected_changes:
            try:
                diff, changed = _file_selection().build_git_candidate_patch(
                    repository=repository,
                    baseline_commit=(
                        str(baseline["commit"])
                        if isinstance(baseline.get("commit"), str)
                        else None
                    ),
                    baseline_kind=str(baseline.get("kind") or "git_commit"),
                    recovered_patch=diff if isinstance(diff, str) else None,
                    selection=selection,
                )
                recovery = {
                    **recovery,
                    "provenance": (
                        f"{recovery.get('provenance', 'historical_result')}"
                        "_plus_user_selected_working_tree"
                    ),
                    "diff": diff,
                    "changed_files": list(changed),
                    "file_selection": dict(selection),
                }
            except Exception as error:
                raise BakeoffError(
                    f"The selected live Git changes could not be captured: {_redact(error)}"
                ) from error
        else:
            recovery = {
                **recovery,
                "file_selection": dict(selection),
            }
    if not isinstance(diff, str) or not diff.strip():
        raise BakeoffError(
            "No attributable historical Claude patch could be captured; "
            "the comparison cannot be completed."
        )
    try:
        final_response = _discovery().recover_historical_final_response(
            replay["source_path"],
            replay["message_uuid"],
            whole_thread=replay.get("task_scope") == "whole_thread",
        )
    except Exception:
        final_response = ""
    candidate = (
        _execution().CandidateSolution(
            provider="claude",
            diff=diff,
            model=str(replay.get("claude_model") or "unknown"),
            final_response=final_response or "",
        )
        if isinstance(diff, str)
        else None
    )
    return candidate, recovery, final_response or ""


def _serialize_historical_candidate(
    candidate: Any | None,
    recovery: Mapping[str, Any],
    final_response: str,
) -> dict[str, Any]:
    if candidate is None:
        raise BakeoffError("The historical Claude candidate could not be serialized.")
    return {
        "schema_version": 1,
        "candidate": _jsonable(candidate),
        "recovery": _jsonable(recovery),
        "final_response": final_response,
    }


def _historical_candidate_for_completion(
    run: Mapping[str, Any],
    run_directory: Path,
) -> tuple[Any | None, dict[str, Any], str]:
    if "historical_result" not in run:
        return _historical_candidate(run)
    metadata = run.get("historical_result")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("schema_version") != 1
        or metadata.get("path") != "historical-result.json"
        or not isinstance(metadata.get("sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"]) is None
    ):
        raise BakeoffError("The frozen historical Claude artifact metadata is invalid.")
    artifact_path = run_directory / "historical-result.json"
    try:
        serialized = artifact_path.read_bytes()
    except OSError as error:
        raise BakeoffError(
            f"Cannot read the frozen historical Claude artifact: {_redact(error)}"
        ) from error
    observed_digest = hashlib.sha256(serialized).hexdigest()
    if not secrets.compare_digest(observed_digest, metadata["sha256"]):
        raise BakeoffError("The frozen historical Claude artifact digest does not match.")
    try:
        frozen = json.loads(serialized.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BakeoffError(
            f"Cannot read the frozen historical Claude artifact: {_redact(error)}"
        ) from error
    if not isinstance(frozen, Mapping) or frozen.get("schema_version") != 1:
        raise BakeoffError("The frozen historical Claude candidate is invalid.")
    raw_candidate = frozen.get("candidate")
    raw_recovery = frozen.get("recovery")
    final_response = frozen.get("final_response")
    if (
        not isinstance(raw_candidate, Mapping)
        or not isinstance(raw_candidate.get("diff"), str)
        or not isinstance(raw_recovery, Mapping)
        or not isinstance(final_response, str)
    ):
        raise BakeoffError("The frozen historical Claude candidate is invalid.")
    candidate = _execution().CandidateSolution(
        provider="claude",
        diff=raw_candidate["diff"],
        model=str(raw_candidate.get("model") or "unknown"),
        final_response=final_response,
    )
    return candidate, dict(raw_recovery), final_response


def _codex_candidate(
    run: Mapping[str, Any],
    native: Mapping[str, Any],
) -> tuple[Any, str, tuple[str, ...]]:
    worktree = Path(str(native.get("worktree") or "")).expanduser()
    baseline = run["baseline"]
    selection = run.get("file_selection")
    selection = selection if isinstance(selection, Mapping) else {}
    try:
        if selection.get("source_kind") == "non_git" or baseline.get("kind") in {
            "empty_directory",
            "classified_directory",
        }:
            if baseline.get("kind") == "classified_directory":
                raw_expected = baseline.get("registered_baseline_project")
                if not isinstance(raw_expected, str) or not raw_expected:
                    raise BakeoffError("The registered non-Git baseline project is missing.")
                expected = _canonical_parent_path(raw_expected)
                observed = _canonical_parent_path(worktree)
                if observed != expected:
                    raise BakeoffError(
                        "The observed Codex workspace does not match the registered "
                        "non-Git baseline project."
                    )
            diff, changed = _file_selection().build_directory_result_patch(
                selection,
                worktree,
            )
        else:
            diff, changed = _execution().capture_candidate_diff(
                worktree,
                baseline_commit=(
                    str(baseline["commit"]) if baseline.get("kind") == "git_commit" else None
                ),
            )
    except BakeoffError:
        raise
    except Exception as error:
        raise BakeoffError(_redact(error)) from error
    candidate = _execution().CandidateSolution(
        provider="codex",
        diff=diff,
        model=str(native.get("model") or run.get("model") or "unknown"),
        final_response=str(native.get("final_output") or ""),
    )
    return candidate, diff, changed


def _usage_records(
    run: Mapping[str, Any],
    native: Mapping[str, Any],
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    replay = run["replay"]
    historical = replay.get("historical_usage")
    claude_usage: tuple[Any, ...] = ()
    if isinstance(historical, Mapping):
        claude_usage = (
            _execution().usage_from_payload(
                historical,
                provider="anthropic",
                model=str(replay.get("claude_model") or "unknown"),
                message_id=f"historical:{replay.get('session_id', 'unknown')}",
            ),
        )
    raw_native_usage = native.get("usage")
    codex_usage: tuple[Any, ...] = ()
    if isinstance(raw_native_usage, Mapping):
        codex_usage = (
            _execution().usage_from_payload(
                raw_native_usage,
                provider="openai",
                model=str(native.get("model") or run.get("model") or "unknown"),
                message_id=f"native:{native.get('thread_id', 'unknown')}",
            ),
        )
    return claude_usage, codex_usage


def _command_complete_run(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    run = _load_json(run_directory / "run.json", label="the bakeoff run")
    native_path = args.native_result.expanduser().resolve()
    native = _load_json(native_path, label="the native result")
    claude_candidate, recovery, historical_final = _historical_candidate_for_completion(
        run,
        run_directory,
    )
    codex_candidate, codex_diff, changed = _codex_candidate(run, native)
    claude_usage, codex_usage = _usage_records(run, native)
    limitations = list(recovery.get("limitations", []))
    if claude_candidate is None:
        limitations.append("No attributable historical Claude patch was recovered.")
    if run["baseline"].get("source_kind") == "non_git":
        limitations.append(
            "Generated or dependency trees and non-representable or potentially "
            "sensitive new files are excluded symmetrically from both candidates."
        )
    report = _execution().generate_report(
        original_request=str(run["replay"].get("request") or ""),
        baseline=run["baseline"],
        parity_report=run["capabilities"],
        claude_candidate=claude_candidate,
        codex_candidate=codex_candidate,
        claude_usage=claude_usage,
        codex_usage=codex_usage,
        codex_result=native,
        limitations=limitations,
    )
    report.update(
        {
            "run_directory": str(run_directory),
            "imported_thread_id": run["replay"].get("imported_thread_id"),
            "claude_session_id": run["replay"].get("session_id"),
            "historical_model_request_seconds": run["replay"].get(
                "historical_model_request_seconds"
            ),
            "historical_wall_clock_seconds": run["replay"].get("historical_wall_clock_seconds"),
            "historical_solution": recovery,
            "historical_final_response": historical_final,
            "codex_changed_files": list(changed),
            "codex_diff": codex_diff,
        }
    )
    _write_report(run_directory, report)
    _write_json(
        run_directory / "run.json",
        {**run, "status": "completed", "native_result": str(native_path)},
    )
    return {
        "status": "completed",
        "run_directory": str(run_directory),
        "report_html": str(run_directory / "report.html"),
        "report_json": str(run_directory / "report.json"),
        "review_opened": False,
    }


def _report_candidates(report: Mapping[str, Any]) -> tuple[Any | None, Any | None]:
    raw = report.get("candidates")
    raw = raw if isinstance(raw, Mapping) else {}
    verification = report.get("verification")
    verification = verification if isinstance(verification, Mapping) else {}
    candidates: list[Any | None] = []
    for provider in ("claude", "codex"):
        item = raw.get(provider)
        if not isinstance(item, Mapping):
            candidates.append(None)
            continue
        evidence = _verification().verification_evidence_for_provider(verification, provider)
        candidates.append(
            _execution().CandidateSolution(
                provider=provider,
                diff=str(item.get("diff") or ""),
                model=str(item.get("model") or "unknown"),
                verification=evidence,
                verification_results_available=(
                    _verification().has_comparable_verification_results(verification)
                ),
                final_response=str(item.get("final_response") or ""),
            )
        )
    return candidates[0], candidates[1]


def _command_verify(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    report = _load_json(run_directory / "report.json", label="the bakeoff report")
    claude, codex = _report_candidates(report)
    try:
        verification = _verification().verify_candidates(
            baseline=report["baseline"],
            candidate_patches={
                "claude": claude.diff if claude else None,
                "codex": codex.diff if codex else None,
            },
        )
    except Exception as error:
        raise BakeoffError(_redact(error)) from error
    _write_json(run_directory / "verification.json", verification)
    report["verification"] = verification
    _write_report(run_directory, report)
    return {
        "status": verification.get("status"),
        "verification": verification,
        "report_html": str(run_directory / "report.html"),
        "report_json": str(run_directory / "report.json"),
    }


def _available_evaluators(model: str) -> list[dict[str, Any]]:
    denied = os.environ.get("CODEX_BAKEOFF_DENIED_HOSTS", "api.anthropic.com,claude.ai")
    entries = _execution().check_evaluator_availability(
        denied_hosts=[item.strip() for item in denied.split(",") if item.strip()],
        codex_model=model,
    )
    return [item for item in entries if item.get("available") is True]


def _command_reviewers(args: argparse.Namespace) -> dict[str, Any]:
    catalog = discover_codex_models(args.model_cache)
    recommended = next(
        (item["id"] for item in catalog["options"] if item.get("recommended")),
        catalog["options"][0]["id"] if catalog["options"] else "gpt-5.6-sol",
    )
    entries = _execution().check_evaluator_availability(codex_model=recommended)
    return {
        "status": ("available" if any(item["available"] for item in entries) else "unavailable"),
        "evaluators": entries,
    }


def _command_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    report = _load_json(run_directory / "report.json", label="the bakeoff report")
    if not report.get("verification"):
        _command_verify(argparse.Namespace(run_dir=run_directory))
        report = _load_json(run_directory / "report.json", label="the bakeoff report")
    claude, codex = _report_candidates(report)
    if claude is None or codex is None:
        report["evaluation"] = {
            "status": "unavailable",
            "limitations": ["Both attributable candidates are required for review."],
        }
        _write_report(run_directory, report)
        return {"status": "unavailable", "task_requests": []}
    run = _load_json(run_directory / "run.json", label="the bakeoff run")
    evaluators = _available_evaluators(str(run.get("model") or "gpt-5.6-sol"))
    if args.evaluator:
        allowed = set(args.evaluator)
        evaluators = [item for item in evaluators if item.get("id") in allowed]
    requests = _execution().prepare_review(
        run_directory=run_directory,
        original_request=str(report.get("original_request") or ""),
        candidates=(claude, codex),
        evaluators=evaluators,
    )
    _write_json(
        run_directory / "review.json",
        {"status": "awaiting_native_tasks", "task_requests": requests},
    )
    return {
        "status": "native_task_required" if requests else "unavailable",
        "run_directory": str(run_directory),
        "task_requests": requests,
    }


def _command_collect_native_results(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    results = [
        _load_json(path.expanduser().resolve(), label="a native reviewer result")
        for path in args.native_result
    ]
    path = run_directory / "reviews" / "results.json"
    _write_json(path, {"results": results})
    return {
        "status": "collected",
        "native_results_path": str(path),
        "result_count": len(results),
    }


def _command_complete_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    report = _load_json(run_directory / "report.json", label="the bakeoff report")
    combined = _load_json(
        args.native_results.expanduser().resolve(),
        label="the native reviewer results",
    )
    raw_results = combined.get("results")
    if not isinstance(raw_results, list):
        raise BakeoffError("The reviewer results file has no result list.")
    normalized_results: dict[str, dict[str, Any]] = {}
    for path in getattr(args, "normalized_result", None) or ():
        result = _load_json(
            path.expanduser().resolve(),
            label="a normalized reviewer result",
        )
        normalization_for = result.get("normalization_for")
        if not isinstance(normalization_for, str) or not normalization_for:
            raise BakeoffError("A normalized reviewer result has no normalization target.")
        if normalization_for in normalized_results:
            raise BakeoffError(f"More than one normalized result targets {normalization_for}.")
        normalized_results[normalization_for] = result
    reviews: list[dict[str, Any]] = []
    normalization_requests: list[dict[str, Any]] = []
    for result in raw_results:
        if not isinstance(result, Mapping):
            continue
        evaluator = str(result.get("evaluator") or "")
        model = str(result.get("model") or "")
        raw_ballot = str(result.get("final_output") or "")
        try:
            ballot = _execution().parse_review_ballot(raw_ballot)
        except Exception as error:
            normalized = normalized_results.get(evaluator)
            if normalized is None:
                normalization_requests.append(
                    _execution().prepare_review_normalization(
                        evaluator=evaluator,
                        model=model,
                        raw_ballot=raw_ballot,
                    )
                )
                reviews.append(
                    {
                        "evaluator": evaluator,
                        "model": model,
                        "status": "awaiting_normalization",
                        "raw_ballot": raw_ballot,
                        "normalization": {
                            "required": True,
                            "status": "awaiting_native_task",
                            "validation_error": _redact(error),
                        },
                    }
                )
                continue
            normalized_response = str(normalized.get("final_output") or "")
            try:
                ballot = _execution().parse_review_ballot(normalized_response)
            except Exception as normalization_error:
                reviews.append(
                    {
                        "evaluator": evaluator,
                        "model": model,
                        "status": "invalid",
                        "error": _redact(normalization_error),
                        "raw_ballot": raw_ballot,
                        "normalization_response": normalized_response,
                        "normalization": {
                            "required": True,
                            "status": "failed",
                            "model": normalized.get("model"),
                            "thread_id": normalized.get("thread_id"),
                            "elapsed_seconds": normalized.get("elapsed_seconds"),
                            "usage": normalized.get("usage"),
                            "initial_validation_error": _redact(error),
                            "validation_error": _redact(normalization_error),
                        },
                    }
                )
                continue
            reviews.append(
                {
                    "evaluator": evaluator,
                    "model": model,
                    "status": "completed",
                    "ballot": ballot,
                    "raw_ballot": raw_ballot,
                    "normalized_ballot": ballot,
                    "normalization_response": normalized_response,
                    "normalization": {
                        "required": True,
                        "status": "completed",
                        "model": normalized.get("model"),
                        "thread_id": normalized.get("thread_id"),
                        "elapsed_seconds": normalized.get("elapsed_seconds"),
                        "usage": normalized.get("usage"),
                        "initial_validation_error": _redact(error),
                    },
                }
            )
            continue
        reviews.append(
            {
                "evaluator": evaluator,
                "model": model,
                "status": "completed",
                "ballot": ballot,
                "raw_ballot": raw_ballot,
                "normalized_ballot": ballot,
                "normalization": {
                    "required": False,
                    "status": "not_required",
                },
            }
        )
    if normalization_requests:
        pending = {
            "status": "awaiting_normalization",
            "candidate_mapping": {"A": "claude", "B": "codex"},
            "reviews": reviews,
            "totals": {"A": 0, "B": 0},
            "task_requests": normalization_requests,
        }
        report["evaluation"] = pending
        _write_report(run_directory, report)
        _write_json(run_directory / "review.json", pending)
        return {
            "status": "native_task_required",
            "purpose": "review_normalization",
            "run_directory": str(run_directory),
            "task_requests": normalization_requests,
            "report_html": str(run_directory / "report.html"),
            "report_json": str(run_directory / "report.json"),
            "review_opened": False,
        }
    valid = [item for item in reviews if item.get("status") == "completed"]
    aggregate = (
        _execution().aggregate_reviews(valid)
        if valid
        else {
            "reviews": reviews,
            "totals": {"A": 0, "B": 0},
        }
    )
    aggregate["status"] = "completed" if valid else "unavailable"
    aggregate["candidate_mapping"] = {"A": "claude", "B": "codex"}
    aggregate["all_results"] = reviews
    report["evaluation"] = aggregate
    _write_report(run_directory, report)
    _write_json(run_directory / "review.json", aggregate)
    return {
        "status": aggregate["status"],
        "evaluation": aggregate,
        "report_html": str(run_directory / "report.html"),
        "report_json": str(run_directory / "report.json"),
        "review_opened": False,
    }


def _command_report(args: argparse.Namespace) -> dict[str, Any]:
    run_directory = _run_directory(args.run_dir)
    report_path = run_directory / "report.json"
    if not report_path.is_file():
        raise BakeoffError("The bakeoff report is not available yet.")
    report = _load_json(report_path, label="the bakeoff report")
    rendered_report = dict(report)
    run_path = run_directory / "run.json"
    if run_path.is_file():
        run = _load_json(run_path, label="the bakeoff run")
        replay = run.get("replay")
        if isinstance(replay, Mapping):
            rendered_report.setdefault(
                "historical_model_request_seconds",
                replay.get("historical_model_request_seconds"),
            )
            rendered_report.setdefault(
                "historical_wall_clock_seconds",
                replay.get("historical_wall_clock_seconds"),
            )
    _write_report_html(run_directory, rendered_report)
    return {
        "status": "ok",
        "run_directory": str(run_directory),
        "report_html": str(run_directory / "report.html"),
        "report_json": str(report_path),
        "review_opened": False,
    }


def _command_latest(args: argparse.Namespace) -> dict[str, Any]:
    root = args.run_root.expanduser().resolve()
    runs = (
        sorted(
            (path for path in root.iterdir() if path.is_dir() and (path / "run.json").is_file()),
            reverse=True,
        )
        if root.is_dir()
        else []
    )
    if not runs:
        raise BakeoffError("No bakeoff run is available.")
    return _command_report(argparse.Namespace(run_dir=runs[0]))


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def _add_session(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--imported-thread-id", required=True)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)


def _add_baseline(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path)
    parser.add_argument(
        "--beginning-kind",
        choices=("git", "non_git"),
        help="Use an explicitly reviewed Git or Non-Git beginning state.",
    )
    parser.add_argument(
        "--ending-kind",
        choices=("git", "non_git"),
        help="Use an explicitly reviewed Git or Non-Git end state.",
    )
    parser.add_argument(
        "--baseline-commit",
        help="Use an explicitly reviewed historical Git commit.",
    )
    parser.add_argument(
        "--ending-commit",
        help="Use an explicitly reviewed historical ending Git commit.",
    )
    parser.add_argument(
        "--confirm-empty-beginning",
        action="store_true",
        help="Confirm that the Non-Git beginning state was an empty directory.",
    )
    parser.add_argument(
        "--confirm-repository-selection",
        action="store_true",
        help="Confirm the reviewed repository when automatic resolution was ambiguous.",
    )
    parser.add_argument(
        "--claude-output-file",
        action="append",
        help=(
            "Git only: select one current staged, unstaged, or untracked "
            "change as part of Claude's output. Repeat for multiple changes."
        ),
    )
    parser.add_argument(
        "--created-by-claude",
        action="append",
        help="Non-Git only: classify a current file as created by Claude.",
    )
    parser.add_argument(
        "--exclude-file",
        action="append",
        help="Non-Git only: exclude a current file or surfaced generated tree.",
    )
    parser.add_argument(
        "--confirm-file-selection",
        action="store_true",
        help=(
            "Confirm the complete Git working-tree selection or Non-Git end-state "
            "file classification."
        ),
    )


def _add_preparation(parser: argparse.ArgumentParser) -> None:
    _add_session(parser)
    _add_baseline(parser)
    parser.add_argument(
        "--source-path",
        type=Path,
        help="Use an explicitly reviewed source transcript path.",
    )
    parser.add_argument(
        "--message-uuid",
        help="Use an explicitly reviewed original user-message UUID.",
    )
    request = parser.add_mutually_exclusive_group()
    request.add_argument("--request", help="Override the replay request after manual review.")
    request.add_argument(
        "--request-stdin",
        action="store_true",
        help="Read the manually reviewed replay request from standard input.",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-cache", type=Path, default=DEFAULT_MODEL_CACHE)
    parser.add_argument("--timeout-seconds", type=_positive_int, default=1800)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    sessions = commands.add_parser("sessions")
    sessions.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sessions.add_argument("--limit", type=_positive_int, default=10)
    sessions.add_argument("--offset", type=int, default=0)
    _add_json(sessions)

    replay = commands.add_parser("replay")
    _add_session(replay)
    _add_json(replay)

    capabilities = commands.add_parser("capabilities")
    _add_session(capabilities)
    _add_json(capabilities)

    baseline = commands.add_parser("baseline")
    _add_session(baseline)
    _add_baseline(baseline)
    _add_json(baseline)

    models = commands.add_parser("models")
    models.add_argument("--model-cache", type=Path, default=DEFAULT_MODEL_CACHE)
    _add_json(models)

    reviewers = commands.add_parser("reviewers")
    reviewers.add_argument("--model-cache", type=Path, default=DEFAULT_MODEL_CACHE)
    _add_json(reviewers)

    prepare = commands.add_parser("prepare")
    _add_preparation(prepare)
    _add_json(prepare)

    run = commands.add_parser("run")
    _add_preparation(run)
    run.add_argument(
        "--approve",
        action="store_true",
        help="Record explicit user approval of the reviewed prepare output.",
    )
    run.add_argument(
        "--expected-historical-result-sha256",
        help="Require the approved prepare-time historical result digest.",
    )
    run.add_argument(
        "--expected-prepared-configuration-sha256",
        help="Require the exact approved prepare-time configuration digest.",
    )
    run.add_argument("--run-root", type=Path)
    _add_json(run)

    collect = commands.add_parser("collect-native-result")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--thread-id", required=True)
    collect.add_argument("--worktree", type=Path, required=True)
    collect.add_argument("--rollout", type=Path)
    collect.add_argument("--evaluator", choices=("codex", "claude"))
    collect.add_argument(
        "--normalization-for",
        choices=("codex", "claude"),
        help="Record one schema-normalization task result for this evaluator.",
    )
    _add_json(collect)

    complete = commands.add_parser("complete-run")
    complete.add_argument("--run-dir", type=Path, required=True)
    complete.add_argument("--native-result", type=Path, required=True)
    _add_json(complete)

    verify = commands.add_parser("verify")
    verify.add_argument("--run-dir", type=Path, required=True)
    _add_json(verify)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--evaluator", action="append", choices=("codex", "claude"))
    _add_json(evaluate)

    collect_reviews = commands.add_parser("collect-native-results")
    collect_reviews.add_argument("--run-dir", type=Path, required=True)
    collect_reviews.add_argument("--native-result", type=Path, action="append", required=True)
    _add_json(collect_reviews)

    complete_reviews = commands.add_parser("complete-evaluation")
    complete_reviews.add_argument("--run-dir", type=Path, required=True)
    complete_reviews.add_argument("--native-results", type=Path, required=True)
    complete_reviews.add_argument(
        "--normalized-result",
        type=Path,
        action="append",
        help="A collected result from an optional reviewer normalization task.",
    )
    _add_json(complete_reviews)

    report = commands.add_parser("report")
    report.add_argument("--run-dir", type=Path, required=True)
    _add_json(report)

    latest = commands.add_parser("latest")
    latest.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    _add_json(latest)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "sessions":
        if args.offset < 0:
            raise BakeoffError("The session offset cannot be negative.")
        return _command_sessions(args)
    if args.command == "replay":
        return {"status": "ok", "replay": _selected_replay(args)}
    if args.command == "capabilities":
        replay = _selected_replay(args)
        return {"status": "ok", **_capabilities(replay)}
    if args.command == "baseline":
        replay, repository_resolution, repository_blockers = _resolved_replay_repository(
            args,
            _selected_replay(args),
        )
        baseline, selection = _classified_baseline(
            args,
            {
                **_baseline(args, replay),
                "repository_resolution": repository_resolution,
            },
        )
        return {
            "status": "ok",
            "baseline": baseline,
            "file_selection": selection,
            "questions": _selection_questions(selection),
            "repository_blocking_reasons": repository_blockers,
            "blocking_reasons": [
                *repository_blockers,
                *_blocking_reasons(baseline, selection),
            ],
        }
    if args.command == "models":
        return discover_codex_models(args.model_cache)
    if args.command == "reviewers":
        return _command_reviewers(args)
    if args.command == "prepare":
        return _command_prepare(args)
    if args.command == "run":
        return _command_run(args)
    if args.command == "collect-native-result":
        if THREAD_PATTERN.fullmatch(args.thread_id) is None:
            raise BakeoffError("The native thread ID is invalid.")
        return _command_collect_native_result(args)
    if args.command == "complete-run":
        return _command_complete_run(args)
    if args.command == "verify":
        return _command_verify(args)
    if args.command == "evaluate":
        return _command_evaluate(args)
    if args.command == "collect-native-results":
        return _command_collect_native_results(args)
    if args.command == "complete-evaluation":
        return _command_complete_evaluation(args)
    if args.command == "report":
        return _command_report(args)
    if args.command == "latest":
        return _command_latest(args)
    raise BakeoffError("Unknown command.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = dispatch(args)
    except BakeoffError as error:
        payload = {"status": "error", "error": _redact(error)}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"error: {payload['error']}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
