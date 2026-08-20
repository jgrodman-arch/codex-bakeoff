"""Load and reconstruct genuine, recorded Claude Code sample sessions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ASSET_ROOT = PLUGIN_ROOT / "assets" / "claude-code-samples"
SAMPLE_INDEX = SAMPLE_ASSET_ROOT / "index.json"
THREAD_PREFIX = "claude-sample:"
REPOSITORY_PATH_MARKER = "__CODEX_BAKEOFF_SAMPLE_REPOSITORY__"
SAMPLE_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
REPOSITORY_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT_PATTERN = re.compile(r"\A[0-9a-fA-F]{40}\Z")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_materialization_lock = threading.RLock()


class SampleError(ValueError):
    """A recorded sample is malformed, unavailable, or cannot be reconstructed."""


@dataclass(frozen=True)
class RecordedSample:
    """Validated, portable metadata for one genuine Claude Code session."""

    id: str
    task_id: str
    title: str
    repository: str
    baseline_commit: str
    prompt: str
    source_url: str
    model: str
    model_label: str
    session_id: str
    transcript_parts: tuple[Path, ...]
    patch_path: Path
    result_path: Path
    recorded_at: str
    duration_ms: int
    duration_api_ms: int
    total_cost_usd: float
    verification: object
    original_repository_path_marker: str


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SampleError(f"Recorded Claude sample has an invalid {key}.")
    return value


def _positive_duration(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SampleError(f"Recorded Claude sample has an invalid {key}.")
    return value


def _positive_cost(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SampleError(f"Recorded Claude sample has an invalid {key}.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise SampleError(f"Recorded Claude sample has an invalid {key}.")
    return normalized


def _recorded_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _asset_path(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SampleError(f"Recorded Claude sample has an invalid {label}.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SampleError(f"Recorded Claude sample {label} escapes its asset directory.")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise SampleError(f"Recorded Claude sample {label} escapes its asset directory.")
    if not candidate.is_file():
        raise SampleError(f"Recorded Claude sample {label} is unavailable.")
    if candidate.stat().st_size > MAX_ARTIFACT_BYTES:
        raise SampleError(f"Recorded Claude sample {label} is too large.")
    return candidate


def _sample_from_payload(payload: Mapping[str, Any], root: Path) -> RecordedSample:
    sample_id = _required_string(payload, "id")
    if SAMPLE_ID_PATTERN.fullmatch(sample_id) is None:
        raise SampleError("Recorded Claude sample has an invalid identifier.")
    repository = _required_string(payload, "repository")
    if REPOSITORY_PATTERN.fullmatch(repository) is None or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        raise SampleError("Recorded Claude sample has an invalid GitHub repository.")
    baseline_commit = _required_string(payload, "baseline_commit")
    if COMMIT_PATTERN.fullmatch(baseline_commit) is None:
        raise SampleError("Recorded Claude sample requires an exact upstream commit.")
    session_id = _required_string(payload, "session_id")
    if SAMPLE_ID_PATTERN.fullmatch(session_id) is None:
        raise SampleError("Recorded Claude sample has an invalid Claude session identifier.")
    marker = _required_string(payload, "original_repository_path_marker")
    if marker != REPOSITORY_PATH_MARKER:
        raise SampleError("Recorded Claude sample has an invalid repository path marker.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SampleError("Recorded Claude sample has an invalid prompt.")

    recorded_at = _required_string(payload, "recorded_at")
    try:
        parsed_recorded_at = _recorded_datetime(recorded_at)
    except ValueError as error:
        raise SampleError("Recorded Claude sample has an invalid recording timestamp.") from error
    if parsed_recorded_at.tzinfo is None:
        raise SampleError("Recorded Claude sample recording timestamp requires a timezone.")

    raw_parts = payload.get("transcript_parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise SampleError("Recorded Claude sample has no genuine session transcript.")
    parts = tuple(_asset_path(root, value, label="transcript part") for value in raw_parts)
    if len(set(parts)) != len(parts):
        raise SampleError("Recorded Claude sample repeats a transcript part.")

    sample = RecordedSample(
        id=sample_id,
        task_id=_required_string(payload, "task_id"),
        title=_required_string(payload, "title"),
        repository=repository,
        baseline_commit=baseline_commit.lower(),
        prompt=prompt,
        source_url=_required_string(payload, "source_url"),
        model=_required_string(payload, "model"),
        model_label=_required_string(payload, "model_label"),
        session_id=session_id,
        transcript_parts=parts,
        patch_path=_asset_path(root, payload.get("patch_path"), label="recorded patch"),
        result_path=_asset_path(root, payload.get("result_path"), label="recorded result"),
        recorded_at=recorded_at,
        duration_ms=_positive_duration(payload, "duration_ms"),
        duration_api_ms=_positive_duration(payload, "duration_api_ms"),
        total_cost_usd=_positive_cost(payload, "total_cost_usd"),
        verification=payload.get("verification"),
        original_repository_path_marker=marker,
    )
    _recorded_result(sample)
    return sample


def load_samples(index_path: str | Path | None = None) -> list[RecordedSample]:
    """Return packaged recordings; missing optional samples are an empty catalog."""

    path = Path(index_path) if index_path is not None else SAMPLE_INDEX
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SampleError("Recorded Claude sample catalog is invalid.") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SampleError("Recorded Claude sample catalog has an unsupported schema.")
    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list):
        raise SampleError("Recorded Claude sample catalog has no sample collection.")

    samples: list[RecordedSample] = []
    seen: set[str] = set()
    for raw_sample in raw_samples:
        if not isinstance(raw_sample, Mapping):
            raise SampleError("Recorded Claude sample catalog contains an invalid sample.")
        sample = _sample_from_payload(raw_sample, path.parent)
        if sample.id in seen:
            raise SampleError("Recorded Claude sample catalog repeats an identifier.")
        seen.add(sample.id)
        samples.append(sample)
    return samples


def is_sample_thread(thread_id: str) -> bool:
    return thread_id.startswith(THREAD_PREFIX)


def sample_id_from_thread(thread_id: str) -> str:
    if not is_sample_thread(thread_id):
        raise SampleError("The selected thread is not a recorded Claude sample.")
    sample_id = thread_id[len(THREAD_PREFIX) :]
    if SAMPLE_ID_PATTERN.fullmatch(sample_id) is None:
        raise SampleError("The recorded Claude sample identifier is invalid.")
    return sample_id


def list_sample_threads(index_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Expose recordings through the existing imported-thread picker."""

    return [
        {
            "imported_thread_id": f"{THREAD_PREFIX}{sample.id}",
            "session_id": sample.session_id,
            "title": f"Sample: {sample.title}",
            "project_dir": sample.repository,
            "created_at": sample.recorded_at,
            "activity_at": sample.recorded_at,
            "claude_model": sample.model,
            "claude_model_label": sample.model_label,
            "sample_id": sample.id,
            "task_id": sample.task_id,
            "duration_ms": sample.duration_ms,
            "duration_api_ms": sample.duration_api_ms,
            "total_cost_usd": sample.total_cost_usd,
            "verification": sample.verification,
        }
        for sample in load_samples(index_path)
    ]


