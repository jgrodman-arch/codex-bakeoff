"""Shared whole-thread and range configuration validation and CLI arguments."""

# ruff: noqa: TID251
from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

COMMIT_PATTERN = importlib.import_module("controller_constants").COMMIT_PATTERN

MAX_SELECTION_ITEMS = 2_000
MAX_REPLAY_MODELS = 8


class ControllerError(ValueError):
    pass


def _thread_id(arguments: Mapping[str, Any]) -> str:
    raw = arguments.get("thread_id") or arguments.get("imported_thread_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ControllerError("Choose an imported Claude thread.")
    return raw.strip()


def _string_list(arguments: Mapping[str, Any], key: str) -> list[str]:
    raw = arguments.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SELECTION_ITEMS:
        raise ControllerError(f"{key} must be a bounded array.")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ControllerError(f"{key} must contain non-empty paths.")
        value = item.strip()
        if value not in result:
            result.append(value)
    return result


def _replay_range(arguments: Mapping[str, Any]) -> dict[str, str]:
    bounds: dict[str, str] = {}
    for key in ("start_message_uuid", "end_message_uuid"):
        value = arguments.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ControllerError("Chunk boundaries must identify original user messages.")
        bounds[key] = value.strip()
    if bounds and len(bounds) != 2:
        raise ControllerError("Choose both the first and last user turn of the chunk.")
    if bounds and arguments.get("message_uuid") not in (None, bounds["start_message_uuid"]):
        raise ControllerError("The message ID must match the chunk's starting boundary.")
    return bounds


def _session_arguments(arguments: Mapping[str, Any]) -> list[str]:
    result = ["--imported-thread-id", _thread_id(arguments)]
    for key, value in _replay_range(arguments).items():
        result.extend(("--" + key.replace("_", "-"), value))
    return result


def _normalized_configuration(arguments: Mapping[str, Any]) -> dict[str, Any]:
    bounds = _replay_range(arguments)
    model = arguments.get("model")
    if not isinstance(model, str) or not model.strip() or "\x00" in model:
        raise ControllerError("Choose a Codex model.")
    model = model.strip()
    selected_models: list[str] | None = None
    if "models" in arguments:
        raw_models = arguments.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise ControllerError("Choose at least one Codex model.")
        if len(raw_models) > MAX_REPLAY_MODELS:
            raise ControllerError(f"Choose no more than {MAX_REPLAY_MODELS} Codex models.")
        selected_models = []
        for selected_model in raw_models:
            if (
                not isinstance(selected_model, str)
                or not selected_model.strip()
                or "\x00" in selected_model
            ):
                raise ControllerError("Choose only valid Codex models.")
            selected_model = selected_model.strip()
            if selected_model in selected_models:
                raise ControllerError("Choose each Codex model only once.")
            selected_models.append(selected_model)
        if model != selected_models[0]:
            raise ControllerError("The primary Codex model must match the first selected variant.")
    source_path = arguments.get("source_path")
    if source_path is not None:
        if not isinstance(source_path, str) or not source_path.strip() or "\x00" in source_path:
            raise ControllerError("source_path must identify a source transcript.")
        source_path = source_path.strip()
    message_uuid = arguments.get("message_uuid")
    if message_uuid is not None:
        if not isinstance(message_uuid, str) or not message_uuid.strip() or "\x00" in message_uuid:
            raise ControllerError("message_uuid must identify an original user message.")
        message_uuid = message_uuid.strip()
    if not bounds and (source_path is None) != (message_uuid is None):
        raise ControllerError("source_path and message_uuid must be provided together.")
    if bounds:
        message_uuid = None
    repo = arguments.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise ControllerError("repo must be a non-empty path.")
    request = arguments.get("request")
    if request is not None:
        if not isinstance(request, str) or not request.strip() or "\x00" in request:
            raise ControllerError("request must be non-empty text.")
        request = request.strip()
    beginning_kind = arguments.get("beginning_kind")
    if beginning_kind is not None and (
        not isinstance(beginning_kind, str) or beginning_kind not in {"git", "non_git"}
    ):
        raise ControllerError("Choose a Git or Non-Git beginning state.")
    ending_kind = arguments.get("ending_kind")
    if ending_kind is not None and (
        not isinstance(ending_kind, str) or ending_kind not in {"git", "non_git"}
    ):
        raise ControllerError("Choose a Git or Non-Git end state.")
    if (beginning_kind is None) != (ending_kind is None):
        raise ControllerError("Choose both the beginning state and end state.")
    if beginning_kind == "git" and ending_kind == "non_git":
        raise ControllerError("A Git beginning state requires a Git end state.")
    baseline_commit = arguments.get("baseline_commit")
    if baseline_commit is not None:
        if not isinstance(baseline_commit, str):
            raise ControllerError("baseline_commit must be a Git commit.")
        baseline_commit = baseline_commit.strip()
        if not baseline_commit:
            baseline_commit = None
    if beginning_kind == "git" and (
        not isinstance(baseline_commit, str) or COMMIT_PATTERN.fullmatch(baseline_commit) is None
    ):
        raise ControllerError("Enter a valid historical Git commit.")
    if beginning_kind == "non_git" and baseline_commit is not None:
        raise ControllerError("A Non-Git beginning state cannot have a Git commit.")
    if baseline_commit is not None and beginning_kind != "git":
        raise ControllerError("Choose a Git beginning state for baseline_commit.")
    ending_commit = arguments.get("ending_commit")
    if ending_commit is not None:
        if not isinstance(ending_commit, str):
            raise ControllerError("ending_commit must be a Git commit.")
        ending_commit = ending_commit.strip()
        if not ending_commit:
            ending_commit = None
    if ending_kind == "git" and (
        not isinstance(ending_commit, str) or COMMIT_PATTERN.fullmatch(ending_commit) is None
    ):
        raise ControllerError("Enter a valid historical ending Git commit.")
    if ending_kind == "non_git" and ending_commit is not None:
        raise ControllerError("A Non-Git end state cannot have a Git commit.")
    if ending_commit is not None and ending_kind != "git":
        raise ControllerError("Choose a Git end state for ending_commit.")
    configuration = {
        "thread_id": _thread_id(arguments),
        "source_path": source_path,
        "message_uuid": message_uuid,
        "request": request,
        "model": model,
        "repo": repo.strip() if isinstance(repo, str) else None,
        "beginning_kind": beginning_kind,
        "ending_kind": ending_kind,
        "baseline_commit": baseline_commit,
        "ending_commit": ending_commit,
        "confirm_empty_beginning": arguments.get("confirm_empty_beginning") is True,
        "confirm_repository_selection": (arguments.get("confirm_repository_selection") is True),
        "claude_output_files": _string_list(arguments, "claude_output_files"),
        "created_by_claude": _string_list(arguments, "created_by_claude"),
        "excluded_files": _string_list(arguments, "excluded_files"),
        "confirm_file_selection": arguments.get("confirm_file_selection") is True,
    }
    if selected_models is not None:
        configuration["models"] = selected_models
    if bounds:
        configuration.update(bounds)
        configuration["carried_forward_files"] = _string_list(arguments, "carried_forward_files")
    elif arguments.get("carried_forward_files"):
        raise ControllerError("Carried-forward files require a chunk range.")
    return configuration


def _configuration_arguments(arguments: Mapping[str, Any]) -> list[str]:
    configuration = _normalized_configuration(arguments)
    result = [*_session_arguments(configuration), "--model", str(configuration["model"])]
    repo = configuration["repo"]
    if isinstance(repo, str):
        result.extend(("--repo", repo))
    source_path = configuration["source_path"]
    message_uuid = configuration["message_uuid"]
    if isinstance(source_path, str):
        result.extend(("--source-path", source_path))
        if isinstance(message_uuid, str):
            result.extend(("--message-uuid", message_uuid))
    request = configuration["request"]
    if isinstance(request, str):
        result.append("--request-stdin")
    beginning_kind = configuration["beginning_kind"]
    if isinstance(beginning_kind, str):
        result.extend(("--beginning-kind", beginning_kind))
    ending_kind = configuration["ending_kind"]
    if isinstance(ending_kind, str):
        result.extend(("--ending-kind", ending_kind))
    baseline_commit = configuration["baseline_commit"]
    if isinstance(baseline_commit, str):
        result.extend(("--baseline-commit", baseline_commit))
    ending_commit = configuration["ending_commit"]
    if isinstance(ending_commit, str):
        result.extend(("--ending-commit", ending_commit))
    if configuration["confirm_empty_beginning"] is True:
        result.append("--confirm-empty-beginning")
    if configuration["confirm_repository_selection"] is True:
        result.append("--confirm-repository-selection")
    for key, flag in (
        ("claude_output_files", "--claude-output-file"),
        ("created_by_claude", "--created-by-claude"),
        ("excluded_files", "--exclude-file"),
    ):
        for item in configuration[key]:
            result.extend((flag, item))
    if configuration["confirm_file_selection"] is True:
        result.append("--confirm-file-selection")
    for path in configuration.get("carried_forward_files", []):
        result.extend(("--carried-forward-file", path))
    return result
