#!/usr/bin/env python3
"""Record genuine, reproducible Claude Code sessions for bundled Replay samples."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_ROOT.parent
TASK_MANIFEST_PATH = PLUGIN_ROOT / "assets" / "claude-code-sample-tasks.json"
DEFAULT_OUTPUT_ROOT = Path.home() / ".cache" / "codex-bakeoff" / "claude-code-recordings"
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
PACKAGED_SAMPLE_ROOT = PLUGIN_ROOT / "assets" / "claude-code-samples"
REPOSITORY_PATH_MARKER = "__CODEX_BAKEOFF_SAMPLE_REPOSITORY__"
MAX_ARTIFACT_BYTES = 149_000
MAX_CONCURRENT_RECORDINGS = 8
ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
REPOSITORY_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")
SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{20,}={0,2}"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+"),
)
TEMPORARY_PATH_PATTERN = re.compile(r"/(?:private/)?(?:var/folders|tmp)/[^\s\"'`]+")
AGGREGATE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
MODEL_TOKEN_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)
PACKED_RESULT_FIELDS = (
    "type",
    "subtype",
    "is_error",
    "session_id",
    "duration_ms",
    "duration_api_ms",
    "total_cost_usd",
    "usage",
    "modelUsage",
    "num_turns",
    "stop_reason",
    "terminal_reason",
    "fast_mode_state",
    "ttft_ms",
)


class RecordingError(RuntimeError):
    """A Claude session could not be recorded or safely packaged."""


@dataclass(frozen=True)
class SampleTask:
    id: str
    title: str
    repository: str
    baseline_commit: str
    prompt: str
    source_url: str


@dataclass(frozen=True)
class SampleModel:
    id: str
    label: str


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RecordingError(f"Sample manifest field {field!r} must be a nonempty string.")
    return value


def load_manifest(path: Path = TASK_MANIFEST_PATH) -> tuple[list[SampleTask], list[SampleModel]]:
    """Load and validate the reviewed benchmark task/model definitions."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RecordingError(f"Cannot read Claude sample task manifest: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RecordingError("Claude sample task manifest must use schema version 1.")

    tasks: list[SampleTask] = []
    models: list[SampleModel] = []
    raw_tasks = payload.get("tasks")
    raw_models = payload.get("models")
    if not isinstance(raw_tasks, list) or not isinstance(raw_models, list):
        raise RecordingError("Claude sample task manifest must contain tasks and models arrays.")
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise RecordingError("Claude sample tasks must be JSON objects.")
        task = SampleTask(
            id=_required_string(raw, "id"),
            title=_required_string(raw, "title"),
            repository=_required_string(raw, "repository"),
            baseline_commit=_required_string(raw, "baseline_commit"),
            prompt=_required_string(raw, "prompt"),
            source_url=_required_string(raw, "source_url"),
        )
        if not ID_PATTERN.fullmatch(task.id):
            raise RecordingError(f"Invalid sample task ID: {task.id!r}")
        if not REPOSITORY_PATTERN.fullmatch(task.repository):
            raise RecordingError(f"Invalid sample repository: {task.repository!r}")
        if not COMMIT_PATTERN.fullmatch(task.baseline_commit):
            raise RecordingError(f"Invalid immutable baseline commit: {task.baseline_commit!r}")
        tasks.append(task)
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise RecordingError("Claude sample models must be JSON objects.")
        model = SampleModel(_required_string(raw, "id"), _required_string(raw, "label"))
        if not ID_PATTERN.fullmatch(model.id):
            raise RecordingError(f"Invalid sample model ID: {model.id!r}")
        models.append(model)
    if len({task.id for task in tasks}) != len(tasks):
        raise RecordingError("Claude sample task IDs must be unique.")
    if len({model.id for model in models}) != len(models):
        raise RecordingError("Claude sample model IDs must be unique.")
    return tasks, models


