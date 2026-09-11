"""Decide when a Replay attempt has durable final artifacts for every model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ReadState = Callable[[Path], Mapping[str, Any]]
WriteState = Callable[[Path, Mapping[str, Any]], None]


class Lock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> object: ...


class AttemptState:
    def __init__(
        self,
        *,
        attempt_path: Callable[[], Path],
        run_root: Callable[[], Path],
        controller_session_id: Callable[[], str],
        state_name: str,
        lock: Lock,
        read_attempt: ReadState,
        read_state: ReadState,
        write_json: WriteState,
        now: Callable[[], str],
    ) -> None:
        self._attempt_path = attempt_path
        self._run_root = run_root
        self._controller_session_id = controller_session_id
        self._state_name = state_name
        self._lock = lock
        self._read_attempt = read_attempt
        self._read_state = read_state
        self._write_json = write_json
        self._now = now

    def update(self, changes: Mapping[str, Any]) -> None:
        with self._lock:
            path = self._attempt_path()
            attempt = (
                dict(self._read_attempt(path))
                if path.exists()
                else {
                    "version": 1,
                    "controller_session_id": self._controller_session_id(),
                    "created_at": self._now(),
                }
            )
            attempt.update(changes)
            self._write_json(path, attempt)

    def record_model_launch(self, model: str, changes: Mapping[str, Any]) -> None:
        with self._lock:
            path = self._attempt_path()
            attempt = dict(self._read_attempt(path))
            for entry in attempt["models"]:
                if entry["model"] == model:
                    entry.update(changes)
                    self._write_json(path, attempt)
                    self.refresh()
                    return

    def refresh(self) -> None:
        try:
            with self._lock:
                path = self._attempt_path()
                if not path.is_file():
                    return
                attempt = dict(self._read_attempt(path))
                if (
                    attempt.get("final_results_ready") is True
                    or attempt.get("start_requested") is not True
                    or not is_ready(
                        attempt,
                        run_root=self._run_root(),
                        controller_session_id=self._controller_session_id(),
                        state_name=self._state_name,
                        read_state=self._read_state,
                    )
                ):
                    return
                attempt.update(final_results_ready=True, final_results_ready_at=self._now())
                self._write_json(path, attempt)
        except (OSError, ValueError, UnicodeError):
            return


def is_ready(
    attempt: Mapping[str, Any],
    *,
    run_root: Path,
    controller_session_id: str,
    state_name: str,
    read_state: ReadState,
) -> bool:
    entries = attempt.get("models")
    if not isinstance(entries, list) or not entries:
        return False
    for entry in entries:
        if not isinstance(entry, Mapping):
            return False
        if entry.get("launch_status") == "failed":
            continue
        run_id = entry.get("run_id")
        if not isinstance(run_id, str):
            return False
        run_directory = (run_root / run_id).resolve()
        if run_directory.parent != run_root or not run_directory.is_dir():
            return False
        try:
            state = read_state(run_directory / state_name)
        except (OSError, ValueError, UnicodeError):
            return False
        if (
            state.get("controller_session_id") != controller_session_id
            or state.get("model") != entry.get("model")
            or state.get("status") not in TERMINAL_STATUSES
        ):
            return False
        if state["status"] == "completed" and not (run_directory / "report.json").is_file():
            return False
    return True
