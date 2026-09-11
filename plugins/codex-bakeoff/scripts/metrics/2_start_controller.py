#!/usr/bin/env python3
"""Observe MCP-owned controller startup; keep the declared metrics script path."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_metrics_common as common  # noqa: E402

LaunchOutcome = Literal["ready", "startup_failure", "launch_unobserved"]


def _observe_startup(
    root: Path, session: str, *, timeout_seconds: float, poll_interval_seconds: float
) -> LaunchOutcome:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            (root / "attempt.json").stat()
            attempt = common.read_attempt(root / "attempt.json", session)
            if attempt.get("startup_failed") is True:
                return "startup_failure"
            if attempt.get("controller_ready") is True:
                return "ready"
        except FileNotFoundError:
            # The observer is armed before the MCP call creates the attempt.
            pass
        except (OSError, ValueError, UnicodeError):
            return "launch_unobserved"
        if time.monotonic() >= deadline:
            return "launch_unobserved"
        common.pause(poll_interval_seconds, deadline)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-session-id")
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--startup-timeout-seconds", type=common.nonnegative_seconds, default=60)
    parser.add_argument("--poll-interval-seconds", type=common.nonnegative_seconds, default=1)
    arguments = parser.parse_args(argv)
    session = arguments.controller_session_id or secrets.token_hex(16)
    try:
        common.require_session(session)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    root = common.run_root(arguments).parent / "controllers" / session
    sidecar = None
    sidecar_error = False
    try:
        sidecar = common.open_sidecar()
    except OSError as error:
        print(f"Unable to open the trusted Replay sidecar: {error.strerror}.", file=sys.stderr)
        sidecar_error = True
    # Always return an identity, even on hosts without analytics. Only MCP launches.
    print(
        json.dumps(
            {
                "observer_started": sidecar is not None,
                "stage": "controller_launch",
                "controller_session_id": session,
                "analytics_available": sidecar is not None,
            }
        ),
        flush=True,
    )
    if sidecar is None:
        return 1 if sidecar_error else 0
    try:
        with sidecar:
            outcome = _observe_startup(
                root,
                session,
                timeout_seconds=arguments.startup_timeout_seconds,
                poll_interval_seconds=arguments.poll_interval_seconds,
            )
            payload = common.encode_measurements(
                [common.measurement("controller_launch", 1, {"outcome": outcome})]
            )
            common.write_sidecar(sidecar, payload)
    except (OSError, ValueError) as error:
        print(f"Unable to write the trusted Replay sidecar: {error}.", file=sys.stderr)
        return 1
    common.preserve_payload(root / "metrics-launch.json", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
