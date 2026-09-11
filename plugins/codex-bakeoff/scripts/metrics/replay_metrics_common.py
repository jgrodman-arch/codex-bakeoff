"""Shared state-reading and trusted-sidecar primitives for Replay metrics scripts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

METRICS_OUTPUT_ENV = "CODEX_PLUGIN_METRICS_OUTPUT"
DEFAULT_RUN_ROOT = Path.home() / ".cache" / "codex-bakeoff" / "runs"
STATE_NAME = "controller-state.json"
MAX_MODELS = 8
MODEL_SLOTS = ("one", "two", "three", "four", "five", "six", "seven", "eight")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
MAX_OUTPUT_BYTES = 64 * 1024
MAX_STATE_BYTES = 512 * 1024
# Match the server budget for an accepted HTTP request plus lifecycle metadata.
MAX_ATTEMPT_BYTES = 2 * 1024 * 1024 + MAX_STATE_BYTES
DEFAULT_PRE_START_TIMEOUT_SECONDS = 10 * 60


def model_family(model: object, families: Sequence[str]) -> str:
    if not isinstance(model, str):
        return "other"
    lowered = model.lower()
    for family in families:
        if re.search(rf"(?:^|[^a-z]){re.escape(family)}(?:[^a-z]|$)", lowered):
            return family
    return "other"


def timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def measurement(name: str, value: int | float, dimensions: Mapping[str, str]) -> dict[str, object]:
    row: dict[str, object] = {"name": name, "value": value}
    if dimensions:
        row["dimensions"] = dict(dimensions)
    return row


def encode_measurements(rows: Sequence[Mapping[str, object]]) -> bytes:
    return (
        json.dumps(
            {"version": 1, "measurements": rows},
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def nonnegative_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number") from error
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return value


def observed_json(path: Path, *, maximum: int | None = MAX_STATE_BYTES) -> Mapping[str, Any] | None:
    """Absence is unresolved; malformed or inaccessible evidence is unreadable."""
    try:
        with path.open("rb") as stream:
            raw = stream.read() if maximum is None else stream.read(maximum + 1)
    except FileNotFoundError:
        return None
    if maximum is not None and len(raw) > maximum:
        raise ValueError("Replay state exceeds its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Replay state must be an object")
    return value


def read_attempt(path: Path, session: str) -> Mapping[str, Any]:
    attempt = observed_json(path, maximum=MAX_ATTEMPT_BYTES)
    if (
        attempt is None
        or attempt.get("controller_session_id") != session
        or attempt.get("version") != 1
    ):
        raise ValueError("Invalid attempt identity")
    return attempt


def read_attempt_if_present(path: Path, session: str) -> Mapping[str, Any] | None:
    attempt = observed_json(path, maximum=MAX_ATTEMPT_BYTES)
    if attempt is None:
        return None
    if attempt.get("controller_session_id") != session or attempt.get("version") != 1:
        raise ValueError("Invalid attempt identity")
    return attempt


def preserve_payload(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except OSError as error:
        print(f"Unable to preserve local Codex Bakeoff metrics: {error.strerror}.", file=sys.stderr)


def arguments(argv: Sequence[str] | None = None, *, launch: bool = False) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-session-id", required=not launch)
    parser.add_argument("--codex-cli-path")
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--run-timeout-seconds", type=nonnegative_seconds, default=3600)
    parser.add_argument(
        "--pre-start-timeout-seconds",
        type=nonnegative_seconds,
        default=DEFAULT_PRE_START_TIMEOUT_SECONDS,
    )
    parser.add_argument("--poll-interval-seconds", type=nonnegative_seconds, default=1)
    return parser.parse_args(argv)


def approved_models(attempt: Mapping[str, Any]) -> list[str]:
    entries = attempt.get("models", [])
    if not isinstance(entries, list) or len(entries) > MAX_MODELS:
        raise ValueError("Invalid approved model list")
    selected: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("model"), str)
            or not entry["model"]
            or entry["model"] in selected
        ):
            raise ValueError("Invalid approved model")
        selected.append(entry["model"])
    return selected


def start_time(attempt: Mapping[str, Any], selected: Sequence[str]) -> dt.datetime | None:
    if attempt.get("start_requested") is not True:
        return None
    started_at = timestamp(attempt.get("start_requested_at"))
    if started_at is None or not selected:
        raise ValueError("Invalid persisted run start")
    return started_at


def run_root(arguments: argparse.Namespace) -> Path:
    return (
        Path(arguments.run_root or os.environ.get("CODEX_BAKEOFF_RUN_ROOT", DEFAULT_RUN_ROOT))
        .expanduser()
        .resolve()
    )


def require_session(session: str) -> None:
    if re.fullmatch(r"[a-f0-9]{32}", session) is None:
        raise ValueError("Invalid Codex Bakeoff controller session ID")


def observer_started(stage: str, session: str) -> None:
    print(
        json.dumps({"observer_started": True, "stage": stage, "controller_session_id": session}),
        flush=True,
    )


def analytics_available() -> bool:
    if os.environ.get(METRICS_OUTPUT_ENV):
        return True
    print(json.dumps({"observer_started": False, "reason": "analytics_unavailable"}), flush=True)
    return False


def open_sidecar() -> BinaryIO | None:
    """Pin the host-owned inode before waiting; never create or follow a link."""
    output = os.environ.get(METRICS_OUTPUT_ENV)
    return os.fdopen(os.open(output, os.O_WRONLY | os.O_NOFOLLOW), "wb") if output else None


def write_sidecar(destination: BinaryIO, payload: bytes) -> None:
    if len(payload) > MAX_OUTPUT_BYTES:
        raise ValueError("Replay measurements exceed the sidecar limit")
    destination.truncate()
    destination.write(payload)


def persisted_pid(attempt: Mapping[str, Any], previous: int | None) -> int | None:
    pid = attempt.get("controller_pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else previous


def controller_dead(
    root: Path, session: str, pid: int | None, *, stopped: bool = False
) -> tuple[bool, int | None]:
    # A durable shutdown receipt is authoritative even before the PID is reaped
    # or after that PID has been reused by another process.
    if stopped:
        return True, pid
    # Missing runtime metadata alone is not evidence that the supervisor died.
    runtime = observed_json(root / "controller-server.json")
    if runtime is not None and runtime.get("controller_session_id") == session:
        candidate = runtime.get("pid")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            pid = candidate
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True, pid
        except PermissionError:
            pass
    return False, pid


def model_state(entry: Mapping[str, Any], run_root: Path, session: str) -> Mapping[str, Any] | None:
    run_id = entry["run_id"]
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is None
    ):
        raise ValueError("Invalid run ID")
    directory = run_root / run_id
    if directory.resolve().parent != run_root or entry.get("run_directory") != str(directory):
        raise ValueError("Invalid run directory")
    state = observed_json(directory / STATE_NAME)
    if state is not None and (
        state.get("controller_session_id") != session or state.get("model") != entry["model"]
    ):
        raise ValueError("Invalid run owner")
    return state


def observation_deadline(started_at: dt.datetime, timeout_seconds: float) -> float:
    elapsed = max(0, (dt.datetime.now(dt.timezone.utc) - started_at).total_seconds())
    return time.monotonic() + timeout_seconds - elapsed


def pause(poll_interval: float, deadline: float | None = None) -> None:
    delay = poll_interval
    if deadline is not None:
        delay = min(delay, max(0, deadline - time.monotonic()))
    time.sleep(max(0.01, delay))