def _git(repository: Path, *arguments: str) -> str:
    command = ["git", "-C", str(repository), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RecordingError(f"Cannot run git: {error}") from error
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RecordingError(f"Git command failed ({' '.join(arguments)}): {details}")
    return result.stdout


def _ensure_baseline(task: SampleTask, output_root: Path) -> Path:
    repository = output_root / "repositories" / task.id
    repository_url = f"https://github.com/{task.repository}.git"
    repository.mkdir(parents=True, exist_ok=True)
    if not (repository / ".git").exists():
        _git(repository, "init", "--quiet")
        _git(
            repository,
            "fetch",
            "--quiet",
            "--no-tags",
            repository_url,
            task.baseline_commit,
        )
        _git(repository, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    elif _git(repository, "rev-parse", "--is-shallow-repository").strip() == "true":
        _git(
            repository,
            "fetch",
            "--quiet",
            "--unshallow",
            "--no-tags",
            repository_url,
            task.baseline_commit,
        )
    actual = _git(repository, "rev-parse", "HEAD").strip().lower()
    if actual != task.baseline_commit:
        raise RecordingError(
            f"Existing repository for {task.id} is at {actual}, "
            f"not immutable baseline {task.baseline_commit}."
        )
    if _git(repository, "status", "--porcelain").strip():
        raise RecordingError(f"The immutable baseline repository for {task.id} is not clean.")
    return repository


def _create_worktree(repository: Path, output_root: Path, sample_id: str, session_id: str) -> Path:
    worktree = output_root / "worktrees" / f"{sample_id}--{session_id}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise RecordingError(f"Refusing to replace existing recording worktree {worktree}.")
    _git(repository, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
    return worktree


def _claude_command(
    task: SampleTask,
    model: SampleModel,
    session_id: str,
    max_budget_usd: Decimal,
) -> list[str]:
    allowed_commands = (
        "Bash(git status*)",
        "Bash(git diff*)",
        "Bash(git log*)",
        "Bash(git show*)",
        "Bash(cc*)",
        "Bash(jq*)",
        "Bash(/opt/homebrew/bin/jq*)",
        "Bash(./jq*)",
    )
    command = [
        "claude",
        "-p",
        task.prompt,
        "--model",
        model.id,
        "--session-id",
        session_id,
        "--output-format",
        "stream-json",
        "--verbose",
        "--safe-mode",
        "--no-chrome",
        "--tools",
        "Bash,Read,Edit,Write,Glob,Grep",
        "--permission-mode",
        "acceptEdits",
        "--max-budget-usd",
        str(max_budget_usd),
    ]
    for allowed in allowed_commands:
        command.extend(("--allowedTools", allowed))
    return command


def _invoke_claude(command: Sequence[str], worktree: Path, recording: Path) -> None:
    recording.mkdir(parents=True, exist_ok=False)
    try:
        with (recording / "stream.jsonl").open("w", encoding="utf-8") as stream:
            with (recording / "stderr.log").open("w", encoding="utf-8") as errors:
                result = subprocess.run(
                    list(command),
                    cwd=worktree,
                    stdout=stream,
                    stderr=errors,
                    text=True,
                    check=False,
                )
    except OSError as error:
        raise RecordingError(f"Cannot start Claude Code: {error}") from error
    if result.returncode:
        raise RecordingError(
            f"Claude Code exited {result.returncode}; original output remains in {recording}."
        )


def _final_result(stream_path: Path) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    try:
        with stream_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "result":
                    result = event
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RecordingError(
            f"Cannot parse Claude Code's original event stream: {error}"
        ) from error
    if result is None:
        raise RecordingError("Claude Code did not emit a final result event.")
    if result.get("subtype") != "success" or result.get("is_error") is True:
        raise RecordingError(f"Claude Code run did not succeed: {result.get('subtype')!r}")
    return result


def _find_transcript(session_id: str) -> Path:
    matches = list(CLAUDE_PROJECTS_ROOT.glob(f"*/{session_id}.jsonl"))
    if len(matches) != 1:
        raise RecordingError(
            f"Expected exactly one persisted Claude transcript for {session_id}; "
            f"found {len(matches)}. Do not use --no-session-persistence or --bare."
        )
    return matches[0]


def _validate_result(result: Mapping[str, Any], session_id: str, model_id: str) -> None:
    if result.get("type") != "result" or result.get("subtype") != "success":
        raise RecordingError("Claude Code did not report an authentic successful result event.")
    if result.get("is_error") is not False:
        raise RecordingError("Claude Code result did not explicitly report is_error=false.")
    if result.get("session_id") != session_id:
        raise RecordingError("Claude Code returned a session ID other than the requested UUID.")
    for field in ("duration_ms", "duration_api_ms"):
        duration = result.get(field)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            raise RecordingError(f"Claude Code did not report a genuine positive {field}.")

    estimated_cost = result.get("total_cost_usd")
    if (
        not isinstance(estimated_cost, (int, float))
        or isinstance(estimated_cost, bool)
        or not math.isfinite(float(estimated_cost))
        or estimated_cost <= 0
    ):
        raise RecordingError("Claude Code did not report a genuine positive total_cost_usd.")

    usage = result.get("usage")
    if not isinstance(usage, dict) or not usage:
        raise RecordingError("Claude Code did not report genuine aggregate token usage.")
    aggregate_tokens = [usage.get(field) for field in AGGREGATE_TOKEN_FIELDS]
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in aggregate_tokens
    ):
        raise RecordingError("Claude Code reported incomplete aggregate token usage.")
    if not aggregate_tokens or sum(aggregate_tokens) <= 0:
        raise RecordingError("Claude Code aggregate token usage has no observed tokens.")

    per_model = result.get("modelUsage")
    if not isinstance(per_model, dict) or model_id not in per_model:
        raise RecordingError(
            f"Claude Code modelUsage must contain the requested primary model {model_id}."
        )
    model_usage = per_model.get(model_id) if isinstance(per_model, dict) else None
    if not isinstance(model_usage, dict) or not model_usage:
        raise RecordingError(f"Claude Code did not report genuine modelUsage for {model_id}.")
    token_counts = [model_usage.get(field) for field in MODEL_TOKEN_FIELDS]
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in token_counts
    ):
        raise RecordingError(f"Claude Code reported incomplete token usage for {model_id}.")
    if sum(token_counts) <= 0:
        raise RecordingError(f"Claude Code reported no observed tokens for {model_id}.")
    model_cost = model_usage.get("costUSD")
    if (
        not isinstance(model_cost, (int, float))
        or isinstance(model_cost, bool)
        or not math.isfinite(float(model_cost))
        or model_cost <= 0
    ):
        raise RecordingError(f"Claude Code did not report a genuine costUSD for {model_id}.")


def _validate_transcript(
    transcript: Path, *, session_id: str, requested_model: str, prompt: str
) -> str:
    model: str | None = None
    saw_prompt = False
    try:
        with transcript.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                message = event.get("message")
                recorded_session = event.get("sessionId")
                if recorded_session is not None and recorded_session != session_id:
                    raise RecordingError("Claude transcript contains another session's events.")
                if event.get("type") == "user" and isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        saw_prompt = saw_prompt or content == prompt
                    elif isinstance(content, list):
                        text = "".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict)
                            and block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                        )
                        saw_prompt = saw_prompt or text == prompt
                if event.get("type") != "assistant":
                    continue
                candidate = message.get("model") if isinstance(message, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    if candidate != requested_model:
                        raise RecordingError(
                            f"Requested model {requested_model} but observed {candidate}; "
                            "refusing to label a fallback as the requested model."
                        )
                    model = candidate
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RecordingError(f"Cannot inspect the persisted Claude transcript: {error}") from error
    if model is None:
        raise RecordingError("The persisted Claude transcript has no observed assistant model.")
    if not saw_prompt:
        raise RecordingError(
            "The persisted Claude transcript does not contain the exact task prompt."
        )
    return model


def _validated_recording(
    recording: Path, *, task: SampleTask | None = None, model: SampleModel | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_artifacts = (
        "metadata.json",
        "stream.jsonl",
        "transcript.jsonl",
        "result.json",
        "patch.diff",
        "verification.json",
    )
    missing = [name for name in expected_artifacts if not (recording / name).is_file()]
    if missing:
        raise RecordingError(f"Recording {recording} is incomplete; missing {', '.join(missing)}.")
    try:
        metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
        result = json.loads((recording / "result.json").read_text(encoding="utf-8"))
        verification = json.loads((recording / "verification.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RecordingError(f"Cannot read complete recording from {recording}: {error}") from error
    if not isinstance(metadata, dict) or not isinstance(result, dict):
        raise RecordingError(f"Recording {recording} has invalid metadata or final result.")
    if not isinstance(verification, dict) or not isinstance(verification.get("passed"), bool):
        raise RecordingError(f"Recording {recording} has no genuine verification result.")
    expected_id = (
        f"{task.id}--{model.id}"
        if task is not None and model is not None
        else f"{_required_string(metadata, 'task_id')}--{_required_string(metadata, 'model')}"
    )
    if metadata.get("id") != expected_id or recording.name != expected_id:
        raise RecordingError(f"Existing recording {recording.name} has a mismatched sample ID.")
    checks = {
        "task_id": task.id if task else metadata.get("task_id"),
        "title": task.title if task else metadata.get("title"),
        "repository": task.repository if task else metadata.get("repository"),
        "baseline_commit": task.baseline_commit if task else metadata.get("baseline_commit"),
        "prompt": task.prompt if task else metadata.get("prompt"),
        "source_url": task.source_url if task else metadata.get("source_url"),
        "model": model.id if model else metadata.get("model"),
        "model_label": model.label if model else metadata.get("model_label"),
    }
    for field, expected in checks.items():
        if metadata.get(field) != expected:
            raise RecordingError(f"Existing recording {recording.name} has mismatched {field}.")
    if metadata.get("verification") != verification:
        raise RecordingError(f"Existing recording {recording.name} has mismatched verification.")
    session_id = _required_string(metadata, "session_id")
    observed_model = _required_string(metadata, "model")
    _validate_result(result, session_id, observed_model)
    if _final_result(recording / "stream.jsonl") != result:
        raise RecordingError(f"Existing recording {recording.name} has a mismatched result stream.")
    _validate_transcript(
        recording / "transcript.jsonl",
        session_id=session_id,
        requested_model=observed_model,
        prompt=_required_string(metadata, "prompt"),
    )
    for field in ("duration_ms", "duration_api_ms", "total_cost_usd"):
        if metadata.get(field) != result.get(field):
            raise RecordingError(f"Recorded metadata {field} does not match the genuine result.")
    return metadata, result


def _capture_patch(repository: Path) -> str:
    status = _git(repository, "status", "--porcelain=v1", "-z")
    for entry in status.split("\0"):
        if entry.startswith("?? "):
            _git(repository, "add", "--intent-to-add", "--", entry[3:])
    return _git(repository, "diff", "--binary", "HEAD")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _verify(task_id: str, repository: Path) -> dict[str, Any]:
    if str(SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(SCRIPT_ROOT))
    module = importlib.import_module("verify_claude_sample")
    verified = module.verify_task(task_id, repository)
    if not isinstance(verified, dict):
        raise RecordingError("Claude sample verifier returned an invalid result.")
    return verified


def record_sample(
    task: SampleTask,
    model: SampleModel,
    output_root: Path,
    max_budget_usd: Decimal,
    *,
    preparation_lock: threading.Lock | None = None,
) -> Path:
    """Run one real Claude Code session and retain its untouched local artifacts."""

    sample_id = f"{task.id}--{model.id}"
    recording = output_root / "recordings" / sample_id
    if (recording / "metadata.json").is_file():
        _validated_recording(recording, task=task, model=model)
        print(f"Reusing completed recording {sample_id}", file=sys.stderr, flush=True)
        return recording
    if recording.exists():
        return recover_sample(task, model, output_root)

    with contextlib.nullcontext() if preparation_lock is None else preparation_lock:
        baseline = _ensure_baseline(task, output_root)
        session_id = str(uuid.uuid4())
        worktree = _create_worktree(baseline, output_root, sample_id, session_id)
    started_at = datetime.now(timezone.utc)
    command = _claude_command(task, model, session_id, max_budget_usd)
    print(f"Recording {task.id} with {model.label} (cap ${max_budget_usd})", flush=True)
    _invoke_claude(command, worktree, recording)
    return _finalize_recording(task, model, recording, worktree, session_id, started_at)


def _finalize_recording(
    task: SampleTask,
    model: SampleModel,
    recording: Path,
    worktree: Path,
    session_id: str,
    started_at: datetime,
) -> Path:
    sample_id = f"{task.id}--{model.id}"
    result = _final_result(recording / "stream.jsonl")
    _validate_result(result, session_id, model.id)

    original_transcript = _find_transcript(session_id)
    actual_model = _validate_transcript(
        original_transcript, session_id=session_id, requested_model=model.id, prompt=task.prompt
    )
    shutil.copyfile(original_transcript, recording / "transcript.jsonl")
    patch = _capture_patch(worktree)
    (recording / "patch.diff").write_text(patch, encoding="utf-8")
    _write_json(recording / "result.json", result)
    verification = _verify(task.id, worktree)
    _write_json(recording / "verification.json", verification)
    finished_at = datetime.now(timezone.utc)

    metadata = {
        "schema_version": 1,
        "id": sample_id,
        "task_id": task.id,
        "title": task.title,
        "repository": task.repository,
        "baseline_commit": task.baseline_commit,
        "prompt": task.prompt,
        "source_url": task.source_url,
        "model": actual_model,
        "model_label": model.label,
        "session_id": session_id,
        "recorded_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "total_cost_usd": result.get("total_cost_usd"),
        "verification": verification,
        "original_repository_path": str(worktree),
        "original_transcript_path": str(original_transcript),
    }
    _write_json(recording / "metadata.json", metadata)
    return recording


def _recoverable_recording(
    task: SampleTask, model: SampleModel, output_root: Path
) -> tuple[Path, Path, str, dict[str, Any]]:
    sample_id = f"{task.id}--{model.id}"
    recording = output_root / "recordings" / sample_id
    stream = recording / "stream.jsonl"
    if not stream.is_file():
        raise RecordingError(f"Existing recording {sample_id} has no original Claude event stream.")

    result = _final_result(stream)
    session_id = _required_string(result, "session_id")
    try:
        canonical_session_id = str(uuid.UUID(session_id))
    except (ValueError, AttributeError) as error:
        raise RecordingError(
            "Completed Claude result does not contain a valid session UUID."
        ) from error
    if canonical_session_id != session_id:
        raise RecordingError("Completed Claude result does not contain a canonical session UUID.")
    _validate_result(result, session_id, model.id)

    worktree = output_root / "worktrees" / f"{sample_id}--{session_id}"
    if not worktree.is_dir():
        raise RecordingError(f"Completed Claude recording has no matching worktree at {worktree}.")
    if worktree.resolve().parent != (output_root / "worktrees").resolve():
        raise RecordingError(
            "Completed Claude recording worktree resolves outside its sample root."
        )
    actual_baseline = _git(worktree, "rev-parse", "HEAD").strip().lower()
    if actual_baseline != task.baseline_commit:
        raise RecordingError(
            f"Completed Claude recording is at {actual_baseline}, not immutable baseline "
            f"{task.baseline_commit}."
        )

    transcript = _find_transcript(session_id)
    _validate_transcript(
        transcript, session_id=session_id, requested_model=model.id, prompt=task.prompt
    )
    return recording, worktree, session_id, result


def recover_sample(task: SampleTask, model: SampleModel, output_root: Path) -> Path:
    """Finalize an already successful genuine Claude run without invoking Claude again."""

    sample_id = f"{task.id}--{model.id}"
    recording = output_root / "recordings" / sample_id
    if (recording / "metadata.json").is_file():
        _validated_recording(recording, task=task, model=model)
        print(f"Reusing completed recording {sample_id}", file=sys.stderr, flush=True)
        return recording

    recording, worktree, session_id, result = _recoverable_recording(task, model, output_root)
    stream_finished_at = (recording / "stream.jsonl").stat().st_mtime
    started_at = datetime.fromtimestamp(
        stream_finished_at - result["duration_ms"] / 1000, tz=timezone.utc
    )
    print(f"Recovering completed Claude recording {sample_id} without rerunning it", flush=True)
    return _finalize_recording(task, model, recording, worktree, session_id, started_at)


def _scrub_string(value: str, repository: str) -> str:
    scrubbed = value.replace(repository, REPOSITORY_PATH_MARKER)
    scrubbed = scrubbed.replace(str(PLUGIN_ROOT), "[PLUGIN]")
    user_home = str(Path.home())
    if user_home and user_home != "/":
        scrubbed = scrubbed.replace(user_home, "[HOME]")
    scrubbed = scrubbed.replace("/opt/homebrew", "[HOMEBREW]")
    scrubbed = TEMPORARY_PATH_PATTERN.sub("[TEMPORARY_PATH]", scrubbed)
    for pattern in SECRET_PATTERNS:
        scrubbed = pattern.sub("[REDACTED_SECRET]", scrubbed)
    return scrubbed


def _scrub(value: Any, repository: str) -> Any:
    if isinstance(value, str):
        return _scrub_string(value, repository)
    if isinstance(value, list):
        return [_scrub(item, repository) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, repository) for key, item in value.items()}
    return value


def _truncate_strings(value: Any, limit: int = 12_000) -> Any:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= limit:
            return value
        head = encoded[: limit // 2].decode("utf-8", errors="ignore")
        tail = encoded[-limit // 2 :].decode("utf-8", errors="ignore")
        return f"{head}\n[Transcript content truncated from {len(encoded)} bytes]\n{tail}"
    if isinstance(value, list):
        return [_truncate_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_strings(item, limit) for key, item in value.items()}
    return value


def _remove_hidden_thinking(value: Any) -> Any:
    if isinstance(value, list):
        return [
            _remove_hidden_thinking(item)
            for item in value
            if not (
                isinstance(item, dict) and item.get("type") in {"thinking", "redacted_thinking"}
            )
        ]
    if isinstance(value, dict):
        return {
            key: _remove_hidden_thinking(item)
            for key, item in value.items()
            if key not in {"thinking", "redacted_thinking", "signature"}
        }
    return value


def _serialized_event(raw: str, repository: str) -> bytes:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RecordingError(
            f"Persisted Claude transcript contains invalid JSON: {error}"
        ) from error
    if not isinstance(event, dict):
        raise RecordingError("Persisted Claude transcript contains a non-object event.")
    scrubbed = _remove_hidden_thinking(_scrub(event, repository))
    encoded = (json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) < MAX_ARTIFACT_BYTES:
        return encoded
    scrubbed = _truncate_strings(scrubbed)
    encoded = (json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) < MAX_ARTIFACT_BYTES:
        return encoded
    message = scrubbed.get("message")
    if isinstance(message, dict) and "content" in message:
        message["content"] = [
            {
                "type": "text",
                "text": "[Oversized original transcript content omitted for portable packaging]",
            }
        ]
    encoded = (json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) >= MAX_ARTIFACT_BYTES:
        raise RecordingError(
            "One sanitized Claude transcript event exceeds the package size limit."
        )
    return encoded


def _transcript_parts(source: Path, destination: Path, repository: str) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    pending = bytearray()

    def flush() -> None:
        if not pending:
            return
        target = destination / f"transcript.{len(parts):03d}.jsonl"
        target.write_bytes(bytes(pending))
        parts.append(target)
        pending.clear()

    with source.open("r", encoding="utf-8") as transcript:
        for line in transcript:
            if not line.strip():
                continue
            event = _serialized_event(line, repository)
            if pending and len(pending) + len(event) >= MAX_ARTIFACT_BYTES:
                flush()
            pending.extend(event)
    flush()
    if not parts:
        raise RecordingError("The captured Claude transcript contains no usable events.")
    return parts


def _write_bounded_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) >= MAX_ARTIFACT_BYTES:
        raise RecordingError(f"Packaged sample artifact {path.name} exceeds the file size limit.")
    path.write_bytes(encoded)


def _pack_recording(recording: Path, package_root: Path) -> dict[str, Any]:
    metadata, result = _validated_recording(recording)
    sample_id = _required_string(metadata, "id")
    if not ID_PATTERN.fullmatch(sample_id):
        raise RecordingError(f"Recording has unsafe sample ID {sample_id!r}.")
    original_repository = _required_string(metadata, "original_repository_path")
    sample_root = package_root / "records" / sample_id
    sample_root.mkdir(parents=True, exist_ok=True)
    transcript_parts = _transcript_parts(
        recording / "transcript.jsonl", sample_root, original_repository
    )

    patch = (recording / "patch.diff").read_text(encoding="utf-8")
    if _scrub_string(patch, original_repository) != patch:
        raise RecordingError(
            f"Recorded patch for {sample_id} contains a private path or credential; "
            "refusing to alter genuine Claude output or package private data."
        )
    sanitized_patch = patch.encode("utf-8")
    if len(sanitized_patch) >= MAX_ARTIFACT_BYTES:
        raise RecordingError(f"Recorded patch for {sample_id} exceeds the file size limit.")
    patch_path = sample_root / "patch.diff"
    patch_path.write_bytes(sanitized_patch)

    packed_result = {
        key: _scrub(result[key], original_repository)
        for key in PACKED_RESULT_FIELDS
        if key in result
    }
    result_path = sample_root / "result.json"
    _write_bounded_json(result_path, packed_result)

    selected_fields = (
        "id",
        "task_id",
        "title",
        "repository",
        "baseline_commit",
        "prompt",
        "source_url",
        "model",
        "model_label",
        "session_id",
        "recorded_at",
        "duration_ms",
        "duration_api_ms",
        "total_cost_usd",
    )
    entry = {field: _scrub(metadata.get(field), original_repository) for field in selected_fields}
    raw_verification = metadata["verification"]
    raw_checks = raw_verification.get("checks", [])
    entry["verification"] = {
        "passed": raw_verification["passed"],
        "checks": [
            {
                "name": _scrub(check.get("name"), original_repository),
                "passed": check.get("passed"),
                "returncode": check.get("returncode"),
            }
            for check in raw_checks
            if isinstance(check, dict)
        ],
    }
    entry.update(
        {
            "transcript_parts": [str(path.relative_to(package_root)) for path in transcript_parts],
            "patch_path": str(patch_path.relative_to(package_root)),
            "result_path": str(result_path.relative_to(package_root)),
            "original_repository_path_marker": REPOSITORY_PATH_MARKER,
        }
    )
    if str(SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(SCRIPT_ROOT))
    from precompute_sample_configurations import precompute

    precompute(entry, package_root)
    return entry


def pack_recordings(recordings: Sequence[Path], package_root: Path = PACKAGED_SAMPLE_ROOT) -> Path:
    """Package sanitized genuine recordings without generating synthetic sessions."""

    if not recordings:
        raise RecordingError("No genuine Claude Code recordings are available to package.")
    package_root.mkdir(parents=True, exist_ok=True)
    entries = [_pack_recording(recording, package_root) for recording in recordings]
    index_path = package_root / "index.json"
    _write_bounded_json(index_path, {"schema_version": 1, "samples": entries})
    return index_path


def _positive_budget(raw: str) -> Decimal:
    try:
        budget = Decimal(raw)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a positive dollar amount") from error
    if not budget.is_finite() or budget <= 0 or not math.isfinite(float(budget)):
        raise argparse.ArgumentTypeError("must be a positive finite dollar amount")
    return budget


def _selected(
    values: Sequence[SampleTask] | Sequence[SampleModel], requested: Sequence[str] | None
) -> list[SampleTask] | list[SampleModel]:
    if not requested:
        return list(values)
    requested_set = set(requested)
    available = {item.id for item in values}
    unknown = requested_set - available
    if unknown:
        raise RecordingError(f"Unknown requested IDs: {', '.join(sorted(unknown))}")
    return [item for item in values if item.id in requested_set]


def _record_samples(
    matrix: Sequence[tuple[SampleTask, SampleModel]],
    output_root: Path,
    max_budget_usd: Decimal,
    jobs: int,
) -> list[Path]:
    for task, model in matrix:
        recording = output_root / "recordings" / f"{task.id}--{model.id}"
        if recording.exists() and not (recording / "metadata.json").is_file():
            try:
                _recoverable_recording(task, model, output_root)
            except RecordingError as error:
                raise RecordingError(
                    f"Incomplete recording already exists at {recording}; it may still be "
                    f"running or cannot be safely recovered: {error}. Preserve it and let "
                    "any active recording finish before retrying."
                ) from error

    preparation_locks = {task.id: threading.Lock() for task, _ in matrix}
    recordings: list[Path] = []
    failures: list[tuple[str, RecordingError]] = []
    with ThreadPoolExecutor(max_workers=min(jobs, len(matrix))) as executor:
        pending = [
            (
                task,
                model,
                executor.submit(
                    record_sample,
                    task,
                    model,
                    output_root,
                    max_budget_usd,
                    preparation_lock=preparation_locks[task.id],
                ),
            )
            for task, model in matrix
        ]
        for task, model, future in pending:
            try:
                recordings.append(future.result())
            except RecordingError as error:
                failures.append((f"{task.id}--{model.id}", error))

    if failures:
        details = "\n".join(f"  {sample_id}: {error}" for sample_id, error in failures)
        raise RecordingError(f"{len(failures)} Claude Code recording(s) failed:\n{details}")
    return recordings


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-budget-usd",
        type=_positive_budget,
        help="Explicit per-run API-equivalent budget; required before any Claude invocation.",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--task", action="append", help="Record only this task ID; repeatable.")
    parser.add_argument("--model", action="append", help="Record only this model ID; repeatable.")
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        choices=range(1, MAX_CONCURRENT_RECORDINGS + 1),
        default=MAX_CONCURRENT_RECORDINGS,
        metavar="N",
        help=(
            "Maximum concurrent Claude sessions "
            f"(1-{MAX_CONCURRENT_RECORDINGS}; default: {MAX_CONCURRENT_RECORDINGS})."
        ),
    )
    parser.add_argument(
        "--pack", action="store_true", help="Package recorded sessions as portable Replay samples."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the planned matrix without running Claude."
    )
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="Finalize completed existing sessions without invoking Claude or requiring a budget.",
    )
    options = parser.parse_args(arguments)
    if options.recover_only and options.max_budget_usd is not None:
        parser.error("--recover-only cannot be combined with --max-budget-usd")

    try:
        tasks, models = load_manifest()
        selected_tasks = _selected(tasks, options.task)
        selected_models = _selected(models, options.model)
        matrix = [(task, model) for task in selected_tasks for model in selected_models]
        output_root = options.output_root.expanduser().resolve()

        if options.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "output_root": str(output_root),
                        "max_budget_usd": (
                            str(options.max_budget_usd) if options.max_budget_usd else None
                        ),
                        "jobs": min(options.jobs, len(matrix)),
                        "samples": [
                            {
                                "id": f"{task.id}--{model.id}",
                                "task_id": task.id,
                                "model": model.id,
                                "model_label": model.label,
                            }
                            for task, model in matrix
                        ],
                    },
                    indent=2,
                )
            )
            return 0

        if options.recover_only:
            recordings = [recover_sample(task, model, output_root) for task, model in matrix]
            if options.pack:
                index = pack_recordings(recordings)
                print(f"Packaged {len(recordings)} authentic Claude Code recordings: {index}")
            else:
                print(
                    f"Recovered {len(recordings)} authentic Claude Code sessions in {output_root}"
                )
            return 0

        if options.max_budget_usd is None and not options.pack:
            parser.error("--max-budget-usd is required before running Claude Code")

        if options.max_budget_usd is not None:
            if any(model.id == "claude-fable-5" for _, model in matrix):
                print(
                    "Warning: noninteractive Claude Fable 5 can consume additional usage "
                    "credits without a consent prompt; the explicitly supplied per-run "
                    f"API-equivalent cap is ${options.max_budget_usd}.",
                    file=sys.stderr,
                    flush=True,
                )
            recordings = _record_samples(matrix, output_root, options.max_budget_usd, options.jobs)
        else:
            recordings = [
                output_root / "recordings" / f"{task.id}--{model.id}" for task, model in matrix
            ]
            missing = [path.name for path in recordings if not (path / "metadata.json").is_file()]
            if missing:
                raise RecordingError(
                    "Offline packaging requires completed genuine recordings; missing "
                    + ", ".join(missing)
                )
            for (task, model), recording in zip(matrix, recordings):
                _validated_recording(recording, task=task, model=model)

        if options.pack:
            index = pack_recordings(recordings)
            print(f"Packaged {len(recordings)} authentic Claude Code recordings: {index}")
        else:
            print(f"Recorded {len(recordings)} authentic Claude Code sessions in {output_root}")
        return 0
    except RecordingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
