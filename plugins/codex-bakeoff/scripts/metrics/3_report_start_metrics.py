#!/usr/bin/env python3
"""Report the approved Replay start once, after a bounded pre-start wait."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_metrics_common as common  # noqa: E402


def _observe_attempt(
    arguments: argparse.Namespace, run_root: Path
) -> tuple[bool, int | None, bool]:
    session = arguments.controller_session_id
    root = run_root.parent / "controllers" / session
    deadline = time.monotonic() + arguments.pre_start_timeout_seconds
    controller_pid: int | None = None
    while True:
        try:
            attempt = common.read_attempt_if_present(root / "attempt.json", session)
            if attempt is not None:
                if attempt.get("startup_failed") is True:
                    return False, None, False
                if attempt.get("start_requested") is True:
                    selected_model_count = None
                    if "models" in attempt:
                        try:
                            selected_model_count = len(common.approved_models(attempt))
                        except ValueError:
                            pass
                    return True, selected_model_count, False
                controller_pid = common.persisted_pid(attempt, controller_pid)
                dead, controller_pid = common.controller_dead(
                    root, session, controller_pid, stopped=attempt.get("controller_stopped") is True
                )
                if dead:
                    return False, None, False
        except (OSError, ValueError, UnicodeError):
            return False, None, False
        if time.monotonic() >= deadline:
            return False, None, True
        common.pause(arguments.poll_interval_seconds, deadline)


def main(argv: Sequence[str] | None = None) -> int:
    if not common.analytics_available():
        return 0
    arguments = common.arguments(argv)
    try:
        common.require_session(arguments.controller_session_id)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    run_root = common.run_root(arguments)
    root = run_root.parent / "controllers" / arguments.controller_session_id
    try:
        sidecar = common.open_sidecar()
        if sidecar is None:
            return 0
        with sidecar:
            common.observer_started("run_start", arguments.controller_session_id)
            started, selected_model_count, timed_out = _observe_attempt(arguments, run_root)
            measurements = [common.measurement("run_start", 1, {})] if started else []
            if selected_model_count is not None:
                measurements.append(
                    common.measurement("selected_model_count", selected_model_count, {})
                )
            payload = common.encode_measurements(
                measurements
                if measurements
                else [common.measurement("run_start_timeout", 1, {})]
                if timed_out
                else []
            )
            common.write_sidecar(sidecar, payload)
    except (OSError, ValueError) as error:
        print(f"Unable to write the trusted Replay sidecar: {error}.", file=sys.stderr)
        return 1
    common.preserve_payload(root / "metrics-start.json", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
