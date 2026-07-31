"""External-browser launcher and local-controller tests."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PLUGIN_ROOT / "mcp" / "server.py"
CONTROLLER_PATH = PLUGIN_ROOT / "mcp" / "controller.html"


def load_server():
    spec = importlib.util.spec_from_file_location("codex_bakeoff_mcp_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the MCP server.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class McpServerTests(unittest.TestCase):
    def test_python_39_is_supported_and_38_is_reported(self) -> None:
        server = load_server()
        self.assertIsNone(server._python_runtime_issue((3, 9, 0)))
        issue = server._python_runtime_issue((3, 8, 20))
        self.assertEqual(issue["dependency"], "python")
        self.assertEqual(issue["status"], "unsupported")
        self.assertEqual(issue["detected_version"], "3.8.20")
        self.assertEqual(issue["required_version"], "3.9+")

    def test_launcher_reports_unsupported_python_before_starting_controller(self) -> None:
        server = load_server()
        issue = {
            "kind": "dependency",
            "dependency": "python",
            "status": "unsupported",
            "message": "Python is unsupported.",
        }
        with (
            mock.patch.object(server, "_python_runtime_issue", return_value=issue),
            mock.patch.object(server, "_ensure_controller_daemon") as ensure,
        ):
            result = server._call_tool({"name": "open_controller", "arguments": {}})

        ensure.assert_not_called()
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["opened"])
        self.assertEqual(result["structuredContent"]["issue"], issue)

    def test_node_runtime_prefers_forwarded_codex_runtime(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            node = Path(temporary) / "node"
            node.touch()
            node.chmod(0o700)
            with (
                mock.patch.dict(
                    os.environ,
                    {"CODEX_MCP_NODE_PATH": str(node), "PATH": ""},
                    clear=True,
                ),
                mock.patch.object(server.shutil, "which", return_value=None),
            ):
                self.assertEqual(server._node_runtime(), str(node))

    def test_node_runtime_reports_missing_dependency(self) -> None:
        server = load_server()
        with (
            mock.patch.dict(os.environ, {"PATH": ""}, clear=True),
            mock.patch.object(server.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(server.ControllerError, "Node.js 18 or newer"):
                server._node_runtime()

    def test_only_external_browser_launcher_is_model_visible(self) -> None:
        server = load_server()
        tools = server.tool_definitions()
        self.assertEqual(
            [item["name"] for item in tools],
            ["open_controller", "stop_port_process_and_open_controller"],
        )
        launcher = tools[0]
        self.assertIn("external browser", launcher["description"])
        self.assertNotIn("ui", launcher["_meta"])
        self.assertNotIn("ui/resourceUri", launcher["_meta"])
        self.assertNotIn("openai/outputTemplate", launcher["_meta"])
        self.assertTrue(launcher["annotations"]["readOnlyHint"])
        self.assertFalse(tools[1]["annotations"]["readOnlyHint"])
        self.assertTrue(all(item["execution"]["taskSupport"] == "forbidden" for item in tools))

    def test_stdio_handshake_lists_launcher_without_embedded_resource(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        ]
        completed = subprocess.run(
            [sys.executable, str(SERVER_PATH)],
            cwd=PLUGIN_ROOT,
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(
            responses[0]["result"]["serverInfo"]["name"],
            "codex-bakeoff",
        )
        self.assertEqual(
            [item["name"] for item in responses[1]["result"]["tools"]],
            ["open_controller", "stop_port_process_and_open_controller"],
        )
        self.assertEqual(responses[2]["result"]["resources"], [])

    def test_launcher_opens_authenticated_external_url(self) -> None:
        server = load_server()
        with (
            mock.patch.object(
                server,
                "_ensure_controller_daemon",
                return_value=(43117, {"version": "test-version"}),
            ),
            mock.patch.object(
                server,
                "_request_browser_login",
                return_value="http://127.0.0.1:43117/auth?token=secret",
            ),
            mock.patch.object(server.webbrowser, "open", return_value=True) as browser_open,
        ):
            result = server._call_tool({"name": "open_controller", "arguments": {}})

        browser_open.assert_called_once_with(
            "http://127.0.0.1:43117/auth?token=secret",
            new=2,
            autoraise=True,
        )
        self.assertTrue(result["structuredContent"]["opened"])
        self.assertEqual(
            result["structuredContent"]["origin"],
            "http://127.0.0.1:43117",
        )

    def test_port_conflict_requests_confirmation_without_stopping_process(self) -> None:
        server = load_server()
        conflict = server.PortConflictError(43117, 1234, "python3")
        with (
            mock.patch.object(
                server,
                "_ensure_controller_daemon",
                side_effect=conflict,
            ),
            mock.patch.object(server.os, "kill") as kill,
        ):
            result = server._call_tool({"name": "open_controller", "arguments": {}})

        kill.assert_not_called()
        structured = result["structuredContent"]
        self.assertFalse(structured["opened"])
        self.assertTrue(structured["requires_confirmation"])
        self.assertEqual(structured["port"], 43117)
        self.assertEqual(structured["pid"], 1234)
        self.assertEqual(structured["process_name"], "python3")
        self.assertGreaterEqual(len(structured["confirmation_token"]), 32)

    def test_confirmed_port_process_is_stopped_before_controller_opens(self) -> None:
        server = load_server()
        token = "confirmation-" + ("x" * 32)
        with (
            mock.patch.object(
                server,
                "_ensure_controller_daemon",
                return_value=(43117, {"version": "test-version"}),
            ) as ensure,
            mock.patch.object(
                server,
                "_request_browser_login",
                return_value="http://127.0.0.1:43117/auth?token=secret",
            ),
            mock.patch.object(server.webbrowser, "open", return_value=True),
        ):
            result = server._call_tool(
                {
                    "name": "stop_port_process_and_open_controller",
                    "arguments": {
                        "confirmed": True,
                        "confirmation_token": token,
                    },
                }
            )

        ensure.assert_called_once_with(confirmation_token=token)
        self.assertTrue(result["structuredContent"]["opened"])

    def test_stop_tool_requires_explicit_confirmation(self) -> None:
        server = load_server()
        with self.assertRaisesRegex(
            server.ControllerError,
            "Explicit user confirmation",
        ):
            server._call_tool(
                {
                    "name": "stop_port_process_and_open_controller",
                    "arguments": {
                        "confirmed": False,
                        "confirmation_token": "x" * 32,
                    },
                }
            )

    def test_confirmed_listener_must_still_match_before_termination(self) -> None:
        server = load_server()
        confirmed = {"pid": 1234, "process_name": "python3"}
        replacement = {"port": 43117, "pid": 5678, "process_name": "node"}
        with (
            mock.patch.object(server, "_port_listener", return_value=replacement),
            mock.patch.object(server.os, "kill") as kill,
        ):
            with self.assertRaises(server.PortConflictError) as raised:
                server._stop_confirmed_port_listener(43117, confirmed)

        kill.assert_not_called()
        self.assertEqual(raised.exception.pid, 5678)
        self.assertEqual(raised.exception.process_name, "node")

    def test_confirmed_listener_receives_sigterm(self) -> None:
        server = load_server()
        confirmed = {"pid": 1234, "process_name": "python3"}
        listener = {"port": 43117, **confirmed}
        with (
            mock.patch.object(server, "_port_listener", return_value=listener),
            mock.patch.object(
                server,
                "_port_accepts_connections",
                return_value=False,
            ),
            mock.patch.object(server.os, "kill") as kill,
        ):
            server._stop_confirmed_port_listener(43117, confirmed)

        kill.assert_called_once_with(1234, server.signal.SIGTERM)

    def test_loopback_controller_auth_and_same_origin_api(self) -> None:
        server = load_server()
        control_token = "control-" + ("x" * 48)
        httpd = server._ControllerHTTPServer(("127.0.0.1", 0), control_token)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = int(httpd.server_address[1])

        def request(
            method: str,
            path: str,
            *,
            body: str | None = None,
            headers: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, str], bytes]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        try:
            challenge = "test-challenge"
            status, _, body = request("GET", f"/health?challenge={challenge}")
            self.assertEqual(status, 200)
            health = json.loads(body)
            self.assertEqual(health["server"], "codex-bakeoff")
            self.assertEqual(
                health["proof"],
                hmac.new(
                    control_token.encode(),
                    challenge.encode(),
                    hashlib.sha256,
                ).hexdigest(),
            )

            status, _, _ = request("POST", "/api/launch", body="{}")
            self.assertEqual(status, 401)

            status, _, body = request(
                "POST",
                "/api/launch",
                body="{}",
                headers={
                    "Content-Type": "application/json",
                    "X-Codex-Bakeoff-Control": control_token,
                },
            )
            self.assertEqual(status, 200)
            auth_path = json.loads(body)["path"]

            status, _, body = request("GET", auth_path)
            self.assertEqual(status, 200)
            self.assertIn(b"localStorage.setItem", body)
            self.assertNotIn(b"sessionStorage", body)
            session_token = next(iter(httpd.session_tokens))

            status, _, body = request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"codex-bakeoff.controller-draft.v1", body)
            self.assertNotIn(b"window.openai", body)

            tool_result = {
                "content": [{"type": "text", "text": "Ready."}],
                "structuredContent": {"state": {"models": []}},
            }
            with mock.patch.object(server, "_call_tool", return_value=tool_result):
                status, _, body = request(
                    "POST",
                    "/api/call",
                    body=json.dumps({"name": "get_state", "arguments": {}}),
                    headers={
                        "Content-Type": "application/json",
                        "X-Codex-Bakeoff-Session": session_token,
                        "Origin": httpd.origin,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), tool_result)

                status, _, _ = request(
                    "POST",
                    "/api/call",
                    body=json.dumps({"name": "get_state", "arguments": {}}),
                    headers={
                        "Content-Type": "application/json",
                        "X-Codex-Bakeoff-Session": session_token,
                        "Origin": "https://example.com",
                    },
                )
                self.assertEqual(status, 403)
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def test_authenticated_report_downloads(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_directory.mkdir()
            artifacts = {
                "json": (
                    b'{"status":"completed"}\n',
                    "application/json; charset=utf-8",
                ),
                "html": (
                    b"<!doctype html><title>Bakeoff report</title>",
                    "text/html; charset=utf-8",
                ),
            }
            for artifact_format, (body, _) in artifacts.items():
                (run_directory / f"report.{artifact_format}").write_bytes(body)

            with mock.patch.object(server, "RUN_ROOT", run_root):
                httpd = server._ControllerHTTPServer(
                    ("127.0.0.1", 0),
                    "control-" + ("x" * 48),
                )
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                port = int(httpd.server_address[1])
                session_token = httpd.issue_session()

                def request(
                    artifact_format: str,
                    *,
                    authorized: bool = True,
                ) -> tuple[int, dict[str, str], bytes]:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    headers = {
                        "Content-Type": "application/json",
                        "Origin": httpd.origin,
                    }
                    if authorized:
                        headers["X-Codex-Bakeoff-Session"] = session_token
                    try:
                        connection.request(
                            "POST",
                            "/api/download",
                            body=json.dumps({"run_id": "run-1", "format": artifact_format}),
                            headers=headers,
                        )
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                    finally:
                        connection.close()

                try:
                    for artifact_format, (expected_body, content_type) in artifacts.items():
                        status, headers, body = request(artifact_format)
                        self.assertEqual(status, 200)
                        self.assertEqual(body, expected_body)
                        self.assertEqual(headers["Content-Type"], content_type)
                        self.assertEqual(
                            headers["Content-Disposition"],
                            (
                                f'attachment; filename="codex-bakeoff-run-1-report.'
                                f'{artifact_format}"'
                            ),
                        )

                    status, _, _ = request("html", authorized=False)
                    self.assertEqual(status, 401)
                finally:
                    httpd.shutdown()
                    thread.join(timeout=5)
                    httpd.server_close()

    def test_http_daemon_mode_starts_and_stops_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = int(reservation.getsockname()[1])
            environment = {
                **os.environ,
                "CODEX_BAKEOFF_RUN_ROOT": str(root / "runs"),
                "CODEX_BAKEOFF_CONTROLLER_PORT": str(port),
            }
            process = subprocess.Popen(
                [sys.executable, str(SERVER_PATH), "--http"],
                cwd=PLUGIN_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def request(
                method: str,
                path: str,
                *,
                body: str | None = None,
                headers: dict[str, str] | None = None,
            ) -> tuple[int, dict[str, str], bytes]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            try:
                deadline = time.monotonic() + 8
                while True:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(f"HTTP daemon exited early: {stdout}\n{stderr}")
                    try:
                        status, _, _ = request("GET", "/health")
                    except OSError:
                        status = 0
                    if status == 200:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("HTTP daemon did not become ready.")
                    time.sleep(0.05)

                runtime_path = root / "controller-server.json"
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                status, _, _ = request(
                    "POST",
                    "/api/shutdown",
                    body="{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Codex-Bakeoff-Control": runtime["control_token"],
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(process.wait(timeout=8), 0)
                process.communicate()
                self.assertFalse(runtime_path.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=8)
                    process.communicate()

    def test_controller_uses_versioned_local_storage_draft(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        snapshot = controller.split("function draftSnapshot()", 1)[1].split(
            "function saveDraft()", 1
        )[0]
        self.assertIn("codex-bakeoff.controller-draft.v1", controller)
        self.assertIn("codex-bakeoff.controller-session.v1", controller)
        self.assertIn("X-Codex-Bakeoff-Session", controller)
        self.assertIn("localStorage.getItem(CONTROLLER_SESSION_STORAGE_KEY)", controller)
        self.assertNotIn("sessionStorage", controller)
        self.assertIn('fetch("/api/call"', controller)
        self.assertIn('fetch("/api/download"', controller)
        self.assertNotIn("window.openai", controller)
        self.assertIn(
            'data-action="download-report" data-format="json">Download JSON',
            controller,
        )
        self.assertIn(
            'data-action="download-report" data-format="html">Download HTML report',
            controller,
        )
        download_report = controller.split("async function downloadReport", 1)[1].split(
            'app.addEventListener("click"', 1
        )[0]
        self.assertIn("URL.createObjectURL", download_report)
        self.assertIn("URL.revokeObjectURL", download_report)
        for field in (
            "selectedThreadId",
            "model",
            "timeoutSeconds",
            "classifications",
            "gitSelectionConfirmed",
            "nonGitBaseline",
        ):
            self.assertIn(field, snapshot)
        for transient in ("preparation", "approvalChecked", "runId", "report"):
            self.assertNotIn(transient, snapshot)
        start_run = controller.split("async function startRun()", 1)[1].split(
            "async function refreshRun", 1
        )[0]
        self.assertIn("clearDraft()", start_run)
        self.assertLess(
            start_run.index("startPending = true"),
            start_run.index('callTool("start_run"'),
        )
        self.assertLess(
            start_run.index("clearDraft()"),
            start_run.index('callTool("start_run"'),
        )
        select_thread = controller.split("async function selectThread", 1)[1].split(
            "async function prepareRun", 1
        )[0]
        self.assertLess(
            select_thread.index("state.classifications = Object.create(null)"),
            select_thread.index("render();"),
        )

    def test_controller_report_restores_comparisons_and_token_breakdown(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        self.assertIn("Claude worktree", controller)
        self.assertIn("Codex worktree", controller)
        self.assertIn("Claude patch", controller)
        self.assertIn("Claude final response", controller)
        self.assertNotIn("Historical Claude patch", controller)
        self.assertNotIn("Historical Claude final response", controller)
        for label in (
            "Total input processed",
            "Ordinary input tokens",
            "Cache-read tokens",
            "Cache-write tokens",
            "Output tokens",
        ):
            self.assertIn(label, controller)
        for description in (
            "All input tokens processed: ordinary input, cache reads, and cache writes.",
            "Input tokens processed without being read from or written to a provider cache.",
            "Previously cached input tokens reused by the model.",
            "Input tokens written to the provider cache for possible reuse.",
            "Tokens generated by the model in its responses.",
        ):
            self.assertIn(description, controller)
        self.assertIn('class="token-label" tabindex="0"', controller)
        self.assertIn('class="token-tooltip" role="tooltip"', controller)
        self.assertIn("function comparisonClass(value, otherValue)", controller)
        self.assertIn("metric--better", controller)
        self.assertIn("metric--worse", controller)
        self.assertIn("comparisonClass(elapsed, otherElapsed)", controller)
        self.assertIn("comparisonClass(cost?.usd, otherCost?.usd)", controller)

    def test_controller_launch_ignores_completed_runs(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        initializer = controller.split("async function initialize()", 1)[1].split(
            "async function selectThread", 1
        )[0]
        self.assertIn('step: "thread"', controller)
        self.assertIn("const active = recent.find", initializer)
        self.assertIn("if (active)", initializer)
        self.assertNotIn("latestCompleted", initializer)
        self.assertNotIn("loadReport", initializer)

    def test_prepare_maps_ui_classification_to_existing_engine_flags(self) -> None:
        server = load_server()
        observed: list[tuple[str, list[str]]] = []

        def fake_engine(command: str, arguments=()):
            observed.append((command, list(arguments)))
            return {
                "status": "ready_for_approval",
                "blocking_reasons": [],
                "approval_prompt": "Approve?",
                "configuration": {"model": "gpt-5.6-terra"},
            }

        with mock.patch.object(server, "_engine", side_effect=fake_engine):
            payload = server._prepare_payload(
                {
                    "thread_id": "thread-1",
                    "model": "gpt-5.6-terra",
                    "timeout_seconds": 1200,
                    "created_by_claude": ["hello world.html"],
                    "excluded_files": ["generated.js"],
                    "confirm_file_selection": True,
                }
            )

        self.assertTrue(payload["ready"])
        self.assertGreaterEqual(len(payload["prepare_token"]), 32)
        self.assertEqual(
            payload["approval"]["prepare_token"],
            payload["prepare_token"],
        )
        self.assertEqual(observed[0][0], "prepare")
        arguments = observed[0][1]
        self.assertIn("--created-by-claude", arguments)
        self.assertIn("hello world.html", arguments)
        self.assertIn("--exclude-file", arguments)
        self.assertIn("generated.js", arguments)
        self.assertIn("--confirm-file-selection", arguments)

    def test_prepare_token_binds_config_and_makes_start_idempotent(self) -> None:
        server = load_server()

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_calls = 0

            def fake_engine(command: str, arguments=(), **kwargs):
                nonlocal run_calls
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "blocking_reasons": [],
                        "approval_prompt": "Approve?",
                    }
                if command == "run":
                    run_calls += 1
                    run_directory.mkdir()
                    return {
                        "status": "native_task_required",
                        "run_directory": str(run_directory),
                        "task_request": {},
                    }
                raise AssertionError(command)

            config = {
                "thread_id": "thread-1",
                "model": "gpt-5.6-terra",
                "timeout_seconds": 1200,
            }
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(server.threading, "Thread", FakeThread),
            ):
                prepared = server._prepare_payload(config)
                approved = {
                    **config,
                    "approved": True,
                    "prepare_token": prepared["prepare_token"],
                }
                first = server._start_run(approved)
                second = server._start_run(approved)
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "model": "gpt-5.6-sol"})

            self.assertEqual(run_calls, 1)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["run_id"], second["run_id"])

    def test_long_command_timeout_tracks_bounded_run_timeout(self) -> None:
        server = load_server()
        self.assertEqual(
            server._long_command_timeout({"timeout_seconds": 1200}, default=1800),
            1500,
        )
        self.assertEqual(
            server._long_command_timeout({"timeout_seconds": 14_400}, default=1800),
            server.MAX_COMMAND_TIMEOUT,
        )

    def test_worker_failure_prefers_structured_error_over_stderr_warning(self) -> None:
        server = load_server()
        completed = subprocess.CompletedProcess(
            args=["/runtime/node", "worker.mjs"],
            returncode=1,
            stdout=(
                '{"type":"ready","protocolVersion":1}\n'
                '{"type":"failed","id":"run-1","code":"stream_error",'
                '"message":"Codex reported an unrecoverable stream error."}\n'
            ),
            stderr="unrelated runtime warning",
        )
        with (
            mock.patch.object(server, "_node_runtime", return_value="/runtime/node"),
            mock.patch.object(
                server,
                "_run_process",
                return_value=completed,
            ) as run_process,
        ):
            with self.assertRaisesRegex(
                server.ControllerError,
                "stream_error: Codex reported an unrecoverable stream error",
            ):
                server._run_worker(
                    {
                        "model": "gpt-5.6-luna",
                        "prompt": "test",
                        "timeout_seconds": 30,
                    },
                    working_directory=PLUGIN_ROOT,
                    read_only=True,
                )
        self.assertEqual(
            run_process.call_args.args[0],
            ["/runtime/node", str(server.WORKER)],
        )

    def test_get_report_accepts_report_larger_than_legacy_limit(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-large"
            run_directory.mkdir()
            (run_directory / "report.json").write_text(
                json.dumps({"original_request": "x" * (600 * 1024)}),
                encoding="utf-8",
            )
            with mock.patch.object(server, "RUN_ROOT", run_root):
                result = server._call_tool(
                    {
                        "name": "get_report",
                        "arguments": {"run_id": "run-large"},
                    }
                )
        self.assertEqual(
            len(result["structuredContent"]["report"]["original_request"]),
            600 * 1024,
        )

    def test_startup_reconciliation_only_fails_stale_running_state(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            stale_directory = run_root / "stale"
            live_directory = run_root / "live"
            stale_directory.mkdir()
            live_directory.mkdir()
            stale = server._initial_state(stale_directory)
            stale["controller_pid"] = 99_999_999
            live = server._initial_state(live_directory)
            live["controller_pid"] = os.getpid()
            server._write_json(server._state_path(stale_directory), stale)
            server._write_json(server._state_path(live_directory), live)

            with mock.patch.object(server, "RUN_ROOT", run_root):
                server._reconcile_interrupted_runs()

            stale_after = server._read_json(server._state_path(stale_directory))
            live_after = server._read_json(server._state_path(live_directory))
        self.assertEqual(stale_after["status"], "failed")
        self.assertTrue(stale_after["interrupted"])
        self.assertEqual(live_after["status"], "running")

    def test_shutdown_terminates_tracked_process_groups(self) -> None:
        server = load_server()
        process = mock.Mock()
        server._active_processes.add(process)
        try:
            with mock.patch.object(server, "_terminate_process_group") as terminate:
                server._stop_jobs()
            terminate.assert_called_once_with(process)
        finally:
            server._active_processes.discard(process)

    def test_reviews_run_with_only_copied_anonymous_candidates(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            reviews = run_directory / "reviews"
            reviews.mkdir()
            candidate_paths = []
            for label in ("a", "b"):
                path = reviews / f"candidate-{label}.json"
                path.write_text(json.dumps({"label": label.upper()}), encoding="utf-8")
                candidate_paths.append(str(path))
            prompt = f"Read {candidate_paths[0]} and {candidate_paths[1]}"

            def fake_worker(request, *, working_directory, read_only):
                self.assertTrue(read_only)
                self.assertEqual(
                    sorted(item.name for item in working_directory.iterdir()),
                    ["candidate-a.json", "candidate-b.json"],
                )
                self.assertNotIn(candidate_paths[0], request["prompt"])
                self.assertNotIn(candidate_paths[1], request["prompt"])
                self.assertTrue(
                    all(
                        Path(path).parent == working_directory
                        for path in request["candidate_paths"]
                    )
                )
                return {"thread_id": "review-thread", "worktree": str(working_directory)}

            with (
                mock.patch.object(server, "_run_worker", side_effect=fake_worker),
                mock.patch.object(
                    server,
                    "_collect_result",
                    return_value={"native_result_path": str(reviews / "result.json")},
                ),
            ):
                server._run_review_requests(
                    run_directory,
                    [
                        {
                            "evaluator": "codex",
                            "model": "gpt-test",
                            "prompt": prompt,
                            "candidate_paths": candidate_paths,
                        }
                    ],
                )

            self.assertEqual(list((run_directory / "review-workspaces").iterdir()), [])

    def test_normalization_runs_in_empty_workspace(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()

            def fake_worker(request, *, working_directory, read_only):
                self.assertEqual(list(working_directory.iterdir()), [])
                return {"thread_id": "normalize-thread", "worktree": str(working_directory)}

            with (
                mock.patch.object(server, "_run_worker", side_effect=fake_worker),
                mock.patch.object(
                    server,
                    "_collect_result",
                    return_value={"native_result_path": str(run_directory / "normalized.json")},
                ),
            ):
                server._run_review_requests(
                    run_directory,
                    [
                        {
                            "normalization_for": "codex",
                            "model": "gpt-test",
                            "prompt": "Normalize this ballot.",
                        }
                    ],
                    normalization=True,
                )

    def test_materialized_git_workspace_is_isolated_at_requested_commit(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "source"
            run_directory = root / "run"
            repository.mkdir()
            run_directory.mkdir()
            subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Bakeoff Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "bakeoff@example.com"],
                check=True,
            )
            source_file = repository / "index.html"
            source_file.write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "index.html"], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-m", "baseline"],
                check=True,
                capture_output=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            workspace = server._materialize_workspace(
                run_directory,
                {
                    "type": "project",
                    "project": str(repository),
                    "environment": {
                        "type": "worktree",
                        "startingState": {"type": "branch", "branchName": commit},
                    },
                },
            )
            (workspace / "index.html").write_text("candidate\n", encoding="utf-8")

            self.assertEqual(source_file.read_text(encoding="utf-8"), "baseline\n")
            observed = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(observed, commit)


if __name__ == "__main__":
    unittest.main()
