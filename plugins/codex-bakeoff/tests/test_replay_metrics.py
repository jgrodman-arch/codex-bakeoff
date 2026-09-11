"""Focused coverage for trusted, host-compatible numeric Replay measurements."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import math
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "metrics" / "4_report_final_metrics.py"
LAUNCH_SCRIPT = PLUGIN_ROOT / "scripts" / "metrics" / "2_start_controller.py"
OBSERVER_SCRIPTS = {
    "run_start": PLUGIN_ROOT / "scripts" / "metrics" / "3_report_start_metrics.py",
    "final_results": SCRIPT,
}


def load_reporter(stage: str, script: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"codex_bakeoff_metrics_test_{stage}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPORTERS = {stage: load_reporter(stage, script) for stage, script in OBSERVER_SCRIPTS.items()}
metrics = REPORTERS["final_results"]
common = metrics.common
PROBE_SCRIPT = PLUGIN_ROOT / "scripts" / "metrics" / "1_probe_metrics.py"
LOCAL_METRICS_NAMES = {
    "run_start": "metrics-start.json",
    "final_results": "metrics.json",
}

CONTROLLER_SESSION_ID = "a" * 32
SAMPLE_ID = "jqlang__jq-2157--claude-sonnet-5"
SAMPLE_SESSION_ID = "4c7ed7ad-e18b-4686-b2db-2e136e7acb1c"
SAMPLE_COMMIT = "f88c4e5888d6d125695444d044df4bb55ad75888"
DIMENSION_SCORES = {
    "request_fulfillment": (0.75, 1.0),
    "code_quality": (1.0, 0.8),
    "change_scope": (0.5, 0.9),
    "reliability": (0.25, 0.8),
    "safe_operations": (1.0, 1.0),
    "accurate_reporting": (0.5, 0.7),
}


def _report(*, sample: bool = False) -> dict[str, Any]:
    dimensions: dict[str, Any] = {}
    for name, (claude, codex) in DIMENSION_SCORES.items():
        dimensions[name] = {
            "winner": "tie" if claude == codex else "A" if claude > codex else "B",
            "candidates": {
                "A": {
                    "score": claude,
                    "checks": {check: 1 for check in metrics.REVIEW_CHECKS[name]},
                },
                "B": {
                    "score": codex,
                    "checks": {check: 1 for check in metrics.REVIEW_CHECKS[name]},
                },
            },
        }
    report: dict[str, Any] = {
        "schema_version": 3,
        "candidates": {
            "claude": {"model": "claude-sonnet-5"},
            "codex": {"model": "gpt-5.6-sol"},
        },
        "usage": {
            "codex": [
                {
                    "provider": "openai",
                    "input_tokens": 1200,
                    "output_tokens": 340,
                    "cached_input_tokens": 80,
                }
            ],
            "claude": [
                {
                    "provider": "anthropic",
                    "input_tokens": 700,
                    "output_tokens": 210,
                    "cached_input_tokens": 50,
                }
            ],
        },
        "normalized_usage": {
            "codex": {
                "total_input_tokens": 1200,
                "ordinary_input_tokens": 1120,
                "output_tokens": 340,
                "cached_input_tokens": 80,
                "cache_write_tokens": 0,
            },
            "claude": {
                "total_input_tokens": 760,
                "ordinary_input_tokens": 700,
                "output_tokens": 210,
                "cached_input_tokens": 50,
                "cache_write_tokens": 10,
            },
        },
        "estimated_cost": {
            "codex": {"status": "estimated", "usd": 0.18},
            "claude": {"status": "estimated", "usd": 0.29},
        },
        "codex_execution": {"elapsed_seconds": 24.5},
        "historical_model_request_seconds": 12.4,
        "historical_wall_clock_seconds": 18.9,
        "evaluation": {
            "status": "completed",
            "candidate_mapping": {"A": "claude", "B": "codex"},
            "totals": {"A": 0.795, "B": 0.918},
            "reviews": [{"status": "completed", "ballot": {"dimensions": dimensions}}],
        },
    }
    if sample:
        report["recorded_claude_result"] = {
            "duration_ms": 19_200,
            "duration_api_ms": 12_400,
            "usage": {"input_tokens": 700},
            "modelUsage": {"claude-sonnet-5": {"inputTokens": 700}},
        }
    return report


class ReplayMetricsTests(unittest.TestCase):
    def test_unstructured_worker_exits_reach_failure_metrics(self) -> None:
        from test_mcp_server import load_server

        server = load_server()
        for source, exit_code in (
            ("process.exit(23);", 23),
            ("process.kill(process.pid, 'SIGKILL');", -9),
            ("process.exit(0);", 0),
        ):
            with self.subTest(exit_code=exit_code):
                directory = self.create_run(status="running")
                state = server._initial_state(directory)
                state.update(model="gpt-5.6-sol", controller_session_id=CONTROLLER_SESSION_ID)
                server._write_json(server._state_path(directory), state)
                self.attempt([self.entry(directory)], final_results_ready=False)
                worker = self.root / "crashing-worker.mjs"
                worker.write_text(source, encoding="utf-8")
                # Real process completion, including SIGKILL without a failed JSON record.
                with (
                    mock.patch.object(server, "WORKER", worker),
                    mock.patch.object(server, "RUN_ROOT", self.run_root),
                    mock.patch.object(server, "CONTROLLER_SESSION_ID", CONTROLLER_SESSION_ID),
                ):
                    server._coordinator(
                        directory,
                        {
                            "model": "gpt-5.6-sol",
                            "prompt": "test worker exit",
                            "target": {"type": "projectless"},
                        },
                    )
                attempt = json.loads(
                    (self.root / "controllers" / CONTROLLER_SESSION_ID / "attempt.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertIs(attempt["final_results_ready"], True)

                state = server._read_json(server._state_path(directory))
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["implementation_attempt"], 1)
                result, payload = self.invoke()
                self.assertEqual(result, 0)
                rows = {row["name"]: row for row in payload["measurements"]}
                self.assertEqual(rows["failure_exit_code"]["value"], exit_code)
                self.assertEqual(rows["failure_retry_count"]["value"], 0)
                self.assertEqual(rows["failure"]["dimensions"]["worker_code"], "worker_failed")
                self.assertEqual(rows["failure"]["dimensions"]["controller_code"], "none")
                self.assertEqual(rows["failure"]["dimensions"]["retryable"], "unknown")
                self.assertEqual(rows["failure_detail"]["dimensions"]["worker_stage"], "unknown")
                self.assertNotIn("failure_duration_seconds", rows)

    def test_terminal_state_survives_final_receipt_write_failure(self) -> None:
        from test_mcp_server import load_server

        server = load_server()
        directory = self.create_run(status="running", report=_report())
        attempt_path = self.attempt([self.entry(directory)], final_results_ready=False)
        with (
            mock.patch.object(server, "RUN_ROOT", self.run_root),
            mock.patch.object(server, "CONTROLLER_SESSION_ID", CONTROLLER_SESSION_ID),
            mock.patch.object(
                server._attempt_state,
                "_write_json",
                side_effect=PermissionError("receipt unavailable"),
            ),
        ):
            state = server._update_state(directory, status="completed")

        self.assertEqual(state["status"], "completed")
        self.assertEqual(server._read_json(server._state_path(directory))["status"], "completed")
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        self.assertIs(attempt["final_results_ready"], False)

    def test_interruption_does_not_export_previous_retry_diagnostic(self) -> None:
        from test_mcp_server import load_server

        server = load_server()
        for phase in ("implementing", "collecting", "reviewing", "reporting"):
            with self.subTest(phase=phase):
                directory = self.create_run(
                    status="running",
                    state_overrides={
                        "phase": phase,
                        "failure_diagnostic": {
                            "worker_code": "stream_error",
                            "retryable": True,
                            "system_code": "ECONNRESET",
                            "worker_stage": "stream",
                            "exit_code": 1,
                            "retry_count": 2,
                            "elapsed_ms": 5000,
                        },
                    },
                )
                self.attempt([self.entry(directory)], final_results_ready=False)
                with (
                    mock.patch.object(server, "RUN_ROOT", self.run_root),
                    mock.patch.object(server, "CONTROLLER_SESSION_ID", CONTROLLER_SESSION_ID),
                ):
                    server._mark_interrupted(directory, "The coordinator stopped.")

                result, payload = self.invoke()
                self.assertEqual(result, 0)
                rows = {row["name"]: row for row in payload["measurements"]}
                self.assertEqual(rows["attempt"]["dimensions"]["outcome"], "failed")
                self.assertEqual(rows["failure"]["dimensions"]["worker_code"], "unknown")
                self.assertEqual(
                    rows["failure"]["dimensions"]["controller_code"], "coordinator_stopped"
                )
                self.assertEqual(rows["failure"]["dimensions"]["retryable"], "unknown")
                self.assertEqual(rows["failure_detail"]["dimensions"]["system_code"], "unknown")
                self.assertEqual(
                    rows["failure_detail"]["dimensions"]["worker_stage"], "outside_worker"
                )
                self.assertTrue(
                    {
                        "failure_exit_code",
                        "failure_retry_count",
                        "failure_duration_seconds",
                    }.isdisjoint(rows)
                )

    def test_structured_failure_codes_are_allowlisted_and_text_stays_local(self) -> None:
        for code, expected in (("stream_error", "stream_error"), ("private-customer", "unknown")):
            with self.subTest(code=code):
                run = metrics.ReplayRun(
                    model="gpt-5.6-sol",
                    state={
                        "status": "failed",
                        "phase": "implementing",
                        "failure_diagnostic": {
                            "worker_code": code,
                            "controller_code": "none"
                            if expected != "unknown"
                            else "private-customer",
                            "retryable": True,
                            "system_code": "ECONNRESET"
                            if expected != "unknown"
                            else "private-customer",
                            "worker_stage": "stream"
                            if expected != "unknown"
                            else "private-customer",
                            "exit_code": -15,
                            "retry_count": 2,
                            "elapsed_ms": 2500,
                            "evidence": {"message": "private-customer"},
                        },
                    },
                    run={},
                    report={},
                )
                payload = json.loads(metrics._payload(metrics._replay_event([run])))
                failure = next(row for row in payload["measurements"] if row["name"] == "failure")
                self.assertEqual(failure["dimensions"]["worker_code"], expected)
                self.assertEqual(
                    failure["dimensions"]["controller_code"],
                    "none" if expected != "unknown" else "unknown",
                )
                self.assertEqual(failure["dimensions"]["retryable"], "yes")
                rows = {row["name"]: row for row in payload["measurements"]}
                detail = rows["failure_detail"]["dimensions"]
                self.assertEqual(
                    detail["system_code"], "econnreset" if expected != "unknown" else "unknown"
                )
                self.assertEqual(
                    detail["worker_stage"], "stream" if expected != "unknown" else "unknown"
                )
                self.assertEqual(rows["failure_exit_code"]["value"], -15)
                self.assertEqual(rows["failure_retry_count"]["value"], 2)
                self.assertEqual(rows["failure_duration_seconds"]["value"], 2.5)
                self.assertNotIn("private-customer", json.dumps(payload))

    def test_failure_messages_do_not_change_structured_metrics(self) -> None:
        for diagnostic in (
            {"worker_code": "stream_error", "controller_code": "none"},
            {"controller_code": "controller_error", "worker_stage": "outside_worker"},
        ):
            baseline = None
            for message in (
                "",
                "timed out",
                "model access unavailable",
                "worker failed",
                "coordinator review failed",
                "private-customer",
            ):
                with self.subTest(diagnostic=diagnostic, message=message):
                    run = metrics.ReplayRun(
                        model="gpt-5.6-sol",
                        state={
                            "status": "failed",
                            "phase": "implementing",
                            "error": message,
                            "failure_diagnostic": diagnostic,
                        },
                        run={},
                        report={},
                    )
                    payload = metrics._payload(metrics._replay_event([run]))
                    if baseline is None:
                        baseline = payload
                    self.assertEqual(payload, baseline)

    def test_missing_or_invalid_failure_numbers_are_not_fabricated(self) -> None:
        for diagnostic in (
            {},
            {"exit_code": True, "retry_count": 4, "elapsed_ms": -1},
            {"exit_code": 999, "retry_count": "private", "elapsed_ms": float("nan")},
        ):
            with self.subTest(diagnostic=diagnostic):
                run = metrics.ReplayRun(
                    model="gpt-5.6-sol",
                    state={
                        "status": "failed",
                        "failure_diagnostic": diagnostic,
                    },
                    run={},
                    report={},
                )
                payload = json.loads(metrics._payload(metrics._replay_event([run])))
                names = {row["name"] for row in payload["measurements"]}
                self.assertTrue(
                    {
                        "failure_exit_code",
                        "failure_retry_count",
                        "failure_duration_seconds",
                    }.isdisjoint(names)
                )

    def attempt(self, entries: list[dict[str, object]], **changes: object) -> Path:
        path = self.root / "controllers" / CONTROLLER_SESSION_ID / "attempt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "controller_session_id": CONTROLLER_SESSION_ID,
                    "controller_ready": True,
                    "start_requested": True,
                    "start_requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "final_results_ready": True,
                    "models": entries,
                    **changes,
                }
            )
        )
        temporary.replace(path)
        return path

    def entry(self, directory: Path) -> dict[str, object]:
        state = json.loads((directory / common.STATE_NAME).read_text())
        return {
            "model": state["model"],
            "run_id": directory.name,
            "run_directory": str(directory),
            "launch_status": "started",
        }

    def test_background_observers_report_independent_milestones_after_delayed_go(self) -> None:
        from test_mcp_server import load_server

        server = load_server()
        models = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        sidecars: dict[str, Path] = {}
        inodes: dict[str, int] = {}
        observers: dict[str, subprocess.Popen[str]] = {}

        def stop_observer(process: subprocess.Popen[str]) -> None:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

        for stage, script in OBSERVER_SCRIPTS.items():
            sidecar = self.root / f"host-{stage}.json"
            sidecar.write_text("original", encoding="utf-8")
            sidecars[stage] = sidecar
            inodes[stage] = sidecar.stat().st_ino
            environment = dict(os.environ)
            environment.pop(common.METRICS_OUTPUT_ENV, None)
            environment[common.METRICS_OUTPUT_ENV] = str(sidecar)
            environment["PYTHONSAFEPATH"] = "1"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--controller-session-id",
                    CONTROLLER_SESSION_ID,
                    "--run-root",
                    str(self.run_root),
                    "--run-timeout-seconds",
                    "10",
                    "--poll-interval-seconds",
                    "0.01",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            observers[stage] = process
            self.addCleanup(stop_observer, process)

        # Dispatch all observers before the MCP-owned attempt exists, as the skill does.
        for stage, process in observers.items():
            assert process.stdout is not None
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], stage)
            self.assertEqual(
                json.loads(process.stdout.readline()),
                {
                    "observer_started": True,
                    "stage": stage,
                    "controller_session_id": CONTROLLER_SESSION_ID,
                },
            )

        # All processes keep waiting before MCP creates the attempt.
        time.sleep(0.05)
        for stage, process in observers.items():
            self.assertIsNone(process.poll(), stage)
            self.assertEqual(sidecars[stage].read_text(encoding="utf-8"), "original")

        self.attempt(
            [],
            start_requested=False,
            start_requested_at=None,
            final_results_ready=False,
            controller_pid=os.getpid(),
        )
        time.sleep(0.05)
        for stage in ("run_start", "final_results"):
            self.assertIsNone(observers[stage].poll(), stage)
            self.assertEqual(sidecars[stage].read_text(encoding="utf-8"), "original")

        directories = [
            self.create_run(model=model, models=models, status="running", report=_report())
            for model in models
        ]
        self.attempt(
            [self.entry(path) for path in directories],
            final_results_ready=False,
            controller_pid=os.getpid(),
        )
        _, stderr = observers["run_start"].communicate(timeout=5)
        self.assertEqual(observers["run_start"].returncode, 0, stderr)
        started_bytes = sidecars["run_start"].read_bytes()
        self.assertEqual(
            json.loads(started_bytes)["measurements"],
            [
                {
                    "name": "run_start",
                    "value": 1,
                },
                {
                    "name": "selected_model_count",
                    "value": 3,
                },
            ],
        )
        self.assertIsNone(observers["final_results"].poll())
        self.assertEqual(sidecars["final_results"].read_text(encoding="utf-8"), "original")

        with (
            mock.patch.object(server, "RUN_ROOT", self.run_root),
            mock.patch.object(server, "CONTROLLER_SESSION_ID", CONTROLLER_SESSION_ID),
        ):
            # Completing one model does not finish the final observer.
            server._update_state(directories[1], status="completed")
            with self.assertRaises(subprocess.TimeoutExpired):
                observers["final_results"].wait(timeout=0.05)
            self.assertEqual(sidecars["final_results"].read_text(encoding="utf-8"), "original")
            server._update_state(directories[0], status="failed")
            server._update_state(directories[2], status="cancelled")
            attempt = json.loads(
                (self.root / "controllers" / CONTROLLER_SESSION_ID / "attempt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIs(attempt["final_results_ready"], True)

        _, stderr = observers["final_results"].communicate(timeout=5)
        self.assertEqual(observers["final_results"].returncode, 0, stderr)
        final = json.loads(sidecars["final_results"].read_bytes())
        rows = final["measurements"]
        replay = self.replay(final)
        self.assertEqual(
            [model["status"] for model in replay["codex_models"]],
            ["failed", "completed", "cancelled"],
        )
        self.assertEqual(rows[0]["dimensions"]["outcome"], "mixed")
        self.assertEqual(rows[0]["dimensions"]["reporting_complete"], "yes")
        self.assertEqual(sum(row["name"] == "replay" for row in rows), 3)
        self.assertTrue(
            {"run_start", "selected_model_count"}.isdisjoint(row["name"] for row in rows)
        )
        for name in (
            "claude_input_tokens",
            "claude_output_tokens",
            "claude_cached_input_tokens",
            "claude_cost_usd",
            "claude_duration_seconds",
            "claude_wall_clock_seconds",
        ):
            shared = [row for row in rows if row["name"] == name]
            self.assertEqual(len(shared), 1, name)
            self.assertNotIn("model_slot", shared[0]["dimensions"])
        self.assertEqual(sidecars["run_start"].read_bytes(), started_bytes)
        for stage, sidecar in sidecars.items():
            self.assertEqual(sidecar.stat().st_ino, inodes[stage], stage)
            local = self.root / "controllers" / CONTROLLER_SESSION_ID / LOCAL_METRICS_NAMES[stage]
            self.assertEqual(local.read_bytes(), sidecar.read_bytes(), stage)

    def test_run_start_reports_only_start_and_approved_model_count(self) -> None:
        for count in (1, 2, 8):
            with self.subTest(count=count):
                self.attempt(
                    [
                        {"model": f"gpt-5.6-sol-{index}", "launch_status": "pending"}
                        for index in range(count)
                    ]
                )
                code, payload = self.invoke(stage="run_start")
                self.assertEqual(code, 0)
                self.assertEqual(
                    payload,
                    {
                        "version": 1,
                        "measurements": [
                            {
                                "name": "run_start",
                                "value": 1,
                            },
                            {
                                "name": "selected_model_count",
                                "value": count,
                            },
                        ],
                    },
                )

        self.attempt(
            [{"model": "gpt-5.6-sol", "launch_status": "pending"}],
            start_requested_at=None,
        )
        code, payload = self.invoke(stage="run_start")
        self.assertEqual(code, 0)
        self.assertEqual(
            [row["name"] for row in payload["measurements"]],
            ["run_start", "selected_model_count"],
        )

        self.attempt([{"launch_status": "pending"}])
        code, payload = self.invoke(stage="run_start")
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["measurements"],
            [
                {
                    "name": "run_start",
                    "value": 1,
                }
            ],
        )

    def test_prestart_observers_report_explicit_timeout(self) -> None:
        for stage, measurement in (
            ("run_start", "run_start_timeout"),
            ("final_results", "final_prestart_timeout"),
        ):
            with self.subTest(stage=stage):
                self.output.write_text("original", encoding="utf-8")
                code, payload = self.invoke(
                    "--pre-start-timeout-seconds",
                    "0",
                    stage=stage,
                )
                self.assertEqual(code, 0)
                self.assertEqual(
                    payload,
                    {
                        "version": 1,
                        "measurements": [{"name": measurement, "value": 1}],
                    },
                )

    def test_final_observer_deadline_begins_at_the_persisted_start(self) -> None:
        started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
        directory = self.create_run(status="running")
        self.attempt(
            [self.entry(directory)],
            start_requested_at=started_at.isoformat(),
            final_results_ready=False,
        )
        with mock.patch.object(
            metrics.time,
            "sleep",
            side_effect=AssertionError("An expired run must not receive another deadline"),
        ):
            code, payload = self.invoke("--run-timeout-seconds", "30")
        self.assertEqual(code, 0)
        rows = payload["measurements"]
        self.assertEqual(rows[0]["dimensions"]["outcome"], "observation_timeout")
        self.assertEqual(rows[0]["dimensions"]["reporting_complete"], "no")
        self.assertEqual(rows[1]["name"], "attempt_model")
        self.assertEqual(rows[1]["dimensions"]["status"], "unresolved")

    def test_unexpected_observer_failure_is_nonzero_and_preserves_host_file(self) -> None:
        self.attempt([{"model": "gpt-5.6-sol", "launch_status": "pending"}])
        for stage in OBSERVER_SCRIPTS:
            with (
                self.subTest(stage=stage),
                mock.patch.object(
                    REPORTERS[stage],
                    "_observe_attempt",
                    side_effect=ValueError("reporter failed"),
                ),
                mock.patch("sys.stderr"),
            ):
                code, payload = self.invoke(stage=stage)
                self.assertEqual(code, 1)
                self.assertEqual(payload, {})
                self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_attempt_outcomes_account_for_every_approved_model(self) -> None:
        models = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        cases = [
            (["completed"] * 3, "succeeded"),
            (["failed", "completed", "completed"], "mixed"),
            (["failed"] * 3, "failed"),
            (["cancelled"] * 3, "cancelled"),
            (["launch_failed"] * 3, "launch_failure"),
            (["completed", "launch_failed", "cancelled"], "mixed"),
            (["running", "completed", "failed"], "observation_timeout"),
            (["absent"] * 3, "observation_timeout"),
            (["unreadable", "completed", "failed"], "unreadable_state"),
            (["report_absent", "completed", "failed"], "observation_timeout"),
        ]
        for statuses, outcome in cases:
            with self.subTest(statuses=statuses):
                entries = []
                for model, status in zip(models, statuses):
                    if status in {"launch_failed", "absent"}:
                        entries.append(
                            {
                                "model": model,
                                "launch_status": "failed"
                                if status == "launch_failed"
                                else "pending",
                                "controller_code": "launch_failed"
                                if status == "launch_failed"
                                else None,
                            }
                        )
                        continue
                    directory = self.create_run(
                        model=model,
                        models=models,
                        status="completed" if status in {"unreadable", "report_absent"} else status,
                    )
                    entries.append(self.entry(directory))
                    if status == "unreadable":
                        (directory / common.STATE_NAME).write_text("{broken")
                    if status == "report_absent":
                        (directory / "report.json").unlink()
                self.attempt(entries, final_results_ready=outcome != "observation_timeout")
                code, payload = self.invoke()
                self.assertEqual(code, 0)
                rows = payload["measurements"]
                summary = [row for row in rows if row["name"] == "attempt"]
                self.assertEqual(len(summary), 1)
                self.assertEqual(summary[0]["dimensions"]["outcome"], outcome)
                accounted = {
                    row["dimensions"]["model_slot"]
                    for row in rows
                    if row["name"] in {"replay", "attempt_model"}
                }
                self.assertEqual(accounted, {"one", "two", "three"})
                for index, status in enumerate(statuses):
                    if status == "launch_failed":
                        failure = next(
                            row
                            for row in rows
                            if row["name"] == "failure"
                            and row["dimensions"]["model_slot"] == metrics.MODEL_SLOTS[index]
                        )
                        self.assertEqual(failure["dimensions"]["controller_code"], "launch_failed")
                self.replay(payload)  # Validate the actual strict host manifest, enums and limits.

    def test_sol_failure_waits_for_both_other_models(self) -> None:
        models = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        directories = [
            self.create_run(model=model, models=models, status="failed" if i == 0 else "running")
            for i, model in enumerate(models)
        ]
        attempt_path = self.attempt(
            [self.entry(path) for path in directories], final_results_ready=False
        )

        def finish() -> None:
            for directory in directories[1:]:
                (directory / "report.json").write_text(json.dumps(_report()))
                state_path = directory / common.STATE_NAME
                state = json.loads(state_path.read_text())
                state["status"] = "completed"
                temporary = state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(state))
                temporary.replace(state_path)
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["final_results_ready"] = True
            temporary = attempt_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(attempt), encoding="utf-8")
            temporary.replace(attempt_path)

        timer = threading.Timer(0.08, finish)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        code, payload = self.invoke("--run-timeout-seconds", "2", "--poll-interval-seconds", "0.01")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.07)
        self.assertEqual(payload["measurements"][0]["dimensions"]["outcome"], "mixed")
        self.assertEqual(len([r for r in payload["measurements"] if r["name"] == "replay"]), 3)

    def test_final_observer_waits_for_attempt_final_results_ready_receipt(self) -> None:
        directory = self.create_run()
        attempt_path = self.attempt([self.entry(directory)], final_results_ready=False)

        def finish() -> None:
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["final_results_ready"] = True
            temporary = attempt_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(attempt), encoding="utf-8")
            temporary.replace(attempt_path)

        timer = threading.Timer(0.08, finish)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        code, payload = self.invoke("--run-timeout-seconds", "2", "--poll-interval-seconds", "0.01")

        self.assertEqual(code, 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.07)
        self.assertEqual(payload["measurements"][0]["dimensions"]["outcome"], "succeeded")

    def test_attempt_budget_preserves_failure_details_and_approved_slots(self) -> None:
        models = ["gpt-5.6-sol", "gpt-5.6-terra"] + [f"gpt-5.6-sol-{i}" for i in range(6)]
        entries = [{"model": models[0], "launch_status": "pending"}]
        for index, model in enumerate(models[1:], start=1):
            directory = self.create_run(
                model=model,
                models=models,
                status="failed" if index == 1 else "completed",
                state_overrides={
                    "failure_diagnostic": {
                        "worker_code": "stream_error",
                        "system_code": "ECONNRESET",
                        "worker_stage": "stream",
                        "retryable": True,
                        "exit_code": 1,
                        "retry_count": 3,
                        "elapsed_ms": 2500,
                    }
                }
                if index == 1
                else {},
            )
            entries.append(self.entry(directory))
        self.attempt(entries, final_results_ready=False)
        code, payload = self.invoke()
        self.assertEqual(code, 0)
        self.replay(payload)  # Actual manifest validation, including row/byte limits.
        rows = payload["measurements"]
        self.assertEqual(rows[0]["dimensions"]["outcome"], "observation_timeout")
        failure_rows = {row["name"]: row for row in rows if row["name"].startswith("failure")}
        self.assertEqual(
            set(failure_rows),
            {
                "failure",
                "failure_detail",
                "failure_exit_code",
                "failure_retry_count",
                "failure_duration_seconds",
            },
        )
        self.assertTrue(
            all(row["dimensions"]["model_slot"] == "two" for row in failure_rows.values())
        )
        self.assertEqual(failure_rows["failure_detail"]["dimensions"]["system_code"], "econnreset")
        self.assertEqual(failure_rows["failure_retry_count"]["value"], 3)
        self.assertEqual(rows[-1]["name"], "measurements_omitted")
        self.assertGreater(rows[-1]["value"], 0)

    def test_large_approved_request_remains_observable(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)], start_request={"request": "x" * (600 * 1024)})
        code, payload = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(payload["measurements"][0]["dimensions"]["outcome"], "succeeded")

    def test_sample_launch_failure_uses_verified_sibling_provenance(self) -> None:
        models = ["gpt-5.6-sol", "gpt-5.6-terra"]
        directory = self.create_run(model=models[1], models=models, sample=True)
        self.attempt([{"model": models[0], "launch_status": "failed"}, self.entry(directory)])
        code, payload = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(payload["measurements"][0]["dimensions"]["outcome"], "mixed")
        contextual = [
            row for row in payload["measurements"] if "source" in row.get("dimensions", {})
        ]
        self.assertTrue(contextual)
        self.assertTrue(all(row["dimensions"]["source"] == "sample" for row in contextual))
        failure = next(row for row in payload["measurements"] if row["name"] == "failure")
        self.assertEqual(failure["dimensions"]["controller_code"], "unknown")

    def test_unreadable_attempt_and_startup_failure_are_reported(self) -> None:
        path = self.attempt([])
        path.write_text("{bad")
        self.assertEqual(
            self.invoke()[1]["measurements"][0]["dimensions"]["outcome"], "unreadable_state"
        )
        self.attempt([], startup_failed=True)
        self.assertEqual(
            self.invoke()[1]["measurements"][0]["dimensions"]["outcome"], "startup_failure"
        )
        self.attempt([], start_requested=False, final_results_ready=False, controller_pid=1234)
        with mock.patch.object(common.os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(
                self.invoke()[1]["measurements"][0]["dimensions"]["outcome"], "no_run_observed"
            )

    def start_launch_observer(
        self, *, sidecar: bool = True, timeout: float = 5
    ) -> tuple[subprocess.Popen[str], dict[str, Any]]:
        environment = dict(os.environ)
        environment.pop(common.METRICS_OUTPUT_ENV, None)
        if sidecar:
            environment[common.METRICS_OUTPUT_ENV] = str(self.output)
        process = subprocess.Popen(
            [
                sys.executable,
                str(LAUNCH_SCRIPT),
                "--run-root",
                str(self.run_root),
                "--startup-timeout-seconds",
                str(timeout),
                "--poll-interval-seconds",
                "0.01",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        assert process.stdout is not None
        self.assertTrue(
            select.select([process.stdout], [], [], 5)[0], "No startup observer acknowledgment"
        )
        acknowledgment = json.loads(process.stdout.readline())
        self.assertEqual(acknowledgment["observer_started"], sidecar)
        # The observer creates neither controller nor lifecycle state before MCP runs.
        self.assertFalse((self.run_root.parent / "controllers").exists())
        return process, acknowledgment

    def call_mcp_launcher(
        self, session: str, *, cli: str | None = None, repeat: bool = False
    ) -> list[dict[str, Any]]:
        arguments = {"controller_session_id": session}
        if cli is not None:
            arguments["codex_cli_path"] = cli
        requests = [
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": "open_controller", "arguments": arguments},
            }
            for i in range(1, 3 if repeat else 2)
        ]
        environment = dict(os.environ)
        environment.pop(common.METRICS_OUTPUT_ENV, None)
        environment["CODEX_BAKEOFF_RUN_ROOT"] = str(self.run_root)
        environment["CODEX_BAKEOFF_CONTROLLER_IDLE_TIMEOUT_SECONDS"] = "10"
        result = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "mcp" / "server.py")],
            input="\n".join(json.dumps(request) for request in requests) + "\n",
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line)["result"] for line in result.stdout.splitlines()]

    def test_mcp_exit_keeps_controller_and_independent_startup_metrics_alive(self) -> None:
        import urllib.request  # noqa: TID251 - portable plugin loopback integration test.

        urlopen, Request = urllib.request.urlopen, urllib.request.Request  # noqa: TID251
        for sidecar in (True, False):
            with self.subTest(sidecar=sidecar):
                # Each subcase has an independent controller root.
                self.run_root = self.root / str(sidecar) / "runs"
                self.run_root.mkdir(parents=True)
                observer, acknowledgment = self.start_launch_observer(sidecar=sidecar)
                session = acknowledgment["controller_session_id"]
                replies = self.call_mcp_launcher(session, repeat=True)
                self.assertEqual(replies[0], replies[1])
                ready = replies[0]["structuredContent"]
                self.assertTrue(ready["prepared"])
                origin = ready["launch_url"].rstrip("/")
                directory = self.run_root.parent / "controllers" / session
                runtime = json.loads((directory / "controller-server.json").read_text())
                try:
                    with urlopen(origin + "/health", timeout=3) as response:
                        self.assertEqual(json.load(response)["pid"], runtime["pid"])
                    with urlopen(origin, timeout=3) as response:
                        self.assertIn(b"Codex Bakeoff", response.read())
                    _, stderr = observer.communicate(timeout=5)
                    self.assertEqual(observer.returncode, 0, stderr)
                    if sidecar:
                        self.assertEqual(
                            json.loads(self.output.read_text())["measurements"][0]["dimensions"][
                                "outcome"
                            ],
                            "ready",
                        )
                    else:
                        self.assertFalse((directory / "metrics-launch.json").exists())
                    self.assertEqual(len(list(directory.parent.glob("*/attempt.json"))), 1)
                finally:
                    request = Request(
                        origin + "/api/shutdown",
                        data=b"{}",
                        headers={
                            "Content-Type": "application/json",
                            "X-Codex-Replay-Control": runtime["control_token"],
                        },
                    )
                    with urlopen(request, timeout=3):
                        pass
                deadline = time.monotonic() + 5
                while True:
                    attempt = json.loads((directory / "attempt.json").read_text())
                    if attempt.get("controller_stopped") is True or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.assertIs(attempt.get("controller_stopped"), True)
                self.assertIsNotNone(common.timestamp(attempt["controller_stopped_at"]))

    def test_mcp_startup_failure_is_persisted_and_reported_without_relaunch(self) -> None:
        observer, acknowledgment = self.start_launch_observer()
        session = acknowledgment["controller_session_id"]
        replies = self.call_mcp_launcher(session, cli="/missing/codex", repeat=True)
        self.assertTrue(all(reply["isError"] for reply in replies))
        _, stderr = observer.communicate(timeout=5)
        self.assertEqual(observer.returncode, 0, stderr)
        attempt = json.loads((self.root / "controllers" / session / "attempt.json").read_text())
        self.assertTrue(attempt["startup_failed"])
        self.assertFalse(attempt["controller_ready"])
        self.assertEqual(
            json.loads(self.output.read_text())["measurements"][0]["dimensions"]["outcome"],
            "startup_failure",
        )
        self.assertFalse((self.root / "controllers" / session / "controller-server.json").exists())

    def test_missing_mcp_launch_reports_unknown_without_writing_lifecycle_state(self) -> None:
        observer, acknowledgment = self.start_launch_observer(timeout=0.1)
        _, stderr = observer.communicate(timeout=5)
        self.assertEqual(observer.returncode, 0, stderr)
        self.assertEqual(
            json.loads(self.output.read_text())["measurements"][0]["dimensions"]["outcome"],
            "launch_unobserved",
        )
        directory = self.root / "controllers" / acknowledgment["controller_session_id"]
        self.assertEqual([path.name for path in directory.iterdir()], ["metrics-launch.json"])

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_root = self.root / "runs"
        self.run_root.mkdir()
        self.output = self.root / "metrics.json"
        self.output.write_text("original", encoding="utf-8")

    def invoke_probe(
        self, output: Path | None, *, script: Path = PROBE_SCRIPT
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.pop(common.METRICS_OUTPUT_ENV, None)
        if output is not None:
            environment[common.METRICS_OUTPUT_ENV] = str(output)
        environment["PYTHONSAFEPATH"] = "1"
        environment["CODEX_BAKEOFF_RUN_ROOT"] = str(self.run_root)
        return subprocess.run(
            [sys.executable, str(script)],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_probe_writes_plugin_version_and_exits_without_starting_replay(self) -> None:
        from test_plugin_metrics_contract import _manifest

        inode = self.output.stat().st_ino
        result = self.invoke_probe(self.output)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"metrics_probe": "written"})
        payload = json.loads(self.output.read_bytes())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["measurements"][0], {"name": "metrics_probe", "value": 1})
        self.assertEqual(len(payload["measurements"]), 4)
        version_rows = payload["measurements"][1:]
        self.assertEqual(
            [row["dimensions"]["component"] for row in version_rows], ["major", "minor", "patch"]
        )
        for row in version_rows:
            self.assertEqual(row["name"], "plugin_version")
            self.assertIsInstance(row["value"], int)
        plugin_manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            ".".join(str(row["value"]) for row in version_rows), plugin_manifest["version"]
        )
        declarations = _manifest()["operations"]["metrics_probe"]["measurements"]
        for row in payload["measurements"]:
            declaration = declarations[row["name"]]
            for dimension, value in row.get("dimensions", {}).items():
                self.assertIn(value, declaration["dimensions"][dimension])
        self.assertEqual(self.output.stat().st_ino, inode)
        self.assertEqual(list(self.run_root.iterdir()), [])
        self.assertFalse((self.root / "controllers").exists())

    def test_probe_preserves_measurements_with_semver_suffixes(self) -> None:
        plugin_root = self.root / "plugin"
        scripts = plugin_root / "scripts" / "metrics"
        scripts.mkdir(parents=True)
        for source in (PROBE_SCRIPT, PROBE_SCRIPT.with_name("replay_metrics_common.py")):
            (scripts / source.name).write_bytes(source.read_bytes())
        manifest = plugin_root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir()

        for version in ("7.12.345-alpha.1", "7.12.345+build.2", "7.12.345-alpha.1+build.2"):
            with self.subTest(version=version):
                manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
                result = self.invoke_probe(self.output, script=scripts / PROBE_SCRIPT.name)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(self.output.read_bytes()),
                    {
                        "version": 1,
                        "measurements": [
                            {"name": "metrics_probe", "value": 1},
                            {
                                "name": "plugin_version",
                                "value": 7,
                                "dimensions": {"component": "major"},
                            },
                            {
                                "name": "plugin_version",
                                "value": 12,
                                "dimensions": {"component": "minor"},
                            },
                            {
                                "name": "plugin_version",
                                "value": 345,
                                "dimensions": {"component": "patch"},
                            },
                        ],
                    },
                )

    def test_probe_without_host_analytics_exits_without_writing(self) -> None:
        result = self.invoke_probe(None)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"metrics_probe": "analytics_unavailable"})
        self.assertEqual(self.output.read_text(), "original")
        self.assertEqual(list(self.run_root.iterdir()), [])
        self.assertFalse((self.root / "controllers").exists())

    def test_probe_rejects_missing_or_symlinked_host_sidecars(self) -> None:
        missing = self.root / "missing.json"
        symlink = self.root / "symlink.json"
        symlink.symlink_to(self.output)
        for path in (missing, symlink):
            with self.subTest(path=path.name):
                result = self.invoke_probe(path)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("written", result.stdout)
                self.assertEqual(self.output.read_text(), "original")
                self.assertFalse(missing.exists())
                self.assertFalse((self.root / "controllers").exists())

    def test_cancelled_retry_does_not_report_previous_attempt_failure(self) -> None:
        run = metrics.ReplayRun(
            model="gpt-5.6-sol",
            state={
                "status": "cancelled",
                "failure_diagnostic": {
                    "worker_code": "stream_error",
                    "retryable": True,
                    "retry_count": 1,
                    "elapsed_ms": 5000,
                    "exit_code": 1,
                },
            },
            run={},
            report={},
        )
        payload = json.loads(metrics._payload(metrics._replay_event([run])))
        rows = {row["name"]: row for row in payload["measurements"]}
        self.assertEqual(rows["failure"]["dimensions"]["worker_code"], "unknown")
        self.assertNotIn("failure_retry_count", rows)
        self.assertNotIn("failure_duration_seconds", rows)
        self.assertNotIn("failure_exit_code", rows)

    def create_run(
        self,
        *,
        model: str = "gpt-5.6-sol",
        models: Sequence[str] | None = None,
        status: str = "completed",
        sample: bool = False,
        session: str = CONTROLLER_SESSION_ID,
        fingerprint: str = "b" * 64,
        token_hash: str = "c" * 64,
        report: Mapping[str, Any] | None = None,
        state_overrides: Mapping[str, Any] | None = None,
    ) -> Path:
        directory = self.run_root / f"run-{len(list(self.run_root.iterdir()))}-{model}"
        directory.mkdir()
        state: dict[str, Any] = {
            "controller_session_id": session,
            "run_directory": str(directory),
            "prepare_token_hash": token_hash,
            "configuration_fingerprint": fingerprint,
            "model": model,
            "models": list(models or [model]),
            "status": status,
            "phase": "reporting" if status == "completed" else "implementing",
            "started_at": "2026-08-12T10:00:00+00:00",
        }
        state.update(state_overrides or {})
        replay: dict[str, Any] = {
            "imported_thread_id": f"claude-sample:{SAMPLE_ID}" if sample else "thread-private",
            "claude_model": "claude-sonnet-5",
            "historical_usage": {"input_tokens": 700},
            "historical_model_request_seconds": 12.4,
            "historical_wall_clock_seconds": 18.9,
        }
        baseline: dict[str, Any] = {}
        if sample:
            sample_directory = (
                self.root / "controllers" / session / "claude-code-samples" / SAMPLE_ID
            )
            sample_directory.mkdir(parents=True, exist_ok=True)
            source_path = sample_directory / f"{SAMPLE_SESSION_ID}.jsonl"
            source_path.write_text("{}\n", encoding="utf-8")
            repository = sample_directory / "repository"
            repository.mkdir(exist_ok=True)
            baseline = {"commit": SAMPLE_COMMIT, "repository": str(repository)}
            replay.update(
                {
                    "session_id": SAMPLE_SESSION_ID,
                    "source_path": str(source_path),
                    "project_dir": str(repository),
                }
            )
            replay["recorded_claude_result"] = {
                "duration_ms": 19_200,
                "duration_api_ms": 12_400,
                "usage": {"input_tokens": 700},
                "modelUsage": {"claude-sonnet-5": {"inputTokens": 700}},
            }
            (sample_directory / "materialization.json").write_text(
                json.dumps(
                    {
                        "sample_id": SAMPLE_ID,
                        "thread_id": f"claude-sample:{SAMPLE_ID}",
                        "baseline_commit": SAMPLE_COMMIT,
                        "repository_path": str(repository),
                        "transcript_path": str(source_path),
                    }
                ),
                encoding="utf-8",
            )
        (directory / common.STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
        (directory / "run.json").write_text(
            json.dumps({"model": model, "replay": replay, "baseline": baseline}),
            encoding="utf-8",
        )
        if report is not None or status == "completed":
            (directory / "report.json").write_text(
                json.dumps(report if report is not None else _report(sample=sample)),
                encoding="utf-8",
            )
        return directory

    def invoke(self, *arguments: str, stage: str = "final_results") -> tuple[int, dict[str, Any]]:
        with (
            mock.patch.dict(os.environ, {common.METRICS_OUTPUT_ENV: str(self.output)}),
            mock.patch("sys.stdout"),
        ):
            result = REPORTERS[stage].main(
                [
                    "--controller-session-id",
                    CONTROLLER_SESSION_ID,
                    "--run-root",
                    str(self.run_root),
                    "--run-timeout-seconds",
                    "0",
                    *arguments,
                ],
            )
        raw = self.output.read_text(encoding="utf-8")
        return result, json.loads(raw) if raw != "original" else {}

    def replay(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        from test_plugin_metrics_contract import _manifest

        self.assertEqual(set(envelope), {"version", "measurements"})
        self.assertEqual(envelope["version"], 1)
        rows = envelope["measurements"]
        self.assertLessEqual(len(rows), metrics.MAX_OUTPUT_ROWS)
        declarations = _manifest()["operations"]["replay_metrics"]["measurements"]
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        models: dict[str, dict[str, Any]] = {}
        claude: dict[str, Any] = {}
        source = ""

        for row in rows:
            self.assertTrue(set(row) <= {"name", "value", "dimensions"})
            name, value = row["name"], row["value"]
            self.assertIn(name, declarations)
            self.assertIsInstance(value, (int, float))
            self.assertNotIsInstance(value, bool)
            self.assertTrue(math.isfinite(value))
            dimensions = row.get("dimensions", {})
            expected = declarations[name].get("dimensions", {})
            self.assertEqual(set(dimensions), set(expected))
            for dimension, selected in dimensions.items():
                self.assertIn(selected, expected[dimension])
            identity = (name, tuple(sorted(dimensions.items())))
            self.assertNotIn(identity, seen)
            seen.add(identity)

            source = dimensions.get("source", source)
            if "claude_model" in dimensions:
                claude.setdefault("model", dimensions["claude_model"])
            slot = dimensions.get("model_slot")
            if name == "replay" and isinstance(slot, str):
                models[slot] = {"model": dimensions["codex_model"], "status": dimensions["status"]}
                continue
            if name in {"attempt", "attempt_model", "measurements_omitted"}:
                continue
            if name.startswith("claude_") and slot is None:
                field = name[len("claude_") :]
                claude[field] = value
                if name == "claude_duration_seconds":
                    claude["timing_basis"] = dimensions["timing_basis"]
                continue
            if not isinstance(slot, str):
                continue
            model = models[slot]
            if name == "failure":
                model["failure"] = {
                    key: dimensions[key] for key in ("phase", "worker_code", "controller_code")
                }
            elif name == "outcome":
                model["winner"] = dimensions["winner"]
            elif name in {"codex_score", "claude_score"}:
                model[name] = value
            elif name in {"codex_dimension_score", "claude_dimension_score"}:
                provider = name.split("_", 1)[0]
                scores = model.setdefault("dimension_scores", {})
                scores.setdefault(dimensions["score_dimension"], {})[provider] = value
            elif name.startswith("codex_"):
                model[name[len("codex_") :]] = value

        ordered = [models[slot] for slot in metrics.MODEL_SLOTS if slot in models]
        statuses = {model["status"] for model in ordered}
        return {
            "source": source,
            "status": next(iter(statuses)) if len(statuses) == 1 else "partial",
            "claude": claude,
            "codex_models": ordered,
        }

    def test_uninstrumented_execution_exits_without_parsing_or_writing(self) -> None:
        for stage in OBSERVER_SCRIPTS:
            stdout = io.StringIO()
            with (
                self.subTest(stage=stage),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    common, "arguments", side_effect=AssertionError("must not parse")
                ),
                mock.patch("sys.stdout", stdout),
            ):
                self.assertEqual(REPORTERS[stage].main(["--invalid-argument"]), 0)
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    {"observer_started": False, "reason": "analytics_unavailable"},
                )
                self.assertEqual(self.output.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_imported_replay_exports_observed_metadata_and_quality_by_default(self) -> None:
        report = _report()
        report["original_request"] = "x" * (19 * 1024 * 1024)
        directory = self.create_run(report=report)
        with self.assertRaisesRegex(ValueError, "exceeds its size limit"):
            common.observed_json(directory / "report.json")
        self.attempt([self.entry(directory)])

        status, payload = self.invoke()

        self.assertEqual(status, 0)
        replay = self.replay(payload)
        self.assertEqual(replay["source"], "imported")
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(
            replay["claude"],
            {
                "model": "sonnet",
                "input_tokens": 760,
                "output_tokens": 210,
                "cached_input_tokens": 50,
                "cost_usd": 0.29,
                "duration_seconds": 12.4,
                "timing_basis": "model_request",
                "wall_clock_seconds": 18.9,
            },
        )
        model = replay["codex_models"][0]
        expected_metadata = {
            "model": "sol",
            "status": "completed",
            "input_tokens": 1200,
            "output_tokens": 340,
            "cached_input_tokens": 80,
            "cost_usd": 0.18,
            "duration_seconds": 24.5,
        }
        self.assertEqual({key: model[key] for key in expected_metadata}, expected_metadata)
        self.assertEqual(model["winner"], "codex")
        self.assertEqual(model["codex_score"], 0.918)
        self.assertEqual(model["claude_score"], 0.795)
        self.assertEqual(
            model["dimension_scores"],
            {
                name: {"codex": codex, "claude": claude}
                for name, (claude, codex) in DIMENSION_SCORES.items()
            },
        )
        self.assertIn("model_slot", self.output.read_text(encoding="utf-8"))

    def test_sample_exports_approved_quality_and_recorded_wall_clock(self) -> None:
        directory = self.create_run(sample=True)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        model = replay["codex_models"][0]
        self.assertEqual(replay["source"], "sample")
        self.assertEqual(model["winner"], "codex")
        self.assertEqual(model["codex_score"], 0.918)
        self.assertEqual(model["claude_score"], 0.795)
        self.assertEqual(
            model["dimension_scores"],
            {
                name: {"codex": codex, "claude": claude}
                for name, (claude, codex) in DIMENSION_SCORES.items()
            },
        )
        self.assertEqual(replay["claude"]["duration_seconds"], 19.2)
        self.assertEqual(replay["claude"]["timing_basis"], "recorded_wall_clock")

    def test_imported_quality_never_exports_sensitive_or_free_text_content(self) -> None:
        secret = "PRIVATE_CUSTOMER_PROMPT_RESPONSE_PATCH_IDENTIFIER"
        report = _report()
        report.update({"prompt": secret, "response": secret, "patch": secret})
        report["codex_changed_files"] = [f"/private/{secret}/repository.py"]
        report["limitations"] = [secret]
        report["evaluation"]["reviews"][0]["ballot"]["explanation"] = secret
        report["evaluation"]["reviews"][0]["ballot"]["dimensions"][secret] = {
            "winner": secret,
            "candidates": {"A": {"score": 1, "checks": {secret: 1}}},
        }
        report["evaluation"]["reviews"][0]["ballot"]["dimensions"]["request_fulfillment"][
            "candidates"
        ]["A"]["checks"][secret] = 1
        directory = self.create_run(report=report)
        self.attempt([self.entry(directory)])
        run_path = directory / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["replay"].update({"imported_thread_id": secret, "project_dir": f"/private/{secret}"})
        run_path.write_text(json.dumps(run), encoding="utf-8")

        _, payload = self.invoke()

        self.assertEqual(self.replay(payload)["codex_models"][0]["winner"], "codex")
        self.assertNotIn(secret, self.output.read_text(encoding="utf-8"))

    def test_exports_selected_operational_metrics_without_extended_details(self) -> None:
        report = _report()
        report["usage"]["codex"][0].update(
            {
                "cache_write_1h_tokens": 7,
                "cache_write_5m_tokens": 3,
                "reasoning_output_tokens": 41,
            }
        )
        report["usage"]["claude"][0].update(
            {"cache_write_1h_tokens": 8, "cache_write_5m_tokens": 2}
        )
        report["codex_changed_files"] = ["first.py", "second.py"]
        report["capabilities"] = {"items": [{}, {}], "unavailable_capabilities": [{}]}
        report["limitations"] = ["not exported"]
        report["evaluation"]["comparable_dimensions"] = ["request_fulfillment"]
        report["evaluation"]["evaluator_availability"] = [
            {"available": True},
            {"available": False},
        ]
        report["evaluation"]["reviews"][0].update(
            {"model": "gpt-5.6-sol", "normalization": {"required": True}}
        )
        directory = self.create_run(
            report=report,
            state_overrides={
                "completed_at": "2026-08-12T10:00:09+00:00",
                "implementation_attempt": 2,
                "events": [
                    {"at": "2026-08-12T10:00:00+00:00", "phase": "preparing"},
                    {"at": "2026-08-12T10:00:02+00:00", "phase": "implementing"},
                    {"at": "2026-08-12T10:00:07+00:00", "phase": "reporting"},
                    {"at": "2026-08-12T10:00:09+00:00", "phase": "reporting"},
                ],
                "worker_events": [
                    {
                        "type": "completed",
                        "itemCounts": {
                            "command_execution": 4,
                            "file_change": 2,
                            "agent_message": 3,
                            "error": 1,
                        },
                    }
                ],
            },
        )
        self.attempt([self.entry(directory)])
        run_path = directory / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["replay"].update(
            {
                "historical_changed_files": ["historical.py"],
                "historical_model_request_timing": {
                    "request_count": 5,
                    "missing_request_count": 1,
                    "unidentified_assistant_event_count": 2,
                },
            }
        )
        run_path.write_text(json.dumps(run), encoding="utf-8")

        _, payload = self.invoke()

        self.replay(payload)
        rows = payload["measurements"]
        values = {
            row["name"]: row["value"]
            for row in rows
            if row["name"] != "codex_phase_duration_seconds"
        }
        self.assertEqual(values["codex_wall_clock_seconds"], 9)
        self.assertEqual(values["claude_wall_clock_seconds"], 18.9)
        self.assertFalse(
            {
                "codex_ordinary_input_tokens",
                "codex_cache_write_1h_tokens",
                "codex_reasoning_output_tokens",
                "codex_changed_file_count",
                "codex_implementation_attempt_count",
                "codex_tool_call_count",
                "claude_ordinary_input_tokens",
                "claude_cache_write_tokens",
                "claude_changed_file_count",
                "claude_request_count",
            }
            & values.keys()
        )
        phases = {
            row["dimensions"]["phase"]: row["value"]
            for row in rows
            if row["name"] == "codex_phase_duration_seconds"
        }
        self.assertEqual(phases, {"implementing": 5})
        evaluator = next(row for row in rows if row["name"] == "evaluation")
        self.assertEqual(evaluator["dimensions"]["evaluator_model"], "sol")
        self.assertEqual(evaluator["dimensions"]["evaluator_provider"], "codex")
        self.assertEqual(evaluator["dimensions"]["self_judged"], "yes")
        self.assertEqual(evaluator["dimensions"]["normalized"], "yes")
        self.assertFalse(any(row["name"].endswith("check_score") for row in rows))

    def test_imported_quality_exports_dimension_winners_without_individual_checks(self) -> None:
        report = _report()
        candidates = report["evaluation"]["reviews"][0]["ballot"]["dimensions"][
            "request_fulfillment"
        ]["candidates"]
        candidates["A"]["checks"]["required_behavior"] = 0
        candidates["A"]["checks"]["usable_result"] = None
        candidates["B"]["checks"]["required_behavior"] = 1
        directory = self.create_run(report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        self.replay(payload)
        rows = payload["measurements"]
        winner = next(
            row
            for row in rows
            if row["name"] == "dimension_outcome"
            and row["dimensions"]["score_dimension"] == "request_fulfillment"
        )
        self.assertEqual(winner["dimensions"]["winner"], "codex")
        self.assertFalse(any(row["name"].endswith("check_score") for row in rows))

    def test_mixed_sample_provenance_exports_quality_for_every_model_as_imported(self) -> None:
        selected = ["gpt-5.6-sol", "gpt-5.6-terra"]
        sample = self.create_run(model=selected[0], models=selected, sample=True)
        imported = self.create_run(model=selected[1], models=selected)
        self.attempt([self.entry(sample), self.entry(imported)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(replay["source"], "imported")
        for model in replay["codex_models"]:
            self.assertEqual(model["winner"], "codex")
            self.assertIn("dimension_scores", model)

    def test_sample_prefix_without_recorded_result_remains_imported(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)])
        run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        run["replay"]["imported_thread_id"] = "claude-sample:untrusted"
        (directory / "run.json").write_text(json.dumps(run), encoding="utf-8")

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(replay["source"], "imported")
        self.assertEqual(replay["codex_models"][0]["winner"], "codex")

    def test_fabricated_sample_identity_with_recorded_metrics_remains_imported(self) -> None:
        directory = self.create_run(sample=True)
        self.attempt([self.entry(directory)])
        run_path = directory / "run.json"
        original = json.loads(run_path.read_text(encoding="utf-8"))
        changes = {
            "unknown_id": lambda replay: replay.update(
                {"imported_thread_id": "claude-sample:invented-customer-task"}
            ),
            "wrong_session": lambda replay: replay.update({"session_id": "forged-session"}),
            "wrong_model": lambda replay: replay.update({"claude_model": "claude-opus-5"}),
            "missing_model_provenance": lambda replay: replay["recorded_claude_result"].update(
                {"modelUsage": {"claude-opus-5": {"inputTokens": 12}}}
            ),
            "wrong_recorded_session": lambda replay: replay["recorded_claude_result"].update(
                {"session_id": "forged-session"}
            ),
        }
        for label, modify in changes.items():
            with self.subTest(spoof=label):
                forged = json.loads(json.dumps(original))
                modify(forged["replay"])
                run_path.write_text(json.dumps(forged), encoding="utf-8")
                self.output.write_text("original", encoding="utf-8")

                _, payload = self.invoke()

                replay = self.replay(payload)
                self.assertEqual(replay["source"], "imported")
                self.assertEqual(replay["codex_models"][0]["winner"], "codex")

    def test_sample_requires_matching_controller_materialization_and_baseline(self) -> None:
        directory = self.create_run(sample=True)
        self.attempt([self.entry(directory)])
        run_path = directory / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        proof_path = Path(run["replay"]["source_path"]).parent / "materialization.json"
        original_proof = json.loads(proof_path.read_text(encoding="utf-8"))
        for field, forged_value in (
            ("sample_id", "another-sample"),
            ("thread_id", "claude-sample:another-sample"),
            ("baseline_commit", "0" * 40),
            ("repository_path", "/private/customer/repository"),
            ("transcript_path", "/private/customer/transcript.jsonl"),
        ):
            with self.subTest(proof=field):
                proof_path.write_text(
                    json.dumps({**original_proof, field: forged_value}), encoding="utf-8"
                )
                self.output.write_text("original", encoding="utf-8")

                _, payload = self.invoke()

                replay = self.replay(payload)
                self.assertEqual(replay["source"], "imported")
                self.assertEqual(replay["codex_models"][0]["winner"], "codex")

    def test_run_without_evaluation_still_exports_operational_metrics(self) -> None:
        report = _report()
        report.pop("evaluation")
        directory = self.create_run(report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(replay["source"], "imported")
        model = replay["codex_models"][0]
        self.assertEqual(model["input_tokens"], 1200)
        self.assertNotIn("winner", model)
        self.assertNotIn("dimension_scores", model)

    def test_model_array_preserves_selected_order_duplicates_and_group_isolation(self) -> None:
        selected = ["gpt-5.6-sol", "gpt-5.6-sol-preview", "gpt-5.6-terra"]
        third = self.create_run(model=selected[2], models=selected)
        first = self.create_run(model=selected[0], models=selected)
        second = self.create_run(model=selected[1], models=selected)
        self.create_run(model="gpt-5.6-luna", fingerprint="d" * 64)
        self.create_run(model="gpt-5.6-luna", session="e" * 32)
        self.attempt([self.entry(path) for path in (first, second, third)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(
            [model["model"] for model in replay["codex_models"]], ["sol", "sol", "terra"]
        )
        self.assertEqual(replay["claude"]["model"], "sonnet")

    def test_partial_model_start_failure_preserves_selected_model_order(self) -> None:
        selected = ["gpt-5.6-luna", "gpt-5.6-terra"]
        directory = self.create_run(model=selected[1], models=selected)
        self.attempt(
            [
                {
                    "model": selected[0],
                    "launch_status": "failed",
                    "error": "The model is unavailable.",
                    "controller_code": "launch_failed",
                },
                self.entry(directory),
            ]
        )

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(replay["status"], "partial")
        failed, completed = replay["codex_models"]
        self.assertEqual(
            failed,
            {
                "model": "luna",
                "status": "failed",
                "failure": {
                    "phase": "preparation",
                    "worker_code": "unknown",
                    "controller_code": "launch_failed",
                },
            },
        )
        self.assertEqual(completed["model"], "terra")
        self.assertEqual(completed["status"], "completed")

    def test_failed_and_cancelled_runs_are_exported_without_free_form_error_text(self) -> None:
        selected = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        first = self.create_run(
            model=selected[0],
            models=selected,
            status="failed",
            state_overrides={"phase": "reviewing", "error": "secret/private reviewer failed"},
        )
        second = self.create_run(model=selected[1], models=selected, status="cancelled")
        third = self.create_run(
            model=selected[2],
            models=selected,
            status="failed",
            state_overrides={"error": "worker timed out after 60 seconds"},
        )
        self.attempt([self.entry(path) for path in (first, second, third)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(replay["status"], "partial")
        first, second, third = replay["codex_models"]
        self.assertEqual(first["failure"]["phase"], "evaluation")
        self.assertEqual(second["status"], "cancelled")
        for model in (first, second, third):
            self.assertEqual(model["failure"]["worker_code"], "unknown")
            self.assertEqual(model["failure"]["controller_code"], "unknown")
        self.assertTrue(all("reason" not in row["dimensions"] for row in payload["measurements"]))
        self.assertNotIn("secret/private", self.output.read_text(encoding="utf-8"))

    def test_empty_historical_usage_does_not_fabricate_claude_zeroes(self) -> None:
        report = _report()
        report["usage"]["claude"] = []
        report["normalized_usage"]["claude"] = {
            "total_input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
        }
        directory = self.create_run(report=report)
        self.attempt([self.entry(directory)])
        run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        run["replay"].pop("historical_usage")
        (directory / "run.json").write_text(json.dumps(run), encoding="utf-8")

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertNotIn("input_tokens", replay["claude"])
        self.assertNotIn("output_tokens", replay["claude"])
        self.assertNotIn("cached_input_tokens", replay["claude"])
        self.assertIn("input_tokens", replay["codex_models"][0])

    def test_nonfinite_negative_boolean_and_partial_values_are_omitted(self) -> None:
        report = _report()
        report["normalized_usage"]["codex"]["total_input_tokens"] = -1
        report["normalized_usage"]["codex"]["output_tokens"] = True
        report["normalized_usage"]["codex"]["cached_input_tokens"] = math.inf
        report["estimated_cost"]["codex"] = {"status": "partial", "usd": 3}
        report["estimated_cost"]["claude"] = {"status": "estimated", "usd": math.nan}
        report["codex_execution"]["elapsed_seconds"] = -2
        directory = self.create_run(report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        codex = replay["codex_models"][0]
        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cost_usd",
            "duration_seconds",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, codex)
        self.assertNotIn("cost_usd", replay["claude"])

    def test_fractional_tokens_are_omitted_and_integral_floats_are_normalized(self) -> None:
        report = _report()
        report["normalized_usage"]["codex"]["total_input_tokens"] = 1200.5
        report["normalized_usage"]["codex"]["output_tokens"] = 340.0
        report["normalized_usage"]["claude"]["cached_input_tokens"] = 50.25
        directory = self.create_run(report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        model = replay["codex_models"][0]
        self.assertNotIn("input_tokens", model)
        self.assertEqual(model["output_tokens"], 340)
        self.assertIsInstance(model["output_tokens"], int)
        self.assertNotIn("cached_input_tokens", replay["claude"])

    def test_quality_requires_successful_review_and_uses_display_rounded_tie(self) -> None:
        report = _report(sample=True)
        report["evaluation"]["totals"] = {"A": 0.821, "B": 0.824}
        directory = self.create_run(sample=True, report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        self.assertEqual(self.replay(payload)["codex_models"][0]["winner"], "tie")

        report["evaluation"]["reviews"][0]["status"] = "failed"
        report_path = next(self.run_root.iterdir()) / "report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self.output.write_text("original", encoding="utf-8")

        _, payload = self.invoke()

        self.assertNotIn("winner", self.replay(payload)["codex_models"][0])

    def test_failed_sample_with_existing_report_never_exports_quality(self) -> None:
        directory = self.create_run(sample=True, status="failed", report=_report(sample=True))
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(replay["status"], "failed")
        model = replay["codex_models"][0]
        self.assertIn("failure", model)
        self.assertNotIn("winner", model)
        self.assertNotIn("dimension_scores", model)
        self.assertFalse(any(name.endswith("_score") for name in model))

    def test_noncomparable_and_not_applicable_dimension_scores_are_omitted(self) -> None:
        report = _report(sample=True)
        report["evaluation"]["comparable_dimensions"] = ["request_fulfillment", "safe_operations"]
        report["evaluation"]["reviews"][0]["ballot"]["dimensions"]["safe_operations"]["winner"] = (
            "not_applicable"
        )
        directory = self.create_run(sample=True, report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        dimensions = self.replay(payload)["codex_models"][0]["dimension_scores"]
        self.assertEqual(set(dimensions), {"request_fulfillment"})
        self.assertEqual(dimensions["request_fulfillment"], {"codex": 1, "claude": 0.75})

    def test_dimension_with_only_one_provider_score_is_omitted(self) -> None:
        report = _report(sample=True)
        request_fulfillment = report["evaluation"]["reviews"][0]["ballot"]["dimensions"][
            "request_fulfillment"
        ]
        request_fulfillment["candidates"]["A"].pop("score")
        directory = self.create_run(sample=True, report=report)
        self.attempt([self.entry(directory)])

        _, payload = self.invoke()

        dimensions = self.replay(payload)["codex_models"][0]["dimension_scores"]
        self.assertNotIn("request_fulfillment", dimensions)
        self.assertEqual(set(dimensions), set(DIMENSION_SCORES) - {"request_fulfillment"})

    def test_eight_sample_models_preserve_every_replay_within_host_row_limit(self) -> None:
        selected = [f"gpt-5.6-sol-variant-{index}" for index in range(8)]
        directories = [
            self.create_run(model=model, models=selected, sample=True) for model in selected
        ]
        self.attempt([self.entry(directory) for directory in directories])

        _, payload = self.invoke()

        replay = self.replay(payload)
        self.assertEqual(len(replay["codex_models"]), metrics.MAX_MODELS)
        self.assertLessEqual(len(self.output.read_bytes()), metrics.MAX_OUTPUT_BYTES)
        self.assertEqual(len(payload["measurements"]), metrics.MAX_OUTPUT_ROWS)
        for index, model in enumerate(replay["codex_models"]):
            with self.subTest(model=index):
                self.assertEqual(model["model"], "sol")
                self.assertIn("codex_score", model)
                self.assertIn("claude_score", model)
        omitted = payload["measurements"][-1]
        self.assertEqual(omitted["name"], "measurements_omitted")
        self.assertEqual(payload["measurements"][0]["dimensions"]["reporting_complete"], "no")
        complete = json.loads(
            (self.root / "controllers" / CONTROLLER_SESSION_ID / "metrics-full.json").read_bytes()
        )
        complete_rows = complete["measurements"]
        self.assertGreater(len(complete_rows), metrics.MAX_OUTPUT_ROWS)
        self.assertEqual(omitted["value"], len(complete_rows) - len(payload["measurements"]) + 1)
        self.assertFalse(any(row["name"] == "measurements_omitted" for row in complete_rows))
        self.assertEqual(complete_rows[0]["dimensions"]["reporting_complete"], "yes")
        for slot in metrics.MODEL_SLOTS:
            dimensions = {
                row["dimensions"]["score_dimension"]
                for row in complete_rows
                if row["name"] == "codex_dimension_score"
                and row["dimensions"]["model_slot"] == slot
            }
            self.assertEqual(dimensions, set(DIMENSION_SCORES), slot)

    def test_three_models_export_complete_quality_without_exceeding_host_row_limit(self) -> None:
        selected = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        directories = []
        for model in selected:
            report = _report(sample=True)
            report["evaluation"]["reviews"][0]["model"] = model
            directory = self.create_run(
                model=model,
                models=selected,
                sample=True,
                report=report,
                state_overrides={
                    "completed_at": "2026-08-12T10:00:12+00:00",
                    "events": [
                        {"at": "2026-08-12T10:00:00+00:00", "phase": "preparing"},
                        {"at": "2026-08-12T10:00:02+00:00", "phase": "implementing"},
                        {"at": "2026-08-12T10:00:07+00:00", "phase": "reviewing"},
                        {"at": "2026-08-12T10:00:10+00:00", "phase": "reporting"},
                        {"at": "2026-08-12T10:00:12+00:00", "phase": "reporting"},
                    ],
                },
            )
            directories.append(directory)
        self.attempt([self.entry(directory) for directory in directories])

        _, payload = self.invoke()

        replay = self.replay(payload)
        rows = payload["measurements"]
        self.assertEqual(rows[0]["dimensions"]["reporting_complete"], "yes")
        complete_path = self.root / "controllers" / CONTROLLER_SESSION_ID / "metrics-full.json"
        self.assertEqual(complete_path.read_bytes(), self.output.read_bytes())
        self.assertLessEqual(len(rows), metrics.MAX_OUTPUT_ROWS)
        self.assertFalse(any(row["name"] == "measurements_omitted" for row in rows))
        self.assertFalse(any(row["name"].endswith("check_score") for row in rows))
        for slot, model in zip(metrics.MODEL_SLOTS, replay["codex_models"], strict=False):
            with self.subTest(model=slot):
                self.assertEqual(set(model["dimension_scores"]), set(DIMENSION_SCORES))
                dimensions = [
                    row
                    for row in rows
                    if row["name"] == "dimension_outcome"
                    and row["dimensions"]["model_slot"] == slot
                ]
                self.assertEqual(len(dimensions), len(DIMENSION_SCORES))
                phases = {
                    row["dimensions"]["phase"]
                    for row in rows
                    if row["name"] == "codex_phase_duration_seconds"
                    and row["dimensions"]["model_slot"] == slot
                }
                self.assertEqual(phases, {"implementing", "reviewing"})
                self.assertTrue(
                    any(
                        row["name"] == "evaluation" and row["dimensions"]["model_slot"] == slot
                        for row in rows
                    )
                )
                self.assertIn("wall_clock_seconds", model)

    def test_sidecar_is_written_in_place_without_changing_inode(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)])
        original_inode = self.output.stat().st_ino

        _, payload = self.invoke()

        self.assertTrue(payload["measurements"])
        self.assertEqual(self.output.stat().st_ino, original_inode)

    def test_metrics_are_preserved_in_the_controller_directory(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)])

        result, payload = self.invoke()

        saved_metrics = self.root / "controllers" / CONTROLLER_SESSION_ID / "metrics.json"
        self.assertEqual(result, 0)
        self.assertEqual(saved_metrics.read_bytes(), self.output.read_bytes())
        self.assertEqual(json.loads(saved_metrics.read_text(encoding="utf-8")), payload)

    def test_local_metrics_copy_failure_does_not_break_host_collection(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)])

        with (
            mock.patch.object(
                Path, "write_bytes", side_effect=PermissionError(13, "Permission denied")
            ),
            mock.patch("sys.stderr") as stderr,
        ):
            result, payload = self.invoke()

        self.assertEqual(result, 0)
        self.assertTrue(payload["measurements"])
        stderr.write.assert_any_call(
            "Unable to preserve local Codex Bakeoff metrics: Permission denied."
        )

    def test_oversized_event_is_rejected_without_truncating_sidecar(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)])

        with (
            mock.patch.object(metrics, "MAX_OUTPUT_BYTES", 16),
            mock.patch("sys.stderr"),
        ):
            result, payload = self.invoke()

        self.assertEqual(result, 1)
        self.assertEqual(payload, {})
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_sidecar_descriptor_is_pinned_before_controller_wait(self) -> None:
        directory = self.create_run()
        self.attempt([self.entry(directory)])
        renamed = self.root / "pinned-original.json"
        replacement = self.output
        original_wait = metrics._observe_attempt

        def replace_sidecar(*args: Any, **kwargs: Any) -> metrics.AttemptObservation:
            replacement.rename(renamed)
            replacement.write_text("private replacement", encoding="utf-8")
            return original_wait(*args, **kwargs)

        with (
            mock.patch.object(metrics, "_observe_attempt", side_effect=replace_sidecar),
            mock.patch.dict(os.environ, {common.METRICS_OUTPUT_ENV: str(self.output)}),
        ):
            result = metrics.main(
                [
                    "--controller-session-id",
                    CONTROLLER_SESSION_ID,
                    "--run-root",
                    str(self.run_root),
                    "--run-timeout-seconds",
                    "0",
                ]
            )

        self.assertEqual(result, 0)
        self.assertTrue(self.replay(json.loads(renamed.read_text(encoding="utf-8"))))
        self.assertEqual(replacement.read_text(encoding="utf-8"), "private replacement")

    def test_missing_or_symlinked_sidecar_is_rejected_without_writing(self) -> None:
        self.create_run()
        missing = self.root / "missing.json"
        existing = self.root / "untouched.json"
        existing.write_text("private", encoding="utf-8")
        symlink = self.root / "sidecar-link.json"
        symlink.symlink_to(existing)

        for path in (missing, symlink):
            with (
                self.subTest(path=path.name),
                mock.patch.dict(os.environ, {common.METRICS_OUTPUT_ENV: str(path)}),
                mock.patch("sys.stderr"),
            ):
                self.assertEqual(
                    metrics.main(
                        [
                            "--controller-session-id",
                            CONTROLLER_SESSION_ID,
                            "--run-root",
                            str(self.run_root),
                            "--run-timeout-seconds",
                            "0",
                        ]
                    ),
                    1,
                )
        self.assertFalse(missing.exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "private")

    def test_missing_attempt_does_not_reconstruct_legacy_runs(self) -> None:
        self.create_run()
        self.create_run(session="b" * 32)

        result, payload = self.invoke("--pre-start-timeout-seconds", "0")

        self.assertEqual(result, 0)
        self.assertEqual(
            payload["measurements"],
            [{"name": "final_prestart_timeout", "value": 1}],
        )

    def test_idle_observer_stops_when_controller_process_dies(self) -> None:
        self.attempt([], start_requested=False, final_results_ready=False, controller_pid=1234)
        with mock.patch.object(common.os, "kill", side_effect=[None, ProcessLookupError]):
            result, payload = self.invoke("--poll-interval-seconds", "0.01")

        self.assertEqual(result, 0)
        self.assertEqual(payload["measurements"][0]["dimensions"]["outcome"], "no_run_observed")

    def test_shutdown_receipt_finishes_observers_even_while_pid_is_alive(self) -> None:
        self.attempt(
            [],
            start_requested=False,
            final_results_ready=False,
            controller_pid=os.getpid(),
            controller_stopped=True,
        )
        with (
            mock.patch.object(common.os, "kill") as probe,
            mock.patch.object(
                common, "pause", side_effect=AssertionError("Shutdown observers must not wait")
            ),
        ):
            for stage in OBSERVER_SCRIPTS:
                with self.subTest(stage=stage):
                    code, payload = self.invoke(stage=stage)
                    self.assertEqual(code, 0)
                    if stage == "run_start":
                        self.assertEqual(payload["measurements"], [])
                    else:
                        self.assertEqual(
                            payload["measurements"][0]["dimensions"]["outcome"], "no_run_observed"
                        )
            probe.assert_not_called()

    def test_delayed_observers_preserve_finished_results_and_handle_shutdown(self) -> None:
        for status, stopped, final_results_ready, outcome in (
            ("completed", False, True, "succeeded"),
            ("completed", True, True, "succeeded"),
            ("completed", True, False, "succeeded"),
            ("running", True, False, "interrupted"),
        ):
            with self.subTest(
                status=status,
                stopped=stopped,
                final_results_ready=final_results_ready,
            ):
                directory = self.create_run(status=status)
                self.attempt(
                    [self.entry(directory)],
                    final_results_ready=final_results_ready,
                    controller_pid=os.getpid(),
                    controller_stopped=stopped,
                )
                code, start = self.invoke(stage="run_start")
                self.assertEqual(code, 0)
                self.assertEqual(start["measurements"][0]["name"], "run_start")
                code, final = self.invoke("--run-timeout-seconds", "60")
                self.assertEqual(code, 0)
                self.assertEqual(final["measurements"][0]["dimensions"]["outcome"], outcome)
                if status == "completed":
                    self.assertEqual(self.replay(final)["codex_models"][0]["status"], "completed")

    def test_running_attempt_survives_missing_runtime_metadata(self) -> None:
        directory = self.create_run(status="running", report=_report())
        attempt_path = self.attempt(
            [self.entry(directory)], final_results_ready=False, controller_pid=os.getpid()
        )
        runtime = self.root / "controllers" / CONTROLLER_SESSION_ID / "controller-server.json"
        runtime.write_text(
            json.dumps({"controller_session_id": CONTROLLER_SESSION_ID, "pid": os.getpid()}),
            encoding="utf-8",
        )

        def finish_run() -> None:
            state_path = directory / common.STATE_NAME
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "completed"
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(state_path)
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["final_results_ready"] = True
            temporary = attempt_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(attempt), encoding="utf-8")
            temporary.replace(attempt_path)

        shutdown = threading.Timer(0.03, runtime.unlink)
        completed = threading.Timer(0.08, finish_run)
        shutdown.start()
        completed.start()
        self.addCleanup(shutdown.cancel)
        self.addCleanup(completed.cancel)

        result, payload = self.invoke(
            "--run-timeout-seconds", "1", "--poll-interval-seconds", "0.01"
        )

        self.assertEqual(result, 0)
        self.assertEqual(payload["measurements"][0]["dimensions"]["outcome"], "succeeded")
        self.assertEqual(self.replay(payload)["codex_models"][0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
