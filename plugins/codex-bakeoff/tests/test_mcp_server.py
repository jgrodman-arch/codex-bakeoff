"""Local browser controller, durable execution, and replay-engine tests."""

from __future__ import annotations

import ast
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import re
import signal
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
PUBLIC_TOOL_NAMES = ("open_controller",)


def load_server():
    spec = importlib.util.spec_from_file_location("replay_mcp_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the MCP server.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def controller_request(
    port: int,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            method,
            path,
            body=json.dumps(payload) if payload is not None else None,
            headers=headers or {},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        if not isinstance(body, dict):
            raise AssertionError("The controller returned a non-object JSON response.")
        return response.status, body
    finally:
        connection.close()


_CONTROLLER_HARNESS = r"""
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const extract = (start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  if (first < 0 || last <= first) throw new Error(`Missing controller code: ${start}`);
  return source.slice(first, last);
};
"""


def _render_evaluation_table(evaluation: dict[str, object]) -> str:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const render = new Function([
  extract("      const isObject =", "      const safeJson ="),
  extract("      const titleCase =", "      const formatDate ="),
  extract("      function evaluationTable(evaluation)", "      function renderResultsStep()"),
  "return evaluationTable;",
].join("\n"))();
process.stdout.write(render(JSON.parse(process.argv[2])));
"""
    )
    result = subprocess.run(
        ["node", "-e", harness, str(CONTROLLER_PATH), json.dumps(evaluation)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout


class McpServerTests(unittest.TestCase):
    def test_python_39_is_supported_and_38_is_reported(self) -> None:
        server = load_server()
        self.assertIsNone(server._python_runtime_issue((3, 9, 0)))
        issue = server._python_runtime_issue((3, 8, 20))
        self.assertEqual(issue["dependency"], "python")
        self.assertEqual(issue["status"], "unsupported")
        self.assertEqual(issue["detected_version"], "3.8.20")
        self.assertEqual(issue["required_version"], "3.9+")

    def test_controller_opener_reports_unsupported_python_before_starting(self) -> None:
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

    def test_only_controller_opener_is_model_visible(self) -> None:
        server = load_server()
        tools = server.tool_definitions()
        self.assertEqual(tuple(item["name"] for item in tools), PUBLIC_TOOL_NAMES)
        (opener,) = tools
        self.assertIn("codex_cli_path", opener["inputSchema"]["properties"])
        self.assertIn("in-app browser", opener["description"])
        self.assertIn("external browser fallback", opener["description"])
        self.assertTrue(opener["annotations"]["readOnlyHint"])
        for tool in tools:
            with self.subTest(name=tool["name"]):
                self.assertNotIn("ui", tool.get("_meta", {}))
                self.assertNotIn("ui/resourceUri", tool.get("_meta", {}))
                self.assertEqual(tool["execution"]["taskSupport"], "forbidden")

    def test_public_tool_schemas_use_json_compatible_patterns(self) -> None:
        server = load_server()

        def check_schema(schema, path):
            if isinstance(schema, dict):
                pattern = schema.get("pattern")
                if pattern is not None:
                    with self.subTest(path=path, pattern=pattern):
                        self.assertIsInstance(pattern, str)
                        self.assertNotIn(r"\A", pattern)
                        self.assertNotIn(r"\Z", pattern)
                        self.assertNotIn(r"\z", pattern)
                        re.compile(pattern)
                for name, value in schema.items():
                    check_schema(value, f"{path}.{name}")
            elif isinstance(schema, list):
                for index, value in enumerate(schema):
                    check_schema(value, f"{path}[{index}]")

        for tool in server.tool_definitions():
            check_schema(tool["inputSchema"], tool["name"])

    def test_configuration_schemas_accept_non_git_commit_placeholders(self) -> None:
        server = load_server()

        def permits(schema, value):
            if "anyOf" in schema:
                return any(permits(option, value) for option in schema["anyOf"])
            if "oneOf" in schema:
                return sum(permits(option, value) for option in schema["oneOf"]) == 1
            allowed = schema.get("type")
            allowed_types = allowed if isinstance(allowed, list) else [allowed]
            if value is None:
                return "null" in allowed_types
            return (
                "string" in allowed_types
                and isinstance(value, str)
                and len(value) >= schema.get("minLength", 0)
            )

        for name, value, approval in (
            ("prepare_run", "", False),
            ("start_run", None, True),
        ):
            properties = server._configuration_schema(approval=approval)["properties"]
            for field in ("baseline_commit", "ending_commit"):
                with self.subTest(tool=name, field=field, value=value):
                    self.assertTrue(
                        permits(properties[field], value),
                        f"{name}.{field} rejects the valid Non-Git placeholder {value!r}",
                    )
        normalized = server._configuration_schema(approval=True)["properties"]
        for field in (
            "repo",
            "source_path",
            "message_uuid",
            "request",
            "beginning_kind",
            "ending_kind",
        ):
            with self.subTest(tool="start_run", field=field, value=None):
                self.assertTrue(permits(normalized[field], None))

    def test_stdio_handshake_lists_controller_without_embedded_resources(self) -> None:
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
        initialization = responses[0]["result"]
        self.assertEqual(initialization["serverInfo"]["name"], "codex-bakeoff")
        self.assertEqual(
            tuple(item["name"] for item in responses[1]["result"]["tools"]),
            PUBLIC_TOOL_NAMES,
        )
        self.assertEqual(responses[2]["result"]["resources"], [])

    def test_stdio_cannot_call_private_browser_actions(self) -> None:
        server = load_server()
        for name in server.HTTP_TOOL_NAMES:
            with self.subTest(name=name), mock.patch.object(server, "_call_tool") as call_tool:
                result, error = server._handle_request(
                    "tools/call",
                    {"name": name, "arguments": {}},
                )

            call_tool.assert_not_called()
            self.assertIsNone(error)
            self.assertTrue(result["isError"])
            self.assertIn(name, result["content"][0]["text"])

    def test_resource_read_is_not_supported(self) -> None:
        server = load_server()
        result, error = server._handle_request(
            "resources/read",
            {"uri": "ui://codex-bakeoff/not-the-controller.html"},
        )
        self.assertIsNone(result)
        self.assertEqual(error["code"], -32601)

    def test_controller_opener_returns_direct_local_url_without_opening(self) -> None:
        server = load_server()
        launch_url = "http://127.0.0.1:43117/"
        with (
            mock.patch.object(
                server,
                "_ensure_controller_daemon",
                return_value=(43117, {"version": "test-version"}),
            ),
            mock.patch.object(server, "_run_controller_smoke_test"),
        ):
            result = server._call_tool({"name": "open_controller", "arguments": {}})

        structured = result["structuredContent"]
        self.assertTrue(structured["prepared"])
        self.assertFalse(structured["opened"])
        self.assertEqual(structured["launch_url"], launch_url)
        self.assertEqual(structured["origin"], "http://127.0.0.1:43117")
        self.assertEqual(structured["controller_version"], "test-version")
        self.assertNotIn("token=", structured["launch_url"])
        self.assertNotIn("import webbrowser", SERVER_PATH.read_text(encoding="utf-8"))

    def test_controller_opener_carries_invoking_task_codex_path_to_worker(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_directory = root / "bin"
            lib_directory = root / "lib"
            bin_directory.mkdir()
            lib_directory.mkdir()
            target = lib_directory / "codex.js"
            target.touch()
            target.chmod(0o700)
            codex = bin_directory / "codex"
            codex.symlink_to(target)
            hint_path = root / "codex-cli-path.json"
            with (
                mock.patch.object(server, "REPLAY_CACHE_ROOT", root),
                mock.patch.object(server, "CODEX_CLI_PATH_HINT_PATH", hint_path),
                mock.patch.object(
                    server,
                    "_ensure_controller_daemon",
                    return_value=(43117, {"version": "test-version"}),
                ) as ensure,
                mock.patch.object(server, "_run_controller_smoke_test"),
            ):
                result = server._call_tool(
                    {
                        "name": "open_controller",
                        "arguments": {"codex_cli_path": str(codex)},
                    }
                )
                with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
                    worker_environment = server._worker_environment()

            self.assertTrue(result["structuredContent"]["prepared"])
            self.assertFalse(result["structuredContent"]["opened"])
            ensure.assert_called_once_with(codex_cli_path=str(codex))
            self.assertEqual(worker_environment["CODEX_CLI_PATH"], str(codex))
            self.assertEqual(worker_environment["PATH"].split(os.pathsep)[0], str(bin_directory))
            self.assertEqual(json.loads(hint_path.read_text(encoding="utf-8"))["path"], str(codex))

    def test_mcp_server_uses_http_without_launching_external_browser(self) -> None:
        source = SERVER_PATH.read_text(encoding="utf-8")
        self.assertIn("import http.server", source)
        self.assertNotIn("import webbrowser", source)
        self.assertIn("/api/call", source)
        self.assertIn("/api/download", source)
        self.assertIn('"--http"', source)
        self.assertNotIn("ui://codex-bakeoff", source)

    def test_replay_uses_its_own_configurable_controller_port(self) -> None:
        server = load_server()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._controller_port(), 43118)
        with mock.patch.dict(os.environ, {"CODEX_BAKEOFF_CONTROLLER_PORT": "43219"}, clear=True):
            self.assertEqual(server._controller_port(), 43219)

    def test_controller_idle_timeout_defaults_to_one_hour_and_remains_configurable(self) -> None:
        server = load_server()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._controller_idle_timeout_seconds(), 3_600)
        with mock.patch.dict(
            os.environ,
            {"CODEX_BAKEOFF_CONTROLLER_IDLE_TIMEOUT_SECONDS": "0.5"},
            clear=True,
        ):
            self.assertEqual(server._controller_idle_timeout_seconds(), 0.5)

    def test_each_controller_open_prepares_an_independent_session(self) -> None:
        server = load_server()
        with (
            mock.patch.object(
                server,
                "_ensure_controller_daemon",
                side_effect=(
                    (43118, {"version": "test-version", "controller_session_id": "first"}),
                    (43119, {"version": "test-version", "controller_session_id": "second"}),
                ),
            ) as ensure,
            mock.patch.object(server, "_run_controller_smoke_test"),
            mock.patch.object(server.os, "kill") as kill,
        ):
            first = server._call_tool({"name": "open_controller", "arguments": {}})
            second = server._call_tool({"name": "open_controller", "arguments": {}})

        kill.assert_not_called()
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(first["structuredContent"]["launch_url"], "http://127.0.0.1:43118/")
        self.assertEqual(second["structuredContent"]["launch_url"], "http://127.0.0.1:43119/")
        self.assertEqual(first["structuredContent"]["controller_session_id"], "first")
        self.assertEqual(second["structuredContent"]["controller_session_id"], "second")
        self.assertNotIn("requires_confirmation", first["structuredContent"])

    def test_controller_probe_rejects_a_stale_runtime_token(self) -> None:
        server = load_server()
        payload = {
            "server": server.SERVER_NAME,
            "protocol_version": server.CONTROLLER_PROTOCOL_VERSION,
            "version": server.SERVER_VERSION,
            "proof": "proof-from-a-different-controller",
        }
        with (
            mock.patch.object(
                server,
                "_read_controller_runtime",
                return_value={"control_token": "stale-" + ("x" * 48)},
            ),
            mock.patch.object(
                server,
                "_http_request",
                return_value=(200, json.dumps(payload).encode("utf-8")),
            ),
        ):
            status, health = server._probe_controller(43117)

        self.assertEqual(status, "unverified")
        self.assertEqual(health, payload)

    def test_loopback_controller_allows_same_origin_without_browser_authentication(self) -> None:
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
                hmac.new(control_token.encode(), challenge.encode(), hashlib.sha256).hexdigest(),
            )

            status, _, _ = request("GET", "/auth?token=unused")
            self.assertEqual(status, 404)
            status, _, _ = request("POST", "/api/launch", body="{}")
            self.assertEqual(status, 404)

            status, _, body = request(
                "POST",
                "/api/heartbeat",
                body="{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": httpd.origin,
                },
            )
            self.assertEqual(status, 200)
            heartbeat = json.loads(body)
            self.assertTrue(heartbeat["ok"])
            self.assertEqual(heartbeat["controller_session_id"], httpd.controller_session_id)
            self.assertGreater(heartbeat["heartbeat_interval_seconds"], 0)

            status, _, _ = request(
                "POST",
                "/api/heartbeat",
                body="{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://example.com",
                },
            )
            self.assertEqual(status, 403)

            status, _, body = request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"codex-bakeoff.controller-draft.v7", body)
            self.assertNotIn(b"window.openai", body)
            self.assertNotIn(b"controller-session", body)

            result = {
                "content": [{"type": "text", "text": "Ready."}],
                "structuredContent": {"state": {"models": []}},
            }
            with mock.patch.object(server, "_call_tool", return_value=result):
                status, _, body = request(
                    "POST",
                    "/api/call",
                    body=json.dumps({"name": "get_state", "arguments": {}}),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": httpd.origin,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), result)

                status, _, _ = request(
                    "POST",
                    "/api/call",
                    body=json.dumps({"name": "get_state", "arguments": {}}),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://example.com",
                    },
                )
                self.assertEqual(status, 403)

                status, _, _ = request(
                    "POST",
                    "/api/call",
                    body=json.dumps({"name": "get_state", "arguments": {}}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 403)

                status, _, _ = request(
                    "POST",
                    "/api/call",
                    body=json.dumps({"name": "open_controller", "arguments": {}}),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": httpd.origin,
                    },
                )
                self.assertEqual(status, 400)
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def test_same_origin_report_downloads_require_no_session_token(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_directory.mkdir()
            artifacts = {
                "json": (b'{"status":"completed"}\n', "application/json; charset=utf-8"),
                "html": (
                    b"<!doctype html><title>Replay report</title>",
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

                def request(
                    artifact_format: str,
                    *,
                    origin: str | None = None,
                ) -> tuple[int, dict[str, str], bytes]:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    headers = {
                        "Content-Type": "application/json",
                        "Origin": httpd.origin if origin is None else origin,
                    }
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
                            f'attachment; filename="codex-bakeoff-run-1-report.{artifact_format}"',
                        )

                    status, _, _ = request("html", origin="https://example.com")
                    self.assertEqual(status, 403)
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

                runtime_paths = list((root / "controllers").glob("*/controller-server.json"))
                self.assertEqual(len(runtime_paths), 1)
                runtime_path = runtime_paths[0]
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                status, _, _ = request(
                    "POST",
                    "/api/shutdown",
                    body="{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Codex-Replay-Control": runtime["control_token"],
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

    def test_parallel_controllers_skip_occupied_ports_and_isolate_owned_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                preferred_port = int(occupied.getsockname()[1])
                environment = {
                    "CODEX_BAKEOFF_RUN_ROOT": str(run_root),
                    "CODEX_BAKEOFF_CONTROLLER_PORT": str(preferred_port),
                    "CODEX_BAKEOFF_CONTROLLER_IDLE_TIMEOUT_SECONDS": "30",
                }
                with mock.patch.dict(os.environ, environment):
                    server = load_server()
                    runtimes: list[Path] = []
                    run_states: list[Path] = []
                    spawned: list[subprocess.Popen[bytes]] = []
                    original_spawn = server._spawn_controller_daemon

                    def record_spawn(
                        *,
                        reservation: socket.socket,
                        controller_session_id: str,
                        codex_cli_path: str | None = None,
                    ) -> subprocess.Popen[bytes]:
                        process = original_spawn(
                            reservation=reservation,
                            controller_session_id=controller_session_id,
                            codex_cli_path=codex_cli_path,
                        )
                        spawned.append(process)
                        return process

                    def create_owned_run(run_id: str, controller_session_id: str) -> None:
                        run_directory = run_root / run_id
                        run_directory.mkdir()
                        state = server._initial_state(run_directory)
                        state["controller_session_id"] = controller_session_id
                        state["coordinator_pid"] = os.getpid()
                        state_path = server._state_path(run_directory)
                        server._write_json(state_path, state)
                        run_states.append(state_path)

                    try:
                        with mock.patch.object(
                            server,
                            "_spawn_controller_daemon",
                            side_effect=record_spawn,
                        ):
                            first_port, first_health = server._ensure_controller_daemon()
                            first_session = str(first_health["controller_session_id"])
                            runtimes.append(
                                root / "controllers" / first_session / "controller-server.json"
                            )
                            create_owned_run("run-first", first_session)
                            second_port, second_health = server._ensure_controller_daemon()
                            second_session = str(second_health["controller_session_id"])
                            runtimes.append(
                                root / "controllers" / second_session / "controller-server.json"
                            )
                            create_owned_run("run-second", second_session)
                        ports = (first_port, second_port)
                        sessions = (first_session, second_session)

                        self.assertNotEqual(first_port, second_port)
                        self.assertNotIn(preferred_port, ports)
                        self.assertNotEqual(*sessions)
                        self.assertTrue(all(path.is_file() for path in runtimes))

                        for port, session, expected_run in zip(
                            ports,
                            sessions,
                            ("run-first", "run-second"),
                        ):
                            status, health = controller_request(port, "GET", "/health")
                            self.assertEqual(status, 200)
                            self.assertEqual(health["controller_session_id"], session)
                            self.assertEqual(health["active_runs"], 1)

                            status, result = controller_request(
                                port,
                                "POST",
                                "/api/call",
                                payload={"name": "get_state", "arguments": {}},
                                headers={
                                    "Content-Type": "application/json",
                                    "Origin": f"http://127.0.0.1:{port}",
                                },
                            )
                            self.assertEqual(status, 200)
                            state = result["structuredContent"]["state"]
                            self.assertEqual(state["controller_session_id"], session)
                            self.assertEqual(
                                [run["run_id"] for run in state["recent_runs"]],
                                [expected_run],
                            )

                        status, denied = controller_request(
                            first_port,
                            "POST",
                            "/api/call",
                            payload={
                                "name": "cancel_run",
                                "arguments": {"run_id": "run-second"},
                            },
                            headers={
                                "Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{first_port}",
                            },
                        )
                        self.assertEqual(status, 400)
                        self.assertTrue(denied["isError"])
                        self.assertEqual(
                            json.loads(run_states[1].read_text(encoding="utf-8"))["status"],
                            "running",
                        )
                    finally:
                        for state_path in run_states:
                            state = json.loads(state_path.read_text(encoding="utf-8"))
                            state["status"] = "completed"
                            server._write_json(state_path, state)
                        for runtime_path in runtimes:
                            if not runtime_path.is_file():
                                continue
                            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                            status, _ = controller_request(
                                int(runtime["port"]),
                                "POST",
                                "/api/shutdown",
                                payload={},
                                headers={
                                    "Content-Type": "application/json",
                                    "X-Codex-Replay-Control": str(runtime["control_token"]),
                                },
                            )
                            self.assertEqual(status, 200)
                        for process in spawned:
                            self.assertEqual(process.wait(timeout=5), 0)

    def test_browser_heartbeat_delays_idle_controller_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            session = "b" * 32
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = int(reservation.getsockname()[1])
            environment = {
                **os.environ,
                "CODEX_BAKEOFF_RUN_ROOT": str(root / "runs"),
                "CODEX_BAKEOFF_CONTROLLER_PORT": str(port),
                "CODEX_BAKEOFF_CONTROLLER_SESSION_ID": session,
                "CODEX_BAKEOFF_CONTROLLER_IDLE_TIMEOUT_SECONDS": "0.8",
            }
            process = subprocess.Popen(
                [sys.executable, str(SERVER_PATH), "--http"],
                cwd=PLUGIN_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            runtime_path = root / "controllers" / session / "controller-server.json"
            try:
                deadline = time.monotonic() + 5
                while not runtime_path.is_file() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(f"Idle controller exited early: {stdout}\n{stderr}")
                    time.sleep(0.02)
                self.assertTrue(runtime_path.is_file())

                time.sleep(0.45)
                status, heartbeat = controller_request(
                    port,
                    "POST",
                    "/api/heartbeat",
                    payload={},
                    headers={
                        "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(heartbeat["controller_session_id"], session)

                time.sleep(0.45)
                self.assertIsNone(process.poll(), "A recent browser heartbeat was ignored.")
                self.assertEqual(process.wait(timeout=4), 0)
                process.communicate()
                self.assertFalse(runtime_path.exists())
                self.assertFalse(runtime_path.parent.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                    process.communicate()

    def test_idle_cleanup_waits_for_its_own_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            run_directory = run_root / "run-active"
            run_directory.mkdir(parents=True)
            session = "c" * 32
            with mock.patch.dict(os.environ, {"CODEX_BAKEOFF_RUN_ROOT": str(run_root)}):
                server = load_server()
                state = server._initial_state(run_directory)
                state["controller_session_id"] = session
                state["coordinator_pid"] = os.getpid()
                state_path = server._state_path(run_directory)
                server._write_json(state_path, state)

            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = int(reservation.getsockname()[1])
            process = subprocess.Popen(
                [sys.executable, str(SERVER_PATH), "--http"],
                cwd=PLUGIN_ROOT,
                env={
                    **os.environ,
                    "CODEX_BAKEOFF_RUN_ROOT": str(run_root),
                    "CODEX_BAKEOFF_CONTROLLER_PORT": str(port),
                    "CODEX_BAKEOFF_CONTROLLER_SESSION_ID": session,
                    "CODEX_BAKEOFF_CONTROLLER_IDLE_TIMEOUT_SECONDS": "0.35",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            runtime_path = root / "controllers" / session / "controller-server.json"
            try:
                deadline = time.monotonic() + 5
                while not runtime_path.is_file() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(f"Active controller exited early: {stdout}\n{stderr}")
                    time.sleep(0.02)
                self.assertTrue(runtime_path.is_file())

                time.sleep(0.8)
                self.assertIsNone(process.poll(), "An active replay was abandoned.")

                state["status"] = "completed"
                server._write_json(state_path, state)
                completed_at = time.monotonic()
                self.assertEqual(process.wait(timeout=4), 0)
                self.assertGreaterEqual(
                    time.monotonic() - completed_at,
                    0.2,
                    "The controller skipped its post-run idle grace period.",
                )
                process.communicate()
                self.assertFalse(runtime_path.exists())
                self.assertFalse(runtime_path.parent.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                    process.communicate()

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

    def test_controller_uses_direct_http_and_versioned_local_storage_draft(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        snapshot = controller.split("function draftSnapshot()", 1)[1].split(
            "function saveDraft()", 1
        )[0]
        self.assertIn("codex-bakeoff.controller-draft.v7", controller)
        self.assertIn("codex-bakeoff.controller-active-run.v1", controller)
        self.assertIn("controllerStorageKey", controller)
        self.assertIn("controller_session_id", controller)
        self.assertNotIn("codex-bakeoff.controller-session", controller)
        self.assertNotIn("X-Codex-Replay-Session", controller)
        self.assertNotIn("sessionStorage", controller)
        self.assertIn('fetch("/api/call"', controller)
        self.assertIn('fetch("/api/download"', controller)
        self.assertIn('fetch("/api/heartbeat"', controller)
        self.assertIn("startControllerHeartbeat", controller)
        self.assertIn("stopControllerHeartbeat", controller)
        self.assertNotIn("class HostBridge", controller)
        self.assertNotIn("window.openai", controller)
        self.assertNotIn('"ui/initialize"', controller)
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
        self.assertIn('fetch("/api/download"', download_report)
        self.assertIn("URL.createObjectURL", download_report)
        self.assertIn("URL.revokeObjectURL", download_report)
        self.assertIn("function renderDiagnostics()", controller)
        self.assertIn("Current run log", controller)
        self.assertNotIn("Controller log", controller)
        self.assertIn("serverState.run_root", controller)
        self.assertNotIn("controller_log_path", controller)
        for field in (
            "selectedThreadId",
            "selectedThreadNumber",
            "model",
            "selectedModels",
            "timeoutSeconds",
            "classifications",
            "reviewDraft",
            "configurationStep",
            "configurationMaxStep",
        ):
            self.assertIn(field, snapshot)
        for transient in ("preparation", "approvalChecked", "runId", "report"):
            self.assertNotIn(transient, snapshot)
        self.assertIn("delete reviewDraft.request", controller)

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
        self.assertIn(
            "Select one or more models. Selected models run in parallel.",
            configure,
        )
        self.assertNotIn(
            "I reviewed every current Git change and this attribution is complete",
            controller,
        )
        self.assertIn('finalStep ? "approve-configuration"', configure)
        self.assertIn("Checking configuration", configure)
        self.assertIn("Review and confirm", configure)
        self.assertIn("Entire configuration", configure)
        self.assertIn("safeJson(configuration)", configure)
        self.assertIn("Approve with gaps and start", configure)
        self.assertIn('finalStep ? "approve-configuration" : "configuration-next"', configure)
        self.assertIn('data-action="configuration-back"', configure)
        self.assertIn(
            "const nextDisabled = Boolean(state.busy) || replayDetailsLoading()", configure
        )
        self.assertIn('${nextDisabled ? "disabled" : ""}', configure)
        loading_gate = controller.split("function replayDetailsLoading()", 1)[1].split(
            "function configurationStepProblems", 1
        )[0]
        self.assertIn("state.configurationStep === 0", loading_gate)
        self.assertIn('state.promptGeneration === "pending"', loading_gate)
        self.assertIn("state.workingDirectoryLoading", loading_gate)
        next_step = controller.split("async function nextConfigurationStep()", 1)[1].split(
            "function previousConfigurationStep", 1
        )[0]
        self.assertIn("if (replayDetailsLoading()) return", next_step)
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
        self.assertIn("state.workingDirectoryLoading = true", select_thread)
        self.assertIn("void inferWorkingDirectory(id)", select_thread)
        self.assertLess(
            select_thread.index('state.busy = ""'),
            select_thread.index("void inferWorkingDirectory(id)"),
        )
        self.assertLess(
            select_thread.index("state.classifications = Object.create(null)"),
            select_thread.index("render();"),
        )
        navigation = controller.split("function canNavigateTo", 1)[1].split(
            "function renderRail", 1
        )[0]
        self.assertIn("if (state.busy && !state.run) return false", navigation)
        self.assertNotIn('return id === "run"', navigation)

        click_handler = controller.split('app.addEventListener("click"', 1)[1].split(
            'app.addEventListener("input"', 1
        )[0]
        go_step = click_handler.split('if (action === "go-step")', 1)[1]
        self.assertNotIn("pollGeneration", go_step)

        thread_step = controller.split("function renderThreadStep", 1)[1].split(
            "function capabilityRows", 1
        )[0]
        self.assertIn("Inspecting", thread_step)
        self.assertIn('state.busy === "inspection"', thread_step)
        self.assertIn('state.run ? "disabled"', thread_step)
        self.assertIn("Start another replay", thread_step)
        self.assertIn('data-thread-number="${number}"', thread_step)
        self.assertIn('class="thread__number">#${number}', thread_step)

        select_thread = controller.split("async function selectThread", 1)[1].split(
            "async function prepareRun", 1
        )[0]
        self.assertIn("state.selectedThreadNumber =", select_thread)
        self.assertIn('threadStepEyebrow("Configuration")', controller)
        self.assertIn('threadStepEyebrow("Run in progress")', controller)
        self.assertIn('threadStepEyebrow("Comparison complete")', controller)

        synthesis = controller.split("async function synthesizePrompt", 1)[1].split(
            "function resetController", 1
        )[0]
        self.assertIn('callTool("synthesize_request"', synthesis)
        self.assertIn("thread_id: threadIdValue", synthesis)
        self.assertIn("state.promptEditRevision === editRevision", synthesis)
        self.assertIn("text(state.reviewDraft?.request) === fallbackRequest", synthesis)
        working_directory = controller.split("async function inferWorkingDirectory", 1)[1].split(
            "function resetController", 1
        )[0]
        self.assertIn('callTool("infer_working_directory"', working_directory)
        self.assertIn("editRevision !== state.workingDirectoryEditRevision", working_directory)
        self.assertIn("state.reviewDraft.repo = workingDirectory", working_directory)
        self.assertIn("state.workingDirectoryLoading = false", working_directory)
        self.assertIn("Replay working directory", configure)
        self.assertIn("Inferring working directory…", configure)
        self.assertIn("Reconstructing task prompt…", configure)
        self.assertIn('promptLoading ? "" : escapeHtml(draft.request)', configure)
        self.assertNotIn("Replay repository", configure)
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
        self.assertIn("await Promise.all([", initializer)
        self.assertIn('callTool("get_state", {})', initializer)
        self.assertIn(
            'callTool("list_threads", { offset: 0, limit: THREAD_PAGE_SIZE })', initializer
        )
        self.assertNotIn("get_bootstrap", initializer)
        self.assertIn("const THREAD_PAGE_SIZE = 20", controller)
        self.assertIn('data-action="load-more-threads"', controller)

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
        self.assertIn("record.title || record.name", capability_rows)
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
        self.assertIn("review.ballot?.dimensions", controller)
        self.assertIn("decision.candidates.A", controller)
        self.assertIn("decision.candidates.B", controller)
        self.assertIn('codex:"Codex replay"', controller)
        self.assertIn('claude:"Historical Claude"', controller)
        self.assertNotIn("decision.explanation", controller)

    def test_controller_renders_fixed_dimensions_candidate_checks_and_positive_polarity(
        self,
    ) -> None:
        execution = ast.parse(
            (PLUGIN_ROOT / "scripts" / "historical_execution.py").read_text(encoding="utf-8")
        )
        taxonomy = {
            statement.target.id: ast.literal_eval(statement.value)
            for statement in execution.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id
            in {"REVIEW_DIMENSION_CHECKS", "REVIEW_DIMENSION_LABELS", "REVIEW_CHECK_LABELS"}
        }
        checks_by_dimension = taxonomy["REVIEW_DIMENSION_CHECKS"]
        dimensions = {
            identifier: {
                "candidates": {
                    "A": {"checks": dict.fromkeys(checks), "score": None},
                    "B": {"checks": dict.fromkeys(checks), "score": None},
                },
                "winner": "not_applicable",
            }
            for identifier, checks in checks_by_dimension.items()
        }

        def update_dimension(
            identifier: str,
            first: dict[str, int],
            second: dict[str, int],
            first_score: float,
            second_score: float,
            winner: str = "A",
        ) -> None:
            dimension = dimensions[identifier]
            dimension["candidates"]["A"]["checks"].update(first)
            dimension["candidates"]["B"]["checks"].update(second)
            dimension["candidates"]["A"]["score"] = first_score
            dimension["candidates"]["B"]["score"] = second_score
            dimension["winner"] = winner

        update_dimension(
            "reliability",
            {"invalid_inputs": 1, "boundary_conditions": 1, "failure_handling": 0},
            {"invalid_inputs": 0, "boundary_conditions": 0, "failure_handling": 1},
            2 / 3,
            1 / 3,
        )
        dimensions["reliability"]["explanation"] = "SENSITIVE REVIEWER NARRATIVE"
        update_dimension(
            "code_quality",
            {"clear_naming": 1, "readable_structure": 0},
            {"clear_naming": 1, "readable_structure": 0},
            0.5,
            0.5,
            "tie",
        )
        update_dimension(
            "request_fulfillment",
            {"required_behavior": 1, "stated_constraints": 1},
            {"required_behavior": 0, "stated_constraints": 0},
            1,
            0,
        )
        update_dimension(
            "change_scope",
            {"relevant_files": 1, "preserved_behavior": 1},
            {"relevant_files": 0, "preserved_behavior": 0},
            1,
            0,
        )
        update_dimension(
            "safe_operations",
            {"preserved_user_work": 1, "protected_sensitive_data": 1},
            {"preserved_user_work": 0, "protected_sensitive_data": 0},
            1,
            0,
        )
        update_dimension(
            "accurate_reporting",
            {"truthful_summary": 1, "disclosed_limitations": 1},
            {"truthful_summary": 0, "disclosed_limitations": 0},
            1,
            0,
        )
        evaluation = {
            "candidate_mapping": {"A": "codex", "B": "claude"},
            "reviews": [
                {
                    "evaluator": "codex",
                    "model": "gpt-review",
                    "ballot": {"dimensions": dimensions},
                },
                {
                    "evaluator": "claude",
                    "model": "sonnet",
                    "ballot": {"dimensions": dimensions},
                },
            ],
        }

        rendered = _render_evaluation_table(evaluation)

        self.assertIn("<th>Codex replay</th><th>Historical Claude</th>", rendered)
        for identifier, label in taxonomy["REVIEW_DIMENSION_LABELS"].items():
            with self.subTest(dimension=identifier):
                self.assertIn(f"<strong>{label}</strong>", rendered)
        for identifier, label in taxonomy["REVIEW_CHECK_LABELS"].items():
            occurrences = sum(identifier in checks for checks in checks_by_dimension.values())
            with self.subTest(check=identifier):
                self.assertEqual(rendered.count(f">{label}</td>"), occurrences * 2)
        self.assertIn("<td>67%</td><td>33%</td>", rendered)
        self.assertNotIn("66.7%", rendered)
        self.assertEqual(dimensions["reliability"]["candidates"]["A"]["score"], 2 / 3)
        for label in (
            "Invalid inputs handled appropriately",
            "Boundary conditions handled",
            "Required behavior implemented",
            "Stated constraints followed",
            "Only relevant files changed",
            "Existing behavior preserved",
            "Existing user work preserved",
            "Sensitive data protected",
            "Summary is truthful",
            "Limitations disclosed",
        ):
            with self.subTest(check=label):
                self.assertIn(f">{label}</td><td>Pass</td><td>Fail</td>", rendered)
        self.assertIn(
            ">State remains consistent</td><td>N/A</td><td>N/A</td>",
            rendered,
        )
        self.assertIn("<strong>Codex replay</strong>", rendered)
        self.assertIn("<strong>Tie</strong>", rendered)
        self.assertIn("<td>N/A</td><td>N/A</td>", rendered)
        self.assertIn("Pass: satisfied. Fail: failed.", rendered)
        self.assertNotIn("Explanation", rendered)
        self.assertNotIn("SENSITIVE REVIEWER NARRATIVE", rendered)

    def test_controller_renders_codex_reviewer_failures_without_a_claude_reviewer(self) -> None:
        rendered = _render_evaluation_table(
            {
                "candidate_mapping": {"A": "claude", "B": "codex"},
                "reviews": [
                    {
                        "evaluator": "codex",
                        "model": "gpt-review",
                        "status": "failed",
                        "error": "Reviewer unavailable <offline>",
                    },
                    {
                        "evaluator": "codex",
                        "model": "gpt-review",
                        "status": "invalid",
                        "error": "Invalid ballot <details>",
                    },
                ],
            }
        )

        self.assertIn("Reviewer unavailable &lt;offline&gt;", rendered)
        self.assertIn("Invalid ballot &lt;details&gt;", rendered)
        self.assertIn("<strong>Failed</strong>", rendered)
        self.assertIn("<strong>Invalid</strong>", rendered)
        self.assertNotIn("<strong>Skipped</strong>", rendered)
        self.assertIn("Historical Claude", rendered)
        self.assertIn("Codex replay", rendered)
        self.assertNotIn("<offline>", rendered)
        self.assertIn(
            "No validated blind review ballot is available.",
            _render_evaluation_table({}),
        )

    def test_controller_launch_ignores_completed_runs(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        initializer = controller.split("async function initialize()", 1)[1].split(
            "async function selectThread", 1
        )[0]
        self.assertIn('step: "thread"', controller)
        self.assertIn("const active = recent.find", initializer)
        self.assertIn("if (active)", initializer)
        self.assertIn("run.controller_session_id", initializer)
        self.assertIn("state.controllerSessionId", initializer)
        self.assertIn("loadActiveRunId()", initializer)
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

    def test_working_directory_inference_prefers_existing_codex_result(self) -> None:
        server = load_server()
        turns = [{"role": "user", "text": "Work in the nested project."}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorded = root / "recorded"
            inferred = root / "inferred"
            recorded.mkdir()
            inferred.mkdir()

            def fake_engine(command: str, arguments=()):
                if command == "replay":
                    return {
                        "replay": {
                            "project_dir": str(recorded),
                            "project_dirs": [str(recorded), str(inferred)],
                            "historical_changed_files": [str(inferred / "file.py")],
                            "prompt_reconstruction_turns": turns,
                            "prompt_reconstruction_truncated": False,
                        }
                    }
                if command == "models":
                    return {"options": [{"id": server.REQUEST_SYNTHESIS_MODEL}]}
                raise AssertionError(command)

            def fake_worker(request, **kwargs):
                self.assertEqual(request["expected_schema"], server.WORKING_DIRECTORY_SCHEMA)
                self.assertIn(json.dumps(turns, ensure_ascii=False), request["prompt"])
                self.assertEqual(kwargs["log_label"], "working-directory-inference")
                return {"finalResponse": json.dumps({"working_directory": str(inferred)})}

            with (
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(server, "_run_worker", side_effect=fake_worker),
            ):
                result = server._working_directory_payload({"thread_id": "thread-1"})

        self.assertEqual(result["working_directory"], str(inferred.resolve()))
        self.assertEqual(result["source"], "codex")

    def test_working_directory_inference_falls_back_to_existing_cwd(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            recorded = Path(temporary).resolve()

            def fake_engine(command: str, arguments=()):
                if command == "replay":
                    return {
                        "replay": {
                            "project_dir": str(recorded),
                            "prompt_reconstruction_turns": [
                                {"role": "user", "text": "Update the project."}
                            ],
                            "prompt_reconstruction_truncated": False,
                        }
                    }
                if command == "models":
                    return {"options": [{"id": server.REQUEST_SYNTHESIS_MODEL}]}
                raise AssertionError(command)

            with (
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(
                    server,
                    "_run_worker",
                    return_value={
                        "finalResponse": json.dumps(
                            {"working_directory": "/path/that/does/not/exist"}
                        )
                    },
                ),
            ):
                result = server._working_directory_payload({"thread_id": "thread-1"})

        self.assertEqual(result["working_directory"], str(recorded))
        self.assertEqual(result["source"], "cwd")

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
                        "prompt_reconstruction_turns": [{"role": "user", "text": direct_request}],
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

    def test_synthesis_runs_fresh_each_time(self) -> None:
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
                "Add a newly reconstructed simple hello world program.",
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

        self.assertEqual(first["request"], summaries[0])
        self.assertEqual(
            first["request_generation"],
            {
                "method": "llm_synthesis",
                "model": "gpt-5.6-terra",
                "generated_at": first["request_generation"]["generated_at"],
            },
        )
        self.assertEqual(inspected["replay"]["request"], fallback)
        self.assertEqual(inspected["replay"]["request_generation"], {"method": "pending"})
        self.assertEqual(second["request"], summaries[1])
        self.assertEqual(third["request"], summaries[2])
        self.assertEqual(worker.call_count, 3)
        self.assertEqual(model_calls, 4)

    def test_failed_synthesis_keeps_exact_concat(self) -> None:
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
                transcript.write_text("source transcript", encoding="utf-8")

                def fake_engine(command: str, arguments=(), *, transcript_path=transcript):
                    if command == "replay":
                        return {
                            "replay": {
                                "source_path": str(transcript_path),
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
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(
                        server.ControllerError,
                        "Git or Non-Git",
                    ),
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

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_calls = 0
            coordinator = mock.Mock(pid=4321)

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
                        [list(arguments[index : index + 2]) for index in range(len(arguments) - 1)],
                    )
                    self.assertIn(
                        [
                            "--expected-prepared-configuration-sha256",
                            "d" * 64,
                        ],
                        [list(arguments[index : index + 2]) for index in range(len(arguments) - 1)],
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
                mock.patch.object(
                    server,
                    "_spawn_coordinator",
                    return_value=coordinator,
                ) as spawn_coordinator,
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
                    server._start_run({**approved, "message_uuid": "different-message"})
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "ending_commit": "c" * 40})

            self.assertEqual(run_calls, 1)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(first["run"]["coordinator_pid"], coordinator.pid)
            self.assertEqual(second["run"]["coordinator_pid"], coordinator.pid)
            spawn_coordinator.assert_called_once_with(run_directory)
            request = run_directory / server.COORDINATOR_REQUEST_NAME
            self.assertTrue(request.is_file())
            self.assertEqual(request.stat().st_mode & 0o777, 0o600)
            self.assertIn("[controller] run approved", first["run"]["run_log"])

    def test_detached_coordinator_survives_controller_restart_and_can_be_cancelled(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            run_directory = run_root / "run-1"
            run_directory.mkdir(parents=True)
            ready_marker = run_directory / "coordinator-ready"
            stopped_marker = run_directory / "coordinator-stopped"
            fake_coordinator = root / "fake-coordinator.py"
            fake_coordinator.write_text(
                "import os, signal, sys, time\n"
                "from pathlib import Path\n"
                "if sys.argv[1:] != ['--run-coordinator', 'run-1']:\n"
                "    raise SystemExit(2)\n"
                "run = Path(os.environ['CODEX_BAKEOFF_RUN_ROOT']) / 'run-1'\n"
                "def stop(signum, frame):\n"
                "    (run / 'coordinator-stopped').write_text(str(signum))\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "(run / 'coordinator-ready').write_text(str(os.getpid()))\n"
                "deadline = time.monotonic() + 15\n"
                "while time.monotonic() < deadline:\n"
                "    time.sleep(0.02)\n",
                encoding="utf-8",
            )
            launcher = "\n".join(
                [
                    "import importlib.util",
                    "from pathlib import Path",
                    f"spec = importlib.util.spec_from_file_location('replay_parent', {str(SERVER_PATH)!r})",
                    "module = importlib.util.module_from_spec(spec)",
                    "spec.loader.exec_module(module)",
                    f"module.__file__ = {str(fake_coordinator)!r}",
                    f"process = module._spawn_coordinator(Path({str(run_directory)!r}))",
                    "print(process.pid, flush=True)",
                ]
            )
            environment = {**os.environ, "CODEX_BAKEOFF_RUN_ROOT": str(run_root)}
            parent = subprocess.run(
                [sys.executable, "-c", launcher],
                cwd=PLUGIN_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            coordinator_pid = int(parent.stdout.strip())
            try:
                deadline = time.monotonic() + 5
                while not ready_marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready_marker.is_file(), "Detached coordinator never started.")
                self.assertEqual(int(ready_marker.read_text()), coordinator_pid)
                os.kill(coordinator_pid, 0)

                state = server._initial_state(run_directory)
                state["coordinator_pid"] = coordinator_pid
                state["controller_pid"] = coordinator_pid
                original_controller_session_id = "a" * 32
                replacement_controller_session_id = "b" * 32
                state["controller_session_id"] = original_controller_session_id
                server._write_json(server._state_path(run_directory), state)
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = int(reservation.getsockname()[1])
                restarted = subprocess.Popen(
                    [sys.executable, str(SERVER_PATH), "--http"],
                    cwd=PLUGIN_ROOT,
                    env={
                        **environment,
                        "CODEX_BAKEOFF_CONTROLLER_PORT": str(port),
                        "CODEX_BAKEOFF_CONTROLLER_SESSION_ID": replacement_controller_session_id,
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                def request(
                    method: str,
                    path: str,
                    *,
                    payload: dict[str, object] | None = None,
                    headers: dict[str, str] | None = None,
                ) -> tuple[int, bytes]:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.request(
                            method,
                            path,
                            body=json.dumps(payload) if payload is not None else None,
                            headers=headers or {},
                        )
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()

                try:
                    deadline = time.monotonic() + 5
                    runtime_paths: list[Path] = []
                    while not runtime_paths and time.monotonic() < deadline:
                        if restarted.poll() is not None:
                            stdout, stderr = restarted.communicate()
                            self.fail(f"Restarted controller exited early: {stdout}\n{stderr}")
                        runtime_paths = list(
                            (root / "controllers").glob("*/controller-server.json")
                        )
                        time.sleep(0.02)
                    self.assertEqual(len(runtime_paths), 1)
                    runtime_path = runtime_paths[0]
                    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                    control_headers = {
                        "Content-Type": "application/json",
                        "X-Codex-Replay-Control": runtime["control_token"],
                    }
                    request_headers = {
                        "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{port}",
                    }

                    status, running = request(
                        "POST",
                        "/api/call",
                        payload={"name": "get_run", "arguments": {"run_id": "run-1"}},
                        headers=request_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(running)["structuredContent"]["run"]["status"], "running"
                    )
                    self.assertEqual(
                        json.loads(running)["structuredContent"]["run"]["controller_session_id"],
                        replacement_controller_session_id,
                    )

                    status, cancelled = request(
                        "POST",
                        "/api/call",
                        payload={"name": "cancel_run", "arguments": {"run_id": "run-1"}},
                        headers=request_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(cancelled)["structuredContent"]["run"]["status"], "cancelled"
                    )

                    deadline = time.monotonic() + 5
                    while not stopped_marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(
                        stopped_marker.is_file(), "Cancellation never reached coordinator."
                    )
                    self.assertEqual(int(stopped_marker.read_text()), signal.SIGTERM)

                    status, _ = request(
                        "POST", "/api/shutdown", payload={}, headers=control_headers
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(restarted.wait(timeout=5), 0)
                    restarted.communicate()
                finally:
                    if restarted.poll() is None:
                        restarted.terminate()
                        restarted.wait(timeout=5)
                        restarted.communicate()
            finally:
                try:
                    os.killpg(coordinator_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

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

    def test_only_review_and_normalization_workers_use_medium_reasoning(self) -> None:
        server = load_server()
        cases = (
            ("evaluation", True, "medium"),
            ("review_normalization", True, "medium"),
            ("implementation", False, None),
            ("prompt_synthesis", True, None),
            ("working_directory_inference", True, None),
            ("controller_smoke_test", True, None),
            (None, True, None),
            ("evaluation", False, None),
        )

        for purpose, read_only, expected_effort in cases:
            with self.subTest(purpose=purpose, read_only=read_only):
                request = {"model": "gpt-test", "prompt": "Perform the task."}
                if purpose is not None:
                    request["purpose"] = purpose
                payload = server._worker_request(
                    request,
                    working_directory=PLUGIN_ROOT,
                    read_only=read_only,
                )

                self.assertEqual(payload.get("reasoningEffort"), expected_effort)
                expected_keys = {
                    "type",
                    "requestId",
                    "model",
                    "prompt",
                    "workingDirectory",
                    "timeoutSeconds",
                    "sandboxMode",
                    "networkAccess",
                }
                if expected_effort is not None:
                    expected_keys.add("reasoningEffort")
                self.assertEqual(set(payload), expected_keys)
                self.assertTrue(
                    {
                        "config",
                        "configOverrides",
                        "features",
                        "allowSubagents",
                        "disableSubagents",
                    }.isdisjoint(payload)
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
                    "controller_session_id": server.CONTROLLER_SESSION_ID,
                    "status": "running",
                    "log_path": "/tmp/untrusted-log-path",
                },
            )
            log_path = server._run_log_path(run_directory)
            log_path.write_bytes(b"old\n" + b"x" * server.MAX_RUN_LOG_BYTES + b"\nlatest\n")

            with mock.patch.object(server, "RUN_ROOT", run_root):
                result = server._call_tool({"name": "get_run", "arguments": {"run_id": "run-1"}})

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

    def test_get_state_exposes_run_root_without_http_controller_state(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(
                    server,
                    "_engine",
                    return_value={"options": []},
                ),
            ):
                result = server._call_tool({"name": "get_state", "arguments": {}})

        state = result["structuredContent"]["state"]
        self.assertEqual(state["run_root"], str(run_root))
        self.assertEqual(state["controller_session_id"], server.CONTROLLER_SESSION_ID)
        self.assertNotIn("controller_log_path", state)

    def test_list_threads_returns_only_one_paginated_thread_collection(self) -> None:
        server = load_server()
        threads = [{"imported_thread_id": "thread-21"}]
        with mock.patch.object(
            server,
            "_engine",
            return_value={"sessions": threads, "total": 45, "has_more": True},
        ) as engine:
            result = server._call_tool(
                {"name": "list_threads", "arguments": {"offset": 20, "limit": 20}}
            )

        engine.assert_called_once_with("sessions", ["--limit", "20", "--offset", "20"])
        payload = result["structuredContent"]
        self.assertEqual(payload["threads"], threads)
        self.assertNotIn("sessions", payload)

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

    def test_get_report_download_returns_only_the_requested_artifact(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_directory.mkdir()
            bodies = {
                "json": json.dumps({"winner": "codex", "summary": "Résumé"}, ensure_ascii=False),
                "html": "<!doctype html><title>Replay report</title><p>Résumé</p>",
            }
            for artifact_format, body in bodies.items():
                (run_directory / f"report.{artifact_format}").write_text(body, encoding="utf-8")

            with mock.patch.object(server, "RUN_ROOT", run_root):
                default = server._call_tool(
                    {"name": "get_report", "arguments": {"run_id": "run-1"}}
                )["structuredContent"]
                self.assertEqual(default["report"]["winner"], "codex")
                self.assertNotIn("report_html_content", default)
                self.assertNotIn("artifact_content", default)
                for artifact_format, body in bodies.items():
                    with self.subTest(artifact_format=artifact_format):
                        artifact = server._call_tool(
                            {
                                "name": "get_report",
                                "arguments": {
                                    "run_id": "run-1",
                                    "format": artifact_format,
                                },
                            }
                        )["structuredContent"]
                        self.assertEqual(
                            set(artifact),
                            {
                                "artifact_content",
                                "artifact_format",
                                "artifact_mime_type",
                                "artifact_file_name",
                            },
                        )
                        self.assertEqual(artifact["artifact_content"], body)
                        self.assertEqual(artifact["artifact_format"], artifact_format)
                        self.assertEqual(
                            artifact["artifact_mime_type"],
                            "application/json" if artifact_format == "json" else "text/html",
                        )
                        self.assertEqual(
                            artifact["artifact_file_name"],
                            f"codex-bakeoff-run-1-report.{artifact_format}",
                        )

                result, error = server._handle_request(
                    "tools/call",
                    {
                        "name": "get_report",
                        "arguments": {"run_id": "run-1", "format": "pdf"},
                    },
                )
                self.assertIsNone(error)
                self.assertTrue(result["isError"])

    def test_get_report_rejects_non_string_artifact_formats(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_directory.mkdir()
            (run_directory / "report.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(server, "RUN_ROOT", run_root):
                for invalid_format in (["json"], {"format": "json"}, False, 1):
                    with (
                        self.subTest(invalid_format=invalid_format),
                        self.assertRaisesRegex(
                            server.ControllerError,
                            "Report format must be json or html",
                        ),
                    ):
                        server._call_tool(
                            {
                                "name": "get_report",
                                "arguments": {
                                    "run_id": "run-1",
                                    "format": invalid_format,
                                },
                            }
                        )

    def test_get_report_rejects_artifact_symlinks_outside_run_directory(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            run_directory = run_root / "run-1"
            run_directory.mkdir(parents=True)
            external_json = root / "external.json"
            external_json.write_text('{"secret":"outside the run"}', encoding="utf-8")
            report_json = run_directory / "report.json"
            report_json.symlink_to(external_json)

            with mock.patch.object(server, "RUN_ROOT", run_root):
                for arguments in (
                    {"run_id": "run-1"},
                    {"run_id": "run-1", "format": "json"},
                ):
                    with (
                        self.subTest(arguments=arguments),
                        self.assertRaisesRegex(
                            server.ControllerError,
                            "outside its run directory",
                        ),
                    ):
                        server._call_tool({"name": "get_report", "arguments": arguments})

                report_json.unlink()
                report_json.write_text("{}", encoding="utf-8")
                external_html = root / "external.html"
                external_html.write_text("outside the run", encoding="utf-8")
                (run_directory / "report.html").symlink_to(external_html)
                with self.assertRaisesRegex(server.ControllerError, "outside its run directory"):
                    server._call_tool(
                        {
                            "name": "get_report",
                            "arguments": {"run_id": "run-1", "format": "html"},
                        }
                    )

    def test_startup_reconciliation_tracks_detached_coordinator_pid(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            stale_directory = run_root / "stale"
            live_directory = run_root / "live"
            foreign_directory = run_root / "foreign"
            stale_directory.mkdir()
            live_directory.mkdir()
            foreign_directory.mkdir()
            stale = server._initial_state(stale_directory)
            stale["coordinator_pid"] = 99_999_999
            stale["controller_pid"] = os.getpid()
            live = server._initial_state(live_directory)
            live["coordinator_pid"] = os.getpid()
            live["controller_pid"] = 99_999_999
            foreign = server._initial_state(foreign_directory)
            foreign["controller_session_id"] = "d" * 32
            foreign["coordinator_pid"] = 99_999_999
            server._write_json(server._state_path(stale_directory), stale)
            server._write_json(server._state_path(live_directory), live)
            server._write_json(server._state_path(foreign_directory), foreign)

            with mock.patch.object(server, "RUN_ROOT", run_root):
                server._reconcile_interrupted_runs()

            stale_after = server._read_json(server._state_path(stale_directory))
            live_after = server._read_json(server._state_path(live_directory))
            foreign_after = server._read_json(server._state_path(foreign_directory))
        self.assertEqual(stale_after["status"], "failed")
        self.assertTrue(stale_after["interrupted"])
        self.assertEqual(live_after["status"], "running")
        self.assertEqual(foreign_after["status"], "running")

    def test_orphan_recovery_reconciles_dead_workers_without_claiming_legacy_runs(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve() / "runs"
            run_root.mkdir()
            orphan_directory = run_root / "orphan"
            legacy_directory = run_root / "legacy"
            orphan_directory.mkdir()
            legacy_directory.mkdir()

            orphan = server._initial_state(orphan_directory)
            orphan["controller_session_id"] = "e" * 32
            orphan["coordinator_pid"] = 99_999_999
            server._write_json(server._state_path(orphan_directory), orphan)

            legacy = server._initial_state(legacy_directory)
            legacy.pop("controller_session_id")
            legacy["coordinator_pid"] = 99_999_999
            server._write_json(server._state_path(legacy_directory), legacy)

            with mock.patch.object(server, "RUN_ROOT", run_root):
                server._adopt_orphaned_runs()
                server._reconcile_interrupted_runs()

            orphan_after = server._read_json(server._state_path(orphan_directory))
            legacy_after = server._read_json(server._state_path(legacy_directory))

        self.assertEqual(orphan_after["controller_session_id"], server.CONTROLLER_SESSION_ID)
        self.assertEqual(orphan_after["status"], "failed")
        self.assertTrue(orphan_after["interrupted"])
        self.assertEqual(legacy_after["status"], "running")
        self.assertNotIn("controller_session_id", legacy_after)

    def test_orphan_recovery_never_claims_a_live_unresponsive_controller(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_root = root / "runs"
            run_directory = run_root / "owned"
            run_directory.mkdir(parents=True)
            original_owner = "f" * 32

            state = server._initial_state(run_directory)
            state["controller_session_id"] = original_owner
            state["coordinator_pid"] = os.getpid()
            state_path = server._state_path(run_directory)
            server._write_json(state_path, state)
            server._write_private_json(
                root / "controllers" / original_owner / "controller-server.json",
                {
                    "controller_session_id": original_owner,
                    "pid": os.getpid(),
                    "port": 43219,
                    "started_at": time.time() - 30,
                    "control_token": "x" * 48,
                },
            )

            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(
                    server,
                    "_probe_controller",
                    return_value=("unverified", {}),
                ),
            ):
                server._adopt_orphaned_runs()

            recovered = server._read_json(state_path)

        self.assertEqual(recovered["controller_session_id"], original_owner)
        self.assertEqual(recovered["status"], "running")

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
                payload = server._worker_request(
                    request,
                    working_directory=working_directory,
                    read_only=read_only,
                )
                self.assertEqual(payload["reasoningEffort"], "medium")
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
                            "purpose": "evaluation",
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
                self.assertEqual(log_label, "normalization:codex-for-codex")
                self.assertEqual(list(working_directory.iterdir()), [])
                payload = server._worker_request(
                    request,
                    working_directory=working_directory,
                    read_only=read_only,
                )
                self.assertEqual(payload["reasoningEffort"], "medium")
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
                            "purpose": "review_normalization",
                            "normalization_for": "codex",
                            "model": "gpt-test",
                            "prompt": "Normalize this ballot.",
                        }
                    ],
                    normalization=True,
                )

    def test_coordinator_runs_one_codex_review_without_probing_an_installed_claude(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary).resolve()
            report_path = run_directory / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "winner": "codex",
                        "evaluation": {
                            "candidate_mapping": {"A": "claude", "B": "codex"},
                            "reviews": [{"evaluator": "codex", "model": "gpt-test"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            review_request = {
                "purpose": "evaluation",
                "evaluator": "codex",
                "model": "gpt-test",
                "prompt": "Review both anonymous candidates.",
            }

            def fake_engine(command: str, arguments=(), **kwargs):
                if command == "complete-run":
                    return {}
                if command == "evaluate":
                    self.assertEqual(
                        arguments[:4],
                        ["--run-dir", str(run_directory), "--evaluator", "codex"],
                    )
                    self.assertEqual(arguments[4], "--evaluator-availability-json")
                    availability = json.loads(arguments[5])
                    self.assertEqual(
                        [(item["id"], item["model"]) for item in availability],
                        [("codex", "gpt-test")],
                    )
                    self.assertNotIn("--claude-model", arguments)
                    return {"task_requests": [review_request]}
                if command == "collect-native-results":
                    self.assertEqual(arguments.count("--native-result"), 1)
                    return {"native_results_path": str(run_directory / "reviews.json")}
                if command == "complete-evaluation":
                    return {"status": "completed"}
                if command == "report":
                    return {
                        "report_json": str(report_path),
                        "report_html": str(run_directory / "report.html"),
                    }
                raise AssertionError(command)

            with (
                mock.patch.object(server, "_update_state") as update_state,
                mock.patch.object(
                    server,
                    "_run_implementation",
                    return_value=(
                        run_directory,
                        {"thread_id": "implementation-thread", "events": []},
                    ),
                ),
                mock.patch.object(
                    server,
                    "_collect_result",
                    return_value={"native_result_path": str(run_directory / "native.json")},
                ),
                mock.patch.object(server, "_engine", side_effect=fake_engine) as engine,
                mock.patch.object(
                    server,
                    "_run_review_requests",
                    return_value=[run_directory / "review.json"],
                ) as run_reviews,
                mock.patch.object(server.shutil, "which", return_value="/bin/claude") as lookup,
                mock.patch.object(server, "_run_process") as run_process,
            ):
                server._coordinator(
                    run_directory,
                    {
                        "model": "gpt-test",
                        "prompt": "Implement the task.",
                        "target": {"type": "projectless"},
                    },
                )

            lookup.assert_not_called()
            run_process.assert_not_called()
            run_reviews.assert_called_once_with(run_directory, [review_request])
            commands = [call.args[0] for call in engine.call_args_list]
            self.assertNotIn("discover-checks", commands)
            self.assertNotIn("verify", commands)
            self.assertLess(commands.index("complete-run"), commands.index("evaluate"))
            reviewing = next(
                call.kwargs
                for call in update_state.call_args_list
                if call.kwargs.get("phase") == "reviewing"
            )
            self.assertEqual(reviewing["details"]["selected_evaluators"], ["codex"])
            self.assertEqual(
                [item["id"] for item in reviewing["details"]["evaluator_availability"]],
                ["codex"],
            )
            completed = next(
                call.kwargs
                for call in update_state.call_args_list
                if call.kwargs.get("status") == "completed"
            )
            self.assertEqual(
                completed["details"]["report_summary"]["evaluation"]["candidate_mapping"],
                {
                    "A": "claude",
                    "B": "codex",
                },
            )

    def test_controller_has_no_separate_repository_test_execution(self) -> None:
        server = load_server()
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")

        self.assertNotIn("Approve or reject each test", controller)
        self.assertNotIn("Objective verification", controller)
        self.assertNotIn("decide-verification-test", controller)
        self.assertNotIn("continue-verification", controller)
        self.assertNotIn("awaiting_test_approval", dict(server.PHASES))
        self.assertNotIn("verifying", dict(server.PHASES))

    def test_non_codex_reviewers_and_normalization_targets_are_rejected(self) -> None:
        server = load_server()
        for evaluator, normalization in (("claude", False), ("other", False), ("claude", True)):
            with self.subTest(evaluator=evaluator, normalization=normalization):
                with tempfile.TemporaryDirectory() as temporary:
                    field = "normalization_for" if normalization else "evaluator"
                    with (
                        mock.patch.object(server, "_run_worker") as run_worker,
                        self.assertRaisesRegex(
                            server.ControllerError, "Unsupported review evaluator"
                        ),
                    ):
                        server._run_review_requests(
                            Path(temporary).resolve(),
                            [{field: evaluator, "model": "test", "prompt": "Review"}],
                            normalization=normalization,
                        )
                    run_worker.assert_not_called()

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
                ["git", "-C", str(repository), "config", "user.name", "Codex Bakeoff Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "codex-bakeoff@example.com"],
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
                archived = run_directory / "workspaces" / f"codex-attempt-{attempt}-failed"
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
                        "if sys.argv[1:] == ['--version']:",
                        "    print('codex fixture')",
                        "    raise SystemExit(0)",
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
                archived = run_directory / "workspaces" / f"codex-attempt-{attempt}-failed"
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
                    (run_directory / "workspaces" / f"codex-attempt-{attempt}-failed").is_dir()
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