def sample_root(controller_root: str | Path) -> Path:
    return Path(controller_root).resolve() / "claude-code-samples"


def ledger_path(controller_root: str | Path) -> Path:
    return sample_root(controller_root) / "imports.json"


def _repository_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _run_git(arguments: Sequence[str], *, input_text: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SampleError("The recorded Claude sample repository could not be created.") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SampleError(
            f"The recorded Claude sample repository could not be created: {detail[:500]}"
        )
    return completed.stdout.strip()


def _recorded_result(sample: RecordedSample) -> dict[str, Any]:
    try:
        result = json.loads(sample.result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SampleError("The recorded Claude Code result is invalid.") from error
    if not isinstance(result, Mapping):
        raise SampleError("The recorded Claude Code result is invalid.")
    if result.get("type") != "result" or result.get("session_id") != sample.session_id:
        raise SampleError("The recorded Claude Code result does not match its genuine session.")
    captured: dict[str, Any] = {}
    for field, expected in (
        ("duration_ms", sample.duration_ms),
        ("duration_api_ms", sample.duration_api_ms),
    ):
        value = _positive_duration(result, field)
        if value != expected:
            raise SampleError(f"The recorded Claude Code result has inconsistent {field}.")
        captured[field] = value
    observed_cost = _positive_cost(result, "total_cost_usd")
    if observed_cost != sample.total_cost_usd:
        raise SampleError("The recorded Claude Code result has inconsistent total_cost_usd.")
    captured["total_cost_usd"] = observed_cost

    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        raise SampleError("The recorded Claude Code result has no genuine token usage.")
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    token_counts = [usage.get(field) for field in token_fields]
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in token_counts
    ):
        raise SampleError("The recorded Claude Code result has invalid token usage.")
    if sum(token_counts) <= 0:
        raise SampleError("The recorded Claude Code result has no observed token usage.")

    model_usage = result.get("modelUsage")
    if not isinstance(model_usage, Mapping):
        raise SampleError("The recorded Claude Code result has no genuine model usage.")
    selected_usage = model_usage.get(sample.model)
    if not isinstance(selected_usage, Mapping):
        raise SampleError("The recorded Claude Code result does not identify its recorded model.")
    model_fields = (
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
    )
    model_counts = [selected_usage.get(field) for field in model_fields]
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in model_counts
    ):
        raise SampleError("The recorded Claude Code result has invalid model token usage.")
    if sum(model_counts) <= 0:
        raise SampleError("The recorded Claude Code result has no observed model token usage.")
    _positive_cost(selected_usage, "costUSD")
    captured["usage"] = dict(usage)
    captured["modelUsage"] = dict(model_usage)
    return captured


