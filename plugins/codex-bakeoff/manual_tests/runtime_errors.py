#!/usr/bin/env python3
"""Local real-error E2E tests for the existing numeric metrics contract."""

# ruff: noqa: T201
from __future__ import annotations

import argparse
import http.server
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def reset_provider(root):
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            # Real TCP reset, not an injected exception or prerecorded SDK event.
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    home = root / "isolated-codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "diagnostic_fixture"\n'
        "[model_providers.diagnostic_fixture]\n"
        'name = "diagnostic_fixture"\n'
        f'base_url = "http://127.0.0.1:{httpd.server_port}/v1"\n'
        'wire_api = "responses"\n'
        'env_key = "CODEX_BAKEOFF_FIXTURE_API_KEY"\n'
        "request_max_retries = 0\nstream_max_retries = 0\n"
    )
    previous = {
        name: os.environ.get(name) for name in ("CODEX_HOME", "CODEX_BAKEOFF_FIXTURE_API_KEY")
    }
    os.environ.update(CODEX_HOME=str(home), CODEX_BAKEOFF_FIXTURE_API_KEY="synthetic-local-only")
    try:
        yield requests
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def e2e():
    """Real coordinator + child worker + disk state + exporter; only host sink intercepted."""
    server = load("live_server", ROOT / "mcp/server.py")
    failures = 0
    # Invalid syntax is rejected by the real worker. Missing repository is rejected
    # by the real workspace materializer. No patched functions or fake error objects.
    scenarios = (
        (
            "worker_validation",
            {
                "model": "invalid model",
                "prompt": "Synthetic diagnostic test",
                "target": {"type": "projectless"},
            },
            "invalid_request",
        ),
        (
            "missing_repository",
            {
                "model": "gpt-5.6-sol",
                "prompt": "Synthetic diagnostic test",
                "target": {"type": "project", "project": "/nonexistent-replay-diagnostic-fixture"},
            },
            "unknown",
        ),
        (
            "tcp_reset",
            {
                "model": "gpt-5.6-sol",
                "prompt": "Synthetic diagnostic test",
                "target": {"type": "projectless"},
            },
            "stream_error",
        ),
    )
    for name, task, expected_code in scenarios:
        with tempfile.TemporaryDirectory(prefix="replay-live-errors-") as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            run_dir = run_root / "failure"
            run_dir.mkdir(parents=True)
            session = "a" * 32
            server.RUN_ROOT = run_root
            server.CONTROLLER_SESSION_ID = session
            state = server._initial_state(run_dir)
            state.update(
                {
                    "model": task["model"],
                    "models": [task["model"]],
                    "controller_session_id": session,
                }
            )
            server._write_json(run_dir / server.STATE_NAME, state)
            server._write_json(run_dir / server.COORDINATOR_REQUEST_NAME, task)
            server._update_attempt(
                controller_ready=True,
                start_requested=True,
                start_requested_at=server._utc_now(),
                models=[
                    {
                        "model": task["model"],
                        "launch_status": "started",
                        "run_id": run_dir.name,
                        "run_directory": str(run_dir),
                    }
                ],
            )

            def run_coordinator(run_directory: Path = run_dir) -> None:
                server._spawn_coordinator(run_directory)
                with server._active_processes_lock:
                    thread = server._run_threads.get(run_directory.name)
                if thread is not None:
                    thread.join(timeout=60)
                    if thread.is_alive():
                        raise TimeoutError("Comparison supervisor did not finish")

            if name == "tcp_reset":
                with reset_provider(root) as requests:
                    run_coordinator()
                if not requests:
                    print(
                        json.dumps(
                            {
                                "e2e": name,
                                "passed": False,
                                "reason": "CLI did not reach fault server",
                            }
                        ),
                        flush=True,
                    )
                    failures += 1
                    continue
            else:
                run_coordinator()
            state = json.loads((run_dir / server.STATE_NAME).read_text())
            sink = root / "intercepted-metrics.json"
            sink.touch(mode=0o600)
            environment = dict(
                os.environ,
                CODEX_PLUGIN_METRICS_OUTPUT=str(sink),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/metrics/4_report_final_metrics.py"),
                    "--controller-session-id",
                    session,
                    "--run-root",
                    str(run_root),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            payload = json.loads(sink.read_text())
            failure = next(row for row in payload["measurements"] if row["name"] == "failure")
            rows = {row["name"]: row for row in payload["measurements"]}
            passed = (
                result.returncode == 0
                and state["status"] == "failed"
                and failure["dimensions"]["worker_code"] == expected_code
                and failure["dimensions"]["controller_code"]
                == ("controller_error" if name == "missing_repository" else "none")
                and "reason" not in failure["dimensions"]
                and rows["attempt"]["dimensions"]["outcome"] == "failed"
                and rows["attempt"]["dimensions"]["controller_ready"] == "yes"
                and rows["attempt"]["dimensions"]["run_observed"] == "yes"
                and set(payload) == {"version", "measurements"}
                and str(ROOT) not in json.dumps(payload)
                and "127.0.0.1" not in json.dumps(payload)
            )
            if name in {"tcp_reset", "worker_validation"}:
                passed = (
                    passed
                    and rows["failure_exit_code"]["value"]
                    == state["failure_diagnostic"]["exit_code"]
                )
            if name == "tcp_reset":
                passed = passed and rows["failure_retry_count"]["value"] == 3
                passed = passed and rows["failure_duration_seconds"]["value"] > 0
                passed = passed and rows["failure_detail"]["dimensions"]["worker_stage"] == "stream"
            failures += not passed
            print(
                json.dumps(
                    {
                        "e2e": name,
                        "passed": passed,
                        "worker_code": failure["dimensions"]["worker_code"],
                        "measurements": [
                            row
                            for row in payload["measurements"]
                            if row["name"].startswith("failure")
                        ],
                    }
                ),
                flush=True,
            )
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Run real local worker/CLI processes")
    args = parser.parse_args()
    if any(
        os.environ.get(name)
        for name in ("CI", "BUILDKITE", "GITHUB_ACTIONS", "TEAMCITY_VERSION", "JENKINS_URL")
    ):
        parser.error("Real-error E2E tests are local-only and refuse CI/CD environments")
    if not args.run:
        parser.error("Pass --run to execute local E2E tests")
    return int(bool(e2e()))


if __name__ == "__main__":
    raise SystemExit(main())
