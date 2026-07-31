#!/usr/bin/env python3
"""Lean, record-only Codex Bakeoff orchestration."""

from __future__ import annotations

import argparse
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
DEFAULT_RUN_ROOT = Path.home() / ".cache" / "codex-bakeoff" / "runs"
PRICING_PATH = PLUGIN_ROOT / "assets" / "model-pricing.json"
MAX_TIMEOUT_SECONDS = 14_400
THREAD_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}\Z")

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
            or not slug.startswith("gpt-")
            or item.get("visibility") != "list"
            or item.get("supported_in_api") is not True
            or item.get("upgrade")
            or any(
                marker in slug.casefold()
                for marker in ("preview", "alpha", "beta", "experimental", "internal")
            )
        ):
            continue
        options.append(
            {
                "id": slug,
                "label": str(item.get("display_name") or item.get("name") or slug),
                "description": str(item.get("description") or ""),
                "recommended": bool(item.get("is_default")),
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
    if not isinstance(model, str) or not model:
        raise BakeoffError("Choose a Codex model before starting the comparison.")
    catalog = discover_codex_models(model_cache)
    choices = {item["id"] for item in catalog["options"]}
    if model not in choices:
        raise BakeoffError("The selected Codex model is not locally available.")
    return model


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
    session = _selected_session(args)
    try:
        task = _discovery().build_thread_task(session)
        replay = _discovery().build_replay_spec(session, task)
    except Exception as error:
        raise BakeoffError(_redact(error)) from error
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
    try:
        inspected = _discovery().inspect_baseline(inspected_replay)
    except Exception as error:
        raise BakeoffError(_redact(error)) from error
    raw_repository = args.repo or inspected.get("repository") or replay.get("project_dir")
    if not isinstance(raw_repository, (str, Path)) or not str(raw_repository).strip():
        return inspected
    repository = _canonical_parent_path(raw_repository)
    git_root = _git_root(repository)
    if git_root is not None:
        if inspected.get("post_task_git_history") and selected_project is not None:
            return {
                **inspected,
                "kind": "unclassified_directory",
                "commit": None,
                "repository": str(selected_project),
                "attribution_root": str(selected_project),
                "source_kind": "non_git",
                "confidence": "requires_user_classification",
                "proposed_kind": None,
            }
        attribution_root = selected_project or git_root
        if inspected.get("kind") != "git_commit" or not isinstance(inspected.get("commit"), str):
            return {
                **inspected,
                "repository": str(git_root),
                "attribution_root": str(attribution_root),
                "source_kind": "git",
            }
        return {
            **inspected,
            "repository": str(git_root),
            "attribution_root": str(attribution_root),
            "source_kind": "git",
        }

    if selected_project is not None:
        repository = selected_project
    if not repository.is_dir():
        return {
            **inspected,
            "repository": str(repository),
            "attribution_root": str(repository),
            "source_kind": "non_git",
        }
    return {
        **inspected,
        "kind": "unclassified_directory",
        "commit": None,
        "repository": str(repository),
        "attribution_root": str(repository),
        "source_kind": "non_git",
        "confidence": "requires_user_classification",
        "proposed_kind": None,
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
        "Implement the complete original historical user conversation as one task. "
        "Do not inspect or modify the original Claude output directory. "
        "Ignore the contents of memory_summary.md. "
        "Do not read memory files or invoke the codex-bakeoff skill. "
        "Do not run browser-control or browser-based end-to-end tests. Use non-browser validation only. "
        "Implement only from the original request and current workspace. "
        f"Original user requests:\n{request}"
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
            return (
                {
                    **baseline,
                    "repository": selection["source_root"],
                    "attribution_root": selection["attribution_root"],
                    "current_working_tree_state": selection["working_tree_state"],
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
                allow_git_with_commits=bool(baseline.get("post_task_git_history")),
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
    if selection.get("complete") is True:
        return []
    source_kind = selection.get("source_kind")
    if source_kind == "git":
        return [
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
        ]
    if source_kind == "non_git":
        return [
            {
                "id": "classify_non_git_files",
                "question": (
                    "Classify every current file as Created by Claude or Exclude, "
                    "then confirm the complete selection and that the directory "
                    "was empty before Claude. If any file existed before Claude, "
                    "stop: non-empty non-Git baselines are unsupported."
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
        ]
    return []


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
    return {
        "task": {
            "imported_thread_id": replay.get("imported_thread_id"),
            "original_session_id": replay.get("session_id"),
            "task_scope": replay.get("task_scope", "whole_thread"),
            "user_message_count": replay.get("user_message_count", 1),
            "request": replay.get("request"),
            "project": replay.get("project_dir"),
        },
        "baseline": {
            "kind": baseline.get("kind"),
            "proposed_kind": baseline.get("proposed_kind"),
            "repository": baseline.get("repository"),
            "attribution_root": baseline.get("attribution_root"),
            "commit": baseline.get("commit"),
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
            "empty_starting_directory_confirmed": bool(
                context["file_selection"].get("empty_starting_directory_confirmed")
            ),
        },
    }


def _prepare_context(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise BakeoffError("The execution timeout cannot exceed 14,400 seconds.")
    replay = _selected_replay(args)
    baseline, file_selection = _classified_baseline(
        args,
        _baseline(args, replay),
    )
    model = _selected_model(args.model, args.model_cache)
    capabilities = _capabilities(replay)
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
    blocking_reasons = _blocking_reasons(baseline, file_selection)
    runnable = baseline.get("kind") in {"git_commit", "empty_directory"}
    if questions:
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
    return _prepare_context(args)


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
            recovery = _discovery().recover_historical_solution(
                replay,
                baseline.get("commit"),
                baseline_kind=baseline.get("kind", "git_commit"),
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
                    baseline_commit=str(baseline["commit"]),
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
    if not isinstance(diff, str):
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
    claude_candidate, recovery, historical_final = _historical_candidate(run)
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
            "Confirm the complete Git working-tree selection or non-Git file "
            "classification. For non-Git, this also confirms the directory was "
            "empty before Claude."
        ),
    )


def _add_preparation(parser: argparse.ArgumentParser) -> None:
    _add_session(parser)
    _add_baseline(parser)
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
        replay = _selected_replay(args)
        baseline, selection = _classified_baseline(
            args,
            _baseline(args, replay),
        )
        return {
            "status": "ok",
            "baseline": baseline,
            "file_selection": selection,
            "questions": _selection_questions(selection),
            "blocking_reasons": _blocking_reasons(baseline, selection),
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