def _read_materialization(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SampleError("The recorded Claude sample reconstruction is unavailable.") from error
    if not isinstance(payload, dict):
        raise SampleError("The recorded Claude sample reconstruction is unavailable.")
    return payload


def _write_ledger(
    path: Path,
    *,
    sample: RecordedSample,
    transcript_path: Path,
    recorded_result: Mapping[str, Any],
) -> None:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SampleError("The controller-private Claude sample ledger is invalid.") from error
        records = payload.get("records") if isinstance(payload, Mapping) else None
        if not isinstance(records, list):
            raise SampleError("The controller-private Claude sample ledger is invalid.")
    else:
        records = []

    thread_id = f"{THREAD_PREFIX}{sample.id}"
    recorded = _recorded_datetime(sample.recorded_at)
    records = [
        record
        for record in records
        if not isinstance(record, Mapping) or record.get("imported_thread_id") != thread_id
    ]
    records.append(
        {
            "source_path": str(transcript_path),
            "content_sha256": hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
            "imported_thread_id": thread_id,
            "imported_at": int(recorded.astimezone(timezone.utc).timestamp()),
            "source_modified_at": transcript_path.stat().st_mtime_ns,
            "connector_names": [],
            "title": f"Sample: {sample.title}",
            "recorded_claude_result": dict(recorded_result),
        }
    )
    path.write_text(json.dumps({"records": records}, indent=2) + "\n", encoding="utf-8")


def materialize_sample(
    sample_id: str,
    controller_root: str | Path,
    *,
    index_path: str | Path | None = None,
) -> dict[str, Any]:
    """Reconstruct one recorded session on its genuine upstream Git baseline."""

    if SAMPLE_ID_PATTERN.fullmatch(sample_id) is None:
        raise SampleError("The recorded Claude sample identifier is invalid.")
    selected = next((sample for sample in load_samples(index_path) if sample.id == sample_id), None)
    if selected is None:
        raise SampleError("The selected recorded Claude sample is unavailable.")

    root = sample_root(controller_root)
    directory = root / selected.id
    manifest = directory / "materialization.json"
    with _materialization_lock:
        if manifest.is_file():
            return _read_materialization(manifest)
        root.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError as error:
            raise SampleError("A recorded Claude sample reconstruction is incomplete.") from error

        try:
            repository_path = directory / "repository"
            _run_git(("init", "--quiet", str(repository_path)))
            _run_git(
                (
                    "-C",
                    str(repository_path),
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    _repository_url(selected.repository),
                    selected.baseline_commit,
                )
            )
            _run_git(("-C", str(repository_path), "checkout", "--quiet", "--detach", "FETCH_HEAD"))
            baseline_commit = _run_git(("-C", str(repository_path), "rev-parse", "HEAD"))
            if baseline_commit.lower() != selected.baseline_commit:
                raise SampleError("The recorded Claude sample resolved the wrong upstream commit.")

            patch = selected.patch_path.read_text(encoding="utf-8")
            if patch.strip():
                _run_git(("-C", str(repository_path), "apply", "--index", "-"), input_text=patch)
            _run_git(
                (
                    "-C",
                    str(repository_path),
                    "-c",
                    "user.name=Codex Bakeoff",
                    "-c",
                    "user.email=codex-bakeoff@localhost",
                    "commit",
                    "--quiet",
                    "--no-gpg-sign",
                    "--allow-empty",
                    "-m",
                    f"Recorded Claude Code output: {selected.model}",
                )
            )
            ending_commit = _run_git(("-C", str(repository_path), "rev-parse", "HEAD"))

            transcript_path = directory / f"{selected.session_id}.jsonl"
            escaped_repository_path = json.dumps(
                str(repository_path),
                ensure_ascii=False,
            )[1:-1]
            with transcript_path.open("w", encoding="utf-8", newline="") as transcript:
                for part in selected.transcript_parts:
                    content = part.read_text(encoding="utf-8")
                    transcript.write(
                        content.replace(
                            selected.original_repository_path_marker,
                            escaped_repository_path,
                        )
                    )
            recorded_result = _recorded_result(selected)
            result_path = directory / "result.json"
            shutil.copyfile(selected.result_path, result_path)
            private_ledger = ledger_path(controller_root)
            _write_ledger(
                private_ledger,
                sample=selected,
                transcript_path=transcript_path,
                recorded_result=recorded_result,
            )
            result: dict[str, Any] = {
                "sample_id": selected.id,
                "thread_id": f"{THREAD_PREFIX}{selected.id}",
                "repository_path": str(repository_path),
                "baseline_commit": baseline_commit,
                "ending_commit": ending_commit,
                "transcript_path": str(transcript_path),
                "result_path": str(result_path),
                "ledger_path": str(private_ledger),
                "recorded_claude_result": recorded_result,
            }
            manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
