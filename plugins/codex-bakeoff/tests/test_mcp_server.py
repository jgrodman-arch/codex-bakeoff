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
        self.assertIn("codex_cli_path", launcher["inputSchema"]["properties"])
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

    def test_launcher_carries_invoking_task_codex_path_to_worker(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch()
            codex.chmod(0o700)
            hint_path = root / "codex-cli-path.json"
            with (
                mock.patch.object(server, "CODEX_CLI_PATH_HINT_PATH", hint_path),
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
                mock.patch.object(server.webbrowser, "open", return_value=True),
            ):
                result = server._call_tool(
                    {
                        "name": "open_controller",
                        "arguments": {"codex_cli_path": str(codex)},
                    }
                )
                with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
                    worker_environment = server._worker_environment()

            self.assertTrue(result["structuredContent"]["opened"])
            self.assertEqual(worker_environment["CODEX_CLI_PATH"], str(codex.resolve()))
            self.assertEqual(
                json.loads(hint_path.read_text(encoding="utf-8"))["path"],
                str(codex.resolve()),
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
            self.assertIn(b"localStorage.clear()", body)
            self.assertIn(b"codex-bakeoff.controller-instance.v1", body)
            self.assertIn(httpd.instance_id.encode(), body)
            self.assertIn(b"localStorage.setItem", body)
            self.assertNotIn(b"sessionStorage", body)
            session_token = next(iter(httpd.session_tokens))

            with mock.patch.object(server, "APP_HTML", Path("/deleted/controller.html")):
                status, _, body = request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"codex-bakeoff.controller-draft.v5", body)
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
        self.assertIn("codex-bakeoff.controller-draft.v5", controller)
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
        self.assertIn("function renderDiagnostics()", controller)
        self.assertIn("Current run log", controller)
        self.assertIn("Controller log", controller)
        self.assertIn("serverState.run_root", controller)
        self.assertIn("serverState.controller_log_path", controller)
        for field in (
            "selectedThreadId",
            "model",
            "timeoutSeconds",
            "classifications",
            "reviewDraft",
            "configurationStep",
            "configurationMaxStep",
        ):
            self.assertIn(field, snapshot)
        for transient in ("preparation", "approvalChecked", "runId", "report"):
            self.assertNotIn(transient, snapshot)

        steps = controller.split("const STEPS = [", 1)[1].split("];", 1)[0]
        self.assertIn('id: "configure"', steps)
        self.assertNotIn('id: "review"', steps)
        self.assertIn('id: "run"', steps)
        self.assertIn('id: "results"', steps)

        configure = controller.split("function renderReplayConfiguration", 1)[1].split(
            "function normalizedPhases", 1
        )[0]
        self.assertIn("Original Claude thread ID", configure)
        self.assertIn('id="original-thread"', configure)
        self.assertIn("readonly", configure)
        self.assertIn("Message ID within the original thread", configure)
        self.assertIn("Beginning state", configure)
        self.assertIn("End state", configure)
        self.assertIn("Starting commit", configure)
        self.assertNotIn("empty-beginning-confirmation", configure)
        self.assertNotIn("I confirmed the directory was empty", configure)
        self.assertIn(
            'confirm_empty_beginning: draft.beginning_kind === "non_git"',
            controller,
        )
        refresh_check = controller.split("function selectionNeedsRefresh()", 1)[1].split(
            "function selectionPayload", 1
        )[0]
        self.assertNotIn("ending_commit", refresh_check)
        self.assertIn("Ending commit", configure)
        self.assertIn('id="review-ending-commit"', configure)
        self.assertIn("Historical output", configure)
        self.assertIn("Unchecked files will be ignored", configure)
        self.assertIn("Reconstructed task prompt", configure)
        self.assertIn("All locally available Codex models in one list", configure)
        self.assertNotIn(
            "I reviewed every current Git change and this attribution is complete",
            controller,
        )
        self.assertIn('finalStep ? "approve-configuration"', configure)
        self.assertIn("Checking configuration", configure)
        self.assertIn("Review and confirm", configure)
        self.assertIn("Approve with gaps and start", configure)
        self.assertIn('finalStep ? "approve-configuration" : "configuration-next"', configure)
        self.assertIn('data-action="configuration-back"', configure)
        self.assertIn("refreshAttributionAndContinue", controller)

        diagnostics = controller.split("function inspectionDiagnostics()", 1)[1].split(
            "function reviewDraftFromConfiguration", 1
        )[0]
        self.assertIn("repositoryBlockers", diagnostics)
        self.assertIn("!repositoryBlockers.has(item)", diagnostics)

        rail = controller.split("function renderRail()", 1)[1].split(
            "function renderDiagnostics", 1
        )[0]
        self.assertIn('step.id === "run"', rail)
        self.assertIn("Complete required fields before starting", rail)
        self.assertIn('class="step-tooltip" role="tooltip"', rail)
        self.assertIn("Reconstructing task prompt", configure)
        self.assertIn('requestMethod === "pending"', configure)
        self.assertNotIn('requestMethod === "pending" ? "disabled"', configure)
        self.assertNotIn("Validate configuration", controller)
        self.assertNotIn("Choose a baseline", controller)
        self.assertNotIn("Source transcript path", controller)
        self.assertNotIn("Imported thread ID", controller)
        self.assertNotIn("Claude output paths", controller)
        self.assertNotIn(
            "Current Git changes not listed here remain protected",
            controller,
        )
        self.assertNotIn('id="review-thread"', controller)
        self.assertNotIn('id="review-source-path"', controller)
        self.assertNotIn("function renderReviewStep", controller)
        self.assertNotIn('id="approval-dialog"', controller)
        self.assertNotIn('id="approval-check"', controller)

        prepare_run = controller.split("async function prepareRun()", 1)[1].split(
            "async function startRun", 1
        )[0]
        self.assertIn("const reviewRevision = state.reviewRevision", prepare_run)
        self.assertIn('callTool("prepare_run", configuration)', prepare_run)
        self.assertIn("reviewRevision !== state.reviewRevision", prepare_run)
        self.assertIn('state.step !== "configure"', prepare_run)
        self.assertIn("if (shouldStart) await startRun()", prepare_run)
        self.assertNotIn("showModal", prepare_run)
        self.assertIn("approvedConfiguration()", controller)

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
        self.assertNotIn("approvalChecked", start_run)
        select_thread = controller.split("async function selectThread", 1)[1].split(
            "async function prepareRun", 1
        )[0]
        self.assertIn("if (state.busy || state.run) return", select_thread)
        self.assertIn(
            'shouldSynthesize = state.promptGeneration === "pending"',
            select_thread,
        )
        self.assertIn("state.reviewDraft = reviewDraftFromConfiguration()", select_thread)
        self.assertLess(
            select_thread.index("state.classifications = Object.create(null)"),
            select_thread.index("render();"),
        )
        navigation = controller.split("function canNavigateTo", 1)[1].split(
            "function renderRail", 1
        )[0]
        self.assertIn("if (state.busy && !state.run) return false", navigation)
        self.assertNotIn("return id === \"run\"", navigation)

        click_handler = controller.split('app.addEventListener("click"', 1)[1].split(
            'app.addEventListener("input"', 1
        )[0]
        go_step = click_handler.split('if (action === "go-step")', 1)[1]
        self.assertNotIn("pollGeneration", go_step)

        thread_step = controller.split("function renderThreadStep", 1)[1].split(
            "function capabilityRows", 1
        )[0]
        self.assertIn("Inspecting", thread_step)
        self.assertIn("state.busy === \"inspection\"", thread_step)
        self.assertIn("state.run ? \"disabled\"", thread_step)
        self.assertIn("Start another bakeoff", thread_step)

        synthesis = controller.split("async function synthesizePrompt", 1)[1].split(
            "function resetController", 1
        )[0]
        self.assertIn('callTool("synthesize_request"', synthesis)
        self.assertIn("thread_id: threadIdValue", synthesis)
        self.assertIn("state.promptEditRevision === editRevision", synthesis)
        self.assertIn("text(state.reviewDraft?.request) === fallbackRequest", synthesis)
        review_problems = controller.split("function reviewProblems", 1)[1].split(
            "function invalidateReviewPreparation", 1
        )[0]
        self.assertNotIn("Wait for task prompt reconstruction to finish", review_problems)
        prepare_run = controller.split("async function prepareRun", 1)[1].split(
            "async function startRun", 1
        )[0]
        self.assertIn('state.promptGeneration === "pending"', prepare_run)
        self.assertIn("state.promptSynthesisGeneration += 1", prepare_run)
        configure = controller.split("function renderReplayConfiguration", 1)[1].split(
            "function normalizedPhases", 1
        )[0]
        self.assertNotIn('requestMethod === "pending" ? "disabled"', configure)

        initializer = controller.split("async function initialize()", 1)[1].split(
            "async function selectThread", 1
        )[0]
        self.assertIn('state.promptGeneration === "pending"', initializer)

        run_step = controller.split("function renderRunStep", 1)[1].split(
            "function reportParts", 1
        )[0]
        self.assertIn("const runLog = text(run.run_log)", run_step)
        self.assertIn('id="run-log"', run_step)
        self.assertIn("Retained", run_step)
        self.assertIn('data-action="cancel-run"', run_step)
        self.assertIn("Cancel run", run_step)
        cancel_run = controller.split("async function cancelRun", 1)[1].split(
            "function schedulePoll", 1
        )[0]
        self.assertIn('callTool("cancel_run"', cancel_run)
        self.assertIn("state.pollGeneration += 1", cancel_run)

    def test_controller_shows_capability_status_and_optional_guidance(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        capability_rows = controller.split("function capabilityRows", 1)[1].split(
            "function fileMetadata", 1
        )[0]
        configure = controller.split("function renderCapabilityConfiguration", 1)[1].split(
            "function renderConfigurationConfirmation", 1
        )[0]

        self.assertIn("titleCase(record.status)", capability_rows)
        self.assertIn("record.guidance", capability_rows)
        self.assertIn("not_available", capability_rows)
        self.assertIn('id="capability-heading"', configure)
        self.assertIn("item.remediation_action", configure)
        self.assertIn("may affect comparison fairness", configure)

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
        self.assertIn('value === null || value === undefined || value === ""', controller)
        self.assertIn("metric--better", controller)
        self.assertIn("metric--worse", controller)
        self.assertIn("comparisonClass(elapsed, otherElapsed)", controller)
        self.assertIn("comparisonClass(cost?.usd, otherCost?.usd)", controller)
        self.assertIn("function evaluationTable(evaluation)", controller)
        self.assertIn("expandCandidateLabels(decision.explanation)", controller)
        self.assertIn("mapping[label] === \"codex\" ? \"Codex\"", controller)
        self.assertIn("mapping[label] === \"claude\" ? \"Claude\"", controller)

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

    def test_inspection_keeps_partial_results_and_reports_failed_steps(self) -> None:
        server = load_server()

        def fake_engine(command: str, arguments=()):
            if command == "replay":
                raise server.ControllerError("The transcript is ambiguous.")
            if command == "baseline":
                raise server.ControllerError("The repository is ambiguous.")
            if command == "capabilities":
                return {"items": [{"name": "shell"}]}
            if command == "models":
                return {"options": [{"id": "gpt-test"}]}
            raise AssertionError(command)

        with mock.patch.object(server, "_engine", side_effect=fake_engine):
            inspected = server._inspect_thread({"thread_id": "thread-1"})

        self.assertEqual(inspected["replay"]["imported_thread_id"], "thread-1")
        self.assertEqual(inspected["baseline"], {})
        self.assertEqual(inspected["capabilities"]["items"][0]["name"], "shell")
        self.assertEqual(inspected["models"], [{"id": "gpt-test"}])
        self.assertEqual(
            inspected["replay"]["request_generation"],
            {"method": "concatenated_fallback"},
        )
        self.assertEqual(
            [item["step"] for item in inspected["diagnostics"]],
            ["thread", "baseline"],
        )

    def test_inspection_returns_pending_without_running_synthesis_worker(self) -> None:
        server = load_server()
        turns = [
            {"role": "user", "text": "add hello world"},
            {
                "role": "assistant",
                "text": "1. Add docs?\n2. Add a CLI?\n3. Add a simple program?",
            },
            {"role": "user", "text": "3"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "transcript.jsonl"
            transcript.write_text("source transcript", encoding="utf-8")

            def fake_engine(command: str, arguments=()):
                if command == "replay":
                    return {
                        "replay": {
                            "imported_thread_id": "thread-1",
                            "source_path": str(transcript),
                            "request": "add hello world\n\n3",
                            "prompt_reconstruction_turns": turns,
                            "prompt_reconstruction_truncated": False,
                        }
                    }
                if command in {"capabilities", "baseline"}:
                    return {}
                if command == "models":
                    return {"options": [{"id": "gpt-5.6-terra"}]}
                raise AssertionError(command)

            with (
                mock.patch.object(server, "REQUEST_SYNTHESIS_CACHE_PATH", root / "cache.json"),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(server, "_run_worker") as worker,
            ):
                inspected = server._inspect_thread({"thread_id": "thread-1"})

        worker.assert_not_called()
        self.assertEqual(inspected["replay"]["request"], "add hello world\n\n3")
        self.assertEqual(inspected["replay"]["request_generation"], {"method": "pending"})
        for record in (inspected["thread"], inspected["replay"]):
            self.assertNotIn("prompt_reconstruction_turns", record)
            self.assertNotIn("prompt_reconstruction_truncated", record)

    def test_single_user_prompt_skips_synthesis(self) -> None:
        server = load_server()
        direct_request = "Generate a concise morning brief."

        def fake_engine(command: str, arguments=()):
            if command == "replay":
                return {
                    "replay": {
                        "imported_thread_id": "thread-1",
                        "source_path": "/tmp/transcript.jsonl",
                        "request": direct_request,
                        "prompt_reconstruction_turns": [
                            {"role": "user", "text": direct_request}
                        ],
                        "prompt_reconstruction_truncated": False,
                    }
                }
            if command in {"capabilities", "baseline"}:
                return {}
            if command == "models":
                return {"options": [{"id": "gpt-5.6-terra"}]}
            raise AssertionError(command)

        with (
            mock.patch.object(server, "_engine", side_effect=fake_engine) as engine,
            mock.patch.object(server, "_run_worker") as worker,
        ):
            inspected = server._inspect_thread({"thread_id": "thread-1"})
            synthesized = server._synthesize_request_payload({"thread_id": "thread-1"})

        expected = {"method": "single_user_prompt"}
        self.assertEqual(inspected["replay"]["request"], direct_request)
        self.assertEqual(inspected["replay"]["request_generation"], expected)
        self.assertEqual(synthesized["request"], direct_request)
        self.assertEqual(synthesized["request_generation"], expected)
        worker.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in engine.call_args_list].count("models"),
            1,
        )

    def test_synthesis_cache_hit_and_transcript_change_invalidation(self) -> None:
        server = load_server()
        fallback = "add hello world\n\n3"
        turns = [
            {"role": "user", "text": "add hello world"},
            {
                "role": "assistant",
                "text": "1. Add docs?\n2. Add a CLI?\n3. Add a simple program?",
            },
            {"role": "user", "text": "3"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "transcript.jsonl"
            cache_path = root / "request-cache.json"
            transcript.write_text("transcript version one", encoding="utf-8")
            model_calls = 0

            def fake_engine(command: str, arguments=()):
                nonlocal model_calls
                if command == "replay":
                    return {
                        "replay": {
                            "imported_thread_id": "thread-1",
                            "source_path": str(transcript),
                            "request": fallback,
                            "prompt_reconstruction_turns": turns,
                            "prompt_reconstruction_truncated": False,
                        }
                    }
                if command in {"capabilities", "baseline"}:
                    return {}
                if command == "models":
                    model_calls += 1
                    return {"options": [{"id": "gpt-5.6-terra"}]}
                raise AssertionError(command)

            summaries = [
                "Add a simple hello world program.",
                "Add a revised simple hello world program.",
            ]

            def fake_worker(
                request,
                *,
                run_directory,
                working_directory,
                read_only,
                log_label,
            ):
                call_index = fake_worker.calls
                fake_worker.calls += 1
                self.assertEqual(request["model"], "gpt-5.6-terra")
                self.assertEqual(request["expected_schema"], server.REQUEST_SYNTHESIS_SCHEMA)
                self.assertEqual(request["timeout_seconds"], 180)
                self.assertIn(json.dumps(turns, ensure_ascii=False), request["prompt"])
                self.assertTrue(read_only)
                self.assertEqual(run_directory, working_directory)
                self.assertTrue(working_directory.is_dir())
                self.assertEqual(log_label, "prompt-synthesis")
                return {"finalResponse": json.dumps({"request": summaries[call_index]})}

            fake_worker.calls = 0
            with (
                mock.patch.object(server, "REQUEST_SYNTHESIS_CACHE_PATH", cache_path),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(server, "_run_worker", side_effect=fake_worker) as worker,
            ):
                first = server._call_tool(
                    {"name": "synthesize_request", "arguments": {"thread_id": "thread-1"}}
                )["structuredContent"]
                inspected = server._inspect_thread({"thread_id": "thread-1"})
                second = server._synthesize_request_payload({"thread_id": "thread-1"})
                transcript.write_text("transcript version two", encoding="utf-8")
                third = server._synthesize_request_payload({"thread_id": "thread-1"})

            cache = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(first["request"], summaries[0])
        self.assertEqual(
            first["request_generation"],
            {
                "method": "llm_synthesis",
                "model": "gpt-5.6-terra",
                "generated_at": first["request_generation"]["generated_at"],
                "cached": False,
            },
        )
        self.assertEqual(inspected["replay"]["request"], summaries[0])
        self.assertTrue(inspected["replay"]["request_generation"]["cached"])
        self.assertEqual(second["request"], summaries[0])
        self.assertTrue(second["request_generation"]["cached"])
        self.assertEqual(third["request"], summaries[1])
        self.assertFalse(third["request_generation"]["cached"])
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(model_calls, 3)
        self.assertEqual(set(cache), {"thread-1"})
        self.assertEqual(
            set(cache["thread-1"]),
            {"thread_id", "transcript_sha256", "summary", "model", "generated_at"},
        )
        self.assertEqual(cache["thread-1"]["summary"], summaries[1])

    def test_failed_synthesis_keeps_exact_concat_and_is_not_cached(self) -> None:
        server = load_server()
        fallback = "add hello world\n\n3"
        outcomes = (
            server.WorkerError("stream_error", "failed"),
            {"finalResponse": "not json"},
            {"finalResponse": '{"request":"   "}'},
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                transcript = root / "transcript.jsonl"
                cache_path = root / "request-cache.json"
                transcript.write_text("source transcript", encoding="utf-8")

                def fake_engine(command: str, arguments=()):
                    if command == "replay":
                        return {
                            "replay": {
                                "source_path": str(transcript),
                                "request": fallback,
                                "prompt_reconstruction_turns": [
                                    {"role": "user", "text": "add hello world"},
                                    {"role": "user", "text": "3"},
                                ],
                                "prompt_reconstruction_truncated": False,
                            }
                        }
                    if command == "models":
                        return {"options": [{"id": "gpt-5.6-terra"}]}
                    raise AssertionError(command)

                worker_patch = (
                    mock.patch.object(server, "_run_worker", side_effect=outcome)
                    if isinstance(outcome, Exception)
                    else mock.patch.object(server, "_run_worker", return_value=outcome)
                )
                with (
                    mock.patch.object(server, "REQUEST_SYNTHESIS_CACHE_PATH", cache_path),
                    mock.patch.object(server, "_engine", side_effect=fake_engine),
                    worker_patch as worker,
                ):
                    first = server._synthesize_request_payload({"thread_id": "thread-1"})
                    second = server._synthesize_request_payload({"thread_id": "thread-1"})

                self.assertEqual(first["request"], fallback)
                self.assertEqual(
                    first["request_generation"],
                    {"method": "concatenated_fallback"},
                )
                self.assertEqual(second, first)
                self.assertEqual(worker.call_count, 2)
                self.assertFalse(cache_path.exists())

    def test_inspection_runs_discovery_concurrently_with_ordered_diagnostics(self) -> None:
        server = load_server()
        barrier = threading.Barrier(4)

        def fake_engine(command: str, arguments=()):
            barrier.wait(timeout=2)
            if command == "replay":
                raise server.ControllerError("thread failed")
            if command == "baseline":
                raise server.ControllerError("baseline failed")
            if command == "capabilities":
                return {"items": []}
            if command == "models":
                return {"options": []}
            raise AssertionError(command)

        with mock.patch.object(server, "_engine", side_effect=fake_engine):
            inspected = server._inspect_thread({"thread_id": "thread-1"})

        self.assertEqual(
            [item["step"] for item in inspected["diagnostics"]],
            ["thread", "baseline"],
        )

    def test_prepare_maps_ui_classification_to_existing_engine_flags(self) -> None:
        server = load_server()
        observed: list[tuple[str, list[str], str | None]] = []

        def fake_engine(command: str, arguments=(), **kwargs):
            observed.append((command, list(arguments), kwargs.get("input_text")))
            return {
                "status": "ready_for_approval",
                "blocking_reasons": [],
                "approval_prompt": "Approve?",
                "configuration": {"model": "gpt-5.6-terra"},
                "historical_result_sha256": "a" * 64,
                "prepared_configuration_sha256": "c" * 64,
            }

        with mock.patch.object(server, "_engine", side_effect=fake_engine):
            payload = server._prepare_payload(
                {
                    "thread_id": "thread-1",
                    "source_path": "/tmp/reviewed-transcript.jsonl",
                    "message_uuid": "message-1",
                    "request": "Build the reviewed thing.",
                    "repo": "/tmp/reviewed-repo",
                    "beginning_kind": "non_git",
                    "ending_kind": "non_git",
                    "confirm_empty_beginning": True,
                    "confirm_repository_selection": True,
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
        self.assertIn("--request-stdin", arguments)
        self.assertNotIn("Build the reviewed thing.", arguments)
        self.assertEqual(observed[0][2], "Build the reviewed thing.")
        self.assertIn("--beginning-kind", arguments)
        self.assertIn("--ending-kind", arguments)
        self.assertIn("non_git", arguments)
        self.assertIn("--confirm-empty-beginning", arguments)
        self.assertIn("--confirm-repository-selection", arguments)
        self.assertIn("--source-path", arguments)
        self.assertIn("/tmp/reviewed-transcript.jsonl", arguments)
        self.assertIn("--message-uuid", arguments)
        self.assertIn("message-1", arguments)
        self.assertEqual(payload["run_config"]["request"], "Build the reviewed thing.")

    def test_reviewed_git_configuration_requires_a_commit(self) -> None:
        server = load_server()
        with self.assertRaisesRegex(server.ControllerError, "historical Git commit"):
            server._normalized_configuration(
                {
                    "thread_id": "thread-1",
                    "request": "Build it.",
                    "repo": "/tmp/repository",
                    "beginning_kind": "git",
                    "ending_kind": "git",
                    "model": "gpt-test",
                    "timeout_seconds": 1200,
                }
            )

    def test_reviewed_git_configuration_requires_an_ending_commit(self) -> None:
        server = load_server()
        with self.assertRaisesRegex(server.ControllerError, "historical ending Git commit"):
            server._normalized_configuration(
                {
                    "thread_id": "thread-1",
                    "request": "Build it.",
                    "repo": "/tmp/repository",
                    "beginning_kind": "git",
                    "ending_kind": "git",
                    "baseline_commit": "a" * 40,
                    "model": "gpt-test",
                    "timeout_seconds": 1200,
                }
            )

    def test_reviewed_git_configuration_forwards_both_commits(self) -> None:
        server = load_server()
        arguments = server._configuration_arguments(
            {
                "thread_id": "thread-1",
                "request": "Build it.",
                "repo": "/tmp/repository",
                "beginning_kind": "git",
                "ending_kind": "git",
                "baseline_commit": "a" * 40,
                "ending_commit": "b" * 40,
                "model": "gpt-test",
                "timeout_seconds": 1200,
            }
        )
        self.assertIn("--baseline-commit", arguments)
        self.assertIn("a" * 40, arguments)
        self.assertIn("--ending-commit", arguments)
        self.assertIn("b" * 40, arguments)

    def test_reviewed_state_kinds_reject_non_text_values(self) -> None:
        server = load_server()
        for field in ("beginning_kind", "ending_kind"):
            for value in ([], {}):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    server.ControllerError,
                    "Git or Non-Git",
                ):
                    server._normalized_configuration(
                        {
                            "thread_id": "thread-1",
                            field: value,
                            "model": "gpt-test",
                        }
                    )

    def test_git_beginning_rejects_non_git_end(self) -> None:
        server = load_server()
        with self.assertRaisesRegex(server.ControllerError, "requires a Git end state"):
            server._normalized_configuration(
                {
                    "thread_id": "thread-1",
                    "beginning_kind": "git",
                    "ending_kind": "non_git",
                    "baseline_commit": "a" * 40,
                    "model": "gpt-test",
                }
            )

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
                        "historical_result_sha256": "b" * 64,
                        "prepared_configuration_sha256": "d" * 64,
                    }
                if command == "run":
                    run_calls += 1
                    self.assertEqual(
                        kwargs.get("input_text"),
                        "Build the reviewed thing.",
                    )
                    self.assertIn(
                        [
                            "--expected-historical-result-sha256",
                            "b" * 64,
                        ],
                        [
                            list(arguments[index : index + 2])
                            for index in range(len(arguments) - 1)
                        ],
                    )
                    self.assertIn(
                        [
                            "--expected-prepared-configuration-sha256",
                            "d" * 64,
                        ],
                        [
                            list(arguments[index : index + 2])
                            for index in range(len(arguments) - 1)
                        ],
                    )
                    run_directory.mkdir()
                    return {
                        "status": "native_task_required",
                        "run_directory": str(run_directory),
                        "task_request": {},
                    }
                raise AssertionError(command)

            config = {
                "thread_id": "thread-1",
                "source_path": "/tmp/transcript.jsonl",
                "message_uuid": "message-1",
                "request": "Build the reviewed thing.",
                "beginning_kind": "git",
                "ending_kind": "git",
                "baseline_commit": "a" * 40,
                "ending_commit": "b" * 40,
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
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "request": "Build something else."})
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run(
                        {**approved, "message_uuid": "different-message"}
                    )
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "ending_commit": "c" * 40})

            self.assertEqual(run_calls, 1)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertIn("[controller] run approved", first["run"]["run_log"])

    def test_prepare_requires_a_historical_result_digest(self) -> None:
        server = load_server()
        with (
            mock.patch.object(
                server,
                "_engine",
                return_value={
                    "status": "ready_for_approval",
                    "blocking_reasons": [],
                    "approval_prompt": "Approve?",
                },
            ),
            self.assertRaisesRegex(
                server.ControllerError,
                "no valid integrity digest",
            ),
        ):
            server._prepare_payload(
                {
                    "thread_id": "thread-1",
                    "model": "gpt-5.6-terra",
                    "timeout_seconds": 1200,
                }
            )

    def test_prepare_requires_a_configuration_digest(self) -> None:
        server = load_server()
        with (
            mock.patch.object(
                server,
                "_engine",
                return_value={
                    "status": "ready_for_approval",
                    "blocking_reasons": [],
                    "approval_prompt": "Approve?",
                    "historical_result_sha256": "a" * 64,
                },
            ),
            self.assertRaisesRegex(
                server.ControllerError,
                "configuration has no valid integrity digest",
            ),
        ):
            server._prepare_payload(
                {
                    "thread_id": "thread-1",
                    "model": "gpt-5.6-terra",
                    "timeout_seconds": 1200,
                }
            )

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
                '"message":"Codex reported an unrecoverable stream error.",'
                '"retryable":true}\n'
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
                server.WorkerError,
                "stream_error: Codex reported an unrecoverable stream error",
            ) as raised:
                server._run_worker(
                    {
                        "model": "gpt-5.6-luna",
                        "prompt": "test",
                        "timeout_seconds": 30,
                    },
                    run_directory=PLUGIN_ROOT,
                    working_directory=PLUGIN_ROOT,
                    read_only=True,
                    log_label="review:test",
                )
        self.assertEqual(raised.exception.code, "stream_error")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(
            run_process.call_args.args[0],
            ["/runtime/node", str(server.WORKER)],
        )

    def test_run_log_records_state_and_streamed_worker_output(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            state = server._initial_state(run_directory)
            server._write_json(server._state_path(run_directory), state)
            server._update_state(
                run_directory,
                phase="implementing",
                summary="The implementation started.",
            )
            log_path = server._run_log_path(run_directory)
            completed_processes = []
            process_errors = []

            def run_worker() -> None:
                try:
                    completed_processes.append(
                        server._run_process(
                            [
                                sys.executable,
                                "-c",
                                (
                                    "import sys, time; "
                                    "print('worker event', flush=True); "
                                    "time.sleep(0.5); "
                                    "print('worker warning', file=sys.stderr)"
                                ),
                            ],
                            cwd=run_directory,
                            timeout=10,
                            stream_log_path=log_path,
                            stream_log_label="implementation",
                        )
                    )
                except Exception as error:  # noqa: BLE001 - surface thread failures.
                    process_errors.append(error)

            worker_thread = threading.Thread(target=run_worker)
            worker_thread.start()
            deadline = time.monotonic() + 3
            observed_while_running = False
            while time.monotonic() < deadline:
                if "worker event" in log_path.read_text(encoding="utf-8"):
                    observed_while_running = worker_thread.is_alive()
                    break
                time.sleep(0.01)
            worker_thread.join(timeout=10)
            content = log_path.read_text(encoding="utf-8")
            log_mode = log_path.stat().st_mode & 0o777

        self.assertEqual(process_errors, [])
        self.assertFalse(worker_thread.is_alive())
        self.assertTrue(observed_while_running)
        self.assertEqual(completed_processes[0].returncode, 0)
        self.assertEqual(state["log_path"], str(log_path))
        self.assertEqual(log_mode, 0o600)
        self.assertIn("[controller] run approved", content)
        self.assertIn("[controller] implementing [running]: The implementation started.", content)
        self.assertIn("[implementation] started", content)
        self.assertIn("[implementation:stdout] worker event", content)
        self.assertIn("[implementation:stderr] worker warning", content)
        self.assertIn("[implementation] finished with exit code 0", content)

    def test_get_run_returns_bounded_log_tail_from_safe_run_path(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_directory.mkdir()
            server._write_json(
                server._state_path(run_directory),
                {
                    "run_id": "run-1",
                    "status": "running",
                    "log_path": "/tmp/untrusted-log-path",
                },
            )
            log_path = server._run_log_path(run_directory)
            log_path.write_bytes(b"old\n" + b"x" * server.MAX_RUN_LOG_BYTES + b"\nlatest\n")

            with mock.patch.object(server, "RUN_ROOT", run_root):
                result = server._call_tool(
                    {"name": "get_run", "arguments": {"run_id": "run-1"}}
                )

        run = result["structuredContent"]["run"]
        self.assertLessEqual(len(run["run_log"].encode("utf-8")), server.MAX_RUN_LOG_BYTES)
        self.assertTrue(run["run_log"].endswith("\nlatest\n"))
        self.assertNotIn("untrusted-log-path", run["run_log"])

    def test_cancel_run_stops_only_its_process_and_records_terminal_state(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_directory.mkdir()
            state = server._initial_state(run_directory)
            server._write_json(server._state_path(run_directory), state)
            process = mock.Mock()
            cancellation = threading.Event()
            server._run_cancellations["run-1"] = cancellation
            server._run_processes["run-1"] = {process}

            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_terminate_process_group") as terminate,
            ):
                cancelled = server._cancel_run({"run_id": "run-1"})
                repeated = server._cancel_run({"run_id": "run-1"})

            persisted = server._read_json(server._state_path(run_directory))
            with self.assertRaises(server.RunCancelled):
                server._update_state(run_directory, phase="implementing")

        self.assertEqual(cancelled["run"]["status"], "cancelled")
        self.assertFalse(cancelled["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertTrue(cancellation.is_set())
        terminate.assert_called_once_with(process)
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["cancellation_reason"], "user_requested")
        self.assertEqual(persisted["error"], "Cancelled by user.")

    def test_get_state_exposes_local_log_paths(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            controller_log = root / "controller-server.log"
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "CONTROLLER_LOG_PATH", controller_log),
                mock.patch.object(
                    server,
                    "_engine",
                    return_value={"options": []},
                ),
            ):
                result = server._call_tool({"name": "get_state", "arguments": {}})

        state = result["structuredContent"]["state"]
        self.assertEqual(state["run_root"], str(run_root))
        self.assertEqual(state["controller_log_path"], str(controller_log))

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

            def fake_worker(
                request,
                *,
                run_directory,
                working_directory,
                read_only,
                log_label,
            ):
                self.assertTrue(read_only)
                self.assertEqual(run_directory, Path(temporary).resolve())
                self.assertEqual(log_label, "review:codex")
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

            def fake_worker(
                request,
                *,
                run_directory,
                working_directory,
                read_only,
                log_label,
            ):
                self.assertEqual(run_directory, Path(temporary).resolve())
                self.assertEqual(log_label, "normalization:codex")
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

    def test_implementation_retries_three_times_in_fresh_workspaces(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            server._write_json(
                server._state_path(run_directory),
                server._initial_state(run_directory),
            )
            calls = []

            def run_worker(*args, **kwargs):
                calls.append(kwargs)
                workspace = kwargs["working_directory"]
                self.assertEqual(list(workspace.iterdir()), [])
                if len(calls) <= server.IMPLEMENTATION_RETRY_LIMIT:
                    (workspace / f"partial-{len(calls)}.txt").write_text(
                        "partial",
                        encoding="utf-8",
                    )
                    raise server.WorkerError(
                        "stream_error",
                        "stream_error: Codex reported an unrecoverable stream error.",
                        retryable=True,
                    )
                self.assertFalse((workspace / "partial.txt").exists())
                return {"thread_id": "retry-thread", "events": []}

            with mock.patch.object(server, "_run_worker", side_effect=run_worker):
                workspace, worker = server._run_implementation(
                    run_directory,
                    {"model": "gpt-5.6-sol", "prompt": "test"},
                    {"type": "projectless"},
                )

            log = server._run_log_path(run_directory).read_text(encoding="utf-8")
            self.assertEqual(worker["thread_id"], "retry-thread")
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[0]["log_label"], "implementation")
            self.assertEqual(calls[1]["log_label"], "implementation:retry-1")
            self.assertEqual(calls[2]["log_label"], "implementation:retry-2")
            self.assertEqual(calls[3]["log_label"], "implementation:retry-3")
            for attempt in range(1, 4):
                archived = (
                    run_directory / "workspaces" / f"codex-attempt-{attempt}-failed"
                )
                self.assertTrue((archived / f"partial-{attempt}.txt").is_file())
            self.assertEqual(workspace.name, "codex")
            self.assertIn("starting retry 1 of 3", log)
            self.assertIn("starting retry 2 of 3", log)
            self.assertIn("starting retry 3 of 3", log)

    def test_packaged_worker_retries_transient_failures_three_times(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_directory = root / "run"
            run_directory.mkdir()
            server._write_json(
                server._state_path(run_directory),
                server._initial_state(run_directory),
            )
            counter = root / "attempt-count"
            fake_codex = root / "fake-codex.py"
            fake_codex.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import json, os, sys",
                        "from pathlib import Path",
                        "sys.stdin.read()",
                        "args = sys.argv[1:]",
                        "workspace = Path(args[args.index('--cd') + 1])",
                        "counter = Path(os.environ['FAKE_RETRY_COUNTER'])",
                        "attempt = int(counter.read_text() or '0') + 1 if counter.exists() else 1",
                        "counter.write_text(str(attempt))",
                        "print(json.dumps({'type': 'thread.started', 'thread_id': f'fixture-thread-{attempt}'}))",
                        "print(json.dumps({'type': 'turn.started'}))",
                        "if attempt <= 3:",
                        "    assert not list(workspace.iterdir())",
                        "    (workspace / f'partial-{attempt}.txt').write_text('partial')",
                        "    print(json.dumps({'type': 'error', 'message': 'connection reset by peer'}))",
                        "else:",
                        "    assert not list(workspace.iterdir())",
                        "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'completed after retries'}}))",
                        "    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1, 'reasoning_output_tokens': 0}}))",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_CLI_PATH": str(fake_codex),
                    "FAKE_RETRY_COUNTER": str(counter),
                },
            ):
                workspace, worker = server._run_implementation(
                    run_directory,
                    {
                        "model": "gpt-5.6-sol",
                        "prompt": "test process-level retry",
                        "timeout_seconds": 30,
                    },
                    {"type": "projectless"},
                )

            log = server._run_log_path(run_directory).read_text(encoding="utf-8")
            self.assertEqual(counter.read_text(encoding="utf-8"), "4")
            self.assertEqual(worker["thread_id"], "fixture-thread-4")
            self.assertEqual(workspace.name, "codex")
            for attempt in range(1, 4):
                archived = (
                    run_directory / "workspaces" / f"codex-attempt-{attempt}-failed"
                )
                self.assertTrue((archived / f"partial-{attempt}.txt").is_file())
            self.assertEqual(log.count("starting retry"), 3)
            self.assertIn("[implementation:retry-3:stdout]", log)
            self.assertEqual(log.count("connection reset by peer"), 3)

    def test_implementation_stops_after_three_retries(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            server._write_json(
                server._state_path(run_directory),
                server._initial_state(run_directory),
            )
            failure = server.WorkerError(
                "stream_error",
                "stream_error: Codex reported an unrecoverable stream error.",
                retryable=True,
            )
            with (
                mock.patch.object(server, "_run_worker", side_effect=failure) as run_worker,
                self.assertRaises(server.WorkerError),
            ):
                server._run_implementation(
                    run_directory,
                    {"model": "gpt-5.6-sol", "prompt": "test"},
                    {"type": "projectless"},
                )

            self.assertEqual(run_worker.call_count, 4)
            for attempt in range(1, 4):
                self.assertTrue(
                    (
                        run_directory
                        / "workspaces"
                        / f"codex-attempt-{attempt}-failed"
                    ).is_dir()
                )
            self.assertTrue((run_directory / "workspaces" / "codex").is_dir())

    def test_implementation_does_not_retry_non_retryable_failure(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            server._write_json(
                server._state_path(run_directory),
                server._initial_state(run_directory),
            )
            failure = server.WorkerError(
                "stream_error",
                "stream_error: Codex reported an unrecoverable stream error.",
            )
            with (
                mock.patch.object(server, "_run_worker", side_effect=failure) as run_worker,
                self.assertRaises(server.WorkerError),
            ):
                server._run_implementation(
                    run_directory,
                    {"model": "gpt-5.6-sol", "prompt": "test"},
                    {"type": "projectless"},
                )

            archived = run_directory / "workspaces" / "codex-attempt-1-failed"
            self.assertEqual(run_worker.call_count, 1)
            self.assertFalse(archived.exists())


if __name__ == "__main__":
    unittest.main()
