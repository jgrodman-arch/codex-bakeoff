#!/usr/bin/env python3
"""MCP launcher and local web controller for Codex Bakeoff."""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MINIMUM_PYTHON = (3, 9)
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PLUGIN_ROOT / "scripts" / "historical_bakeoff.py"
APP_HTML = Path(__file__).resolve().parent / "controller.html"
WORKER = Path(__file__).resolve().parent / "codex-worker.mjs"
DEFAULT_RUN_ROOT = Path.home() / ".cache" / "codex-bakeoff" / "runs"
RUN_ROOT = Path(os.environ.get("CODEX_BAKEOFF_RUN_ROOT", DEFAULT_RUN_ROOT)).expanduser().resolve()
STATE_NAME = "controller-state.json"

SERVER_NAME = "codex-bakeoff"
APP_TITLE = "Codex Bakeoff"
CONTROLLER_HOST = "127.0.0.1"
DEFAULT_CONTROLLER_PORT = 43117
CONTROLLER_PROTOCOL_VERSION = 1
CONTROLLER_CACHE_ROOT = RUN_ROOT.parent
CONTROLLER_RUNTIME_PATH = CONTROLLER_CACHE_ROOT / "controller-server.json"
CONTROLLER_LOCK_PATH = CONTROLLER_CACHE_ROOT / "controller-server.lock"
CONTROLLER_LOG_PATH = CONTROLLER_CACHE_ROOT / "controller-server.log"
CONTROLLER_SESSION_STORAGE_KEY = "codex-bakeoff.controller-session.v1"
CONTROLLER_SESSION_HEADER = "X-Codex-Bakeoff-Session"
MAX_TEXT_BYTES = 32 * 1024
MAX_STATE_BYTES = 512 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 1024 * 1024
MAX_BROWSER_SESSIONS = 256
MAX_SELECTION_ITEMS = 2_000
MAX_PREPARE_TOKENS = 256
MAX_COMMAND_TIMEOUT = 14_700
RUN_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
COMMIT_PATTERN = re.compile(r"\A[0-9a-fA-F]{7,64}\Z")
HTTP_TOOL_NAMES = frozenset(
    {
        "get_state",
        "list_threads",
        "inspect_thread",
        "prepare_run",
        "start_run",
        "get_run",
        "get_report",
    }
)

PHASES = (
    ("preparing", "Preparing configuration"),
    ("creating_workspace", "Creating isolated workspace"),
    ("implementing", "Implementing with Codex"),
    ("collecting", "Capturing result"),
    ("verifying", "Running shared verification"),
    ("reviewing", "Running blind review"),
    ("reporting", "Finalizing report"),
)

_jobs: dict[str, threading.Thread] = {}
_jobs_lock = threading.RLock()
_prepared_runs: dict[str, dict[str, Any]] = {}
_active_processes: set[subprocess.Popen[str]] = set()
_active_processes_lock = threading.RLock()
_pending_port_conflicts: dict[str, dict[str, Any]] = {}
_pending_port_conflicts_lock = threading.Lock()
_shutdown = threading.Event()


class ControllerError(ValueError):
    """A safe user-facing controller error."""


class PortConflictError(ControllerError):
    """A local listener must be confirmed before it can be stopped."""

    def __init__(self, port: int, pid: int, process_name: str) -> None:
        self.port = port
        self.pid = pid
        self.process_name = process_name
        super().__init__(
            f"Port {port} is already in use by {process_name} (PID {pid})."
        )


def _python_runtime_issue(version_info: Any = None) -> dict[str, Any] | None:
    detected = sys.version_info if version_info is None else version_info
    version = tuple(int(part) for part in detected[:3])
    if version[:2] >= MINIMUM_PYTHON:
        return None
    detected_version = ".".join(str(part) for part in version)
    required_version = ".".join(str(part) for part in MINIMUM_PYTHON) + "+"
    return {
        "kind": "dependency",
        "dependency": "python",
        "status": "unsupported",
        "detected_version": detected_version,
        "required_version": required_version,
        "executable": sys.executable,
        "message": (
            f"Codex Bakeoff requires Python {required_version}; "
            f"{detected_version} is running from {sys.executable}."
        ),
    }


def _python_runtime_error_result() -> dict[str, Any] | None:
    issue = _python_runtime_issue()
    if issue is None:
        return None
    result = _text_result(str(issue["message"]), {"opened": False, "issue": issue})
    result["isError"] = True
    return result


def _node_runtime() -> str:
    configured = os.environ.get("CODEX_MCP_NODE_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    executable = shutil.which("node")
    if executable is not None:
        return executable
    raise ControllerError(
        "Node.js 18 or newer could not be found. Install Node.js or set "
        "CODEX_MCP_NODE_PATH to an executable Node runtime."
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _installed_version() -> str:
    try:
        payload = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return "0.0.0"
    version = payload.get("version") if isinstance(payload, Mapping) else None
    return str(version or "0.0.0")


SERVER_VERSION = _installed_version()


def _object_schema(
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _string_array(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "maxItems": MAX_SELECTION_ITEMS,
        "description": description,
    }


def _configuration_schema(*, approval: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "thread_id": {"type": "string", "minLength": 1},
        "imported_thread_id": {"type": "string", "minLength": 1},
        "model": {"type": "string", "minLength": 1},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 14_400},
        "repo": {"type": "string", "minLength": 1},
        "claude_output_files": _string_array("Git working-tree changes attributed to Claude."),
        "created_by_claude": _string_array("Non-Git files created by Claude."),
        "excluded_files": _string_array("Non-Git files deliberately excluded."),
        "confirm_file_selection": {"type": "boolean"},
    }
    if approval:
        properties["approved"] = {"type": "boolean", "const": True}
        properties["prepare_token"] = {
            "type": "string",
            "minLength": 32,
            "description": "Opaque token returned by prepare_run for this exact configuration.",
        }
    return _object_schema(
        properties,
        ["model", *(["approved", "prepare_token"] if approval else [])],
    )


def _tool_definition(
    name: str,
    title: str,
    description: str,
    schema: Mapping[str, Any],
    *,
    read_only: bool,
    idempotent: bool | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": dict(schema),
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "idempotentHint": read_only if idempotent is None else idempotent,
            "openWorldHint": not read_only,
        },
        "execution": {"taskSupport": "forbidden"},
        "_meta": {
            "openai/toolInvocation/invoking": f"Opening {APP_TITLE} in your browser…",
            "openai/toolInvocation/invoked": f"{APP_TITLE} opened in your browser.",
        },
    }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        _tool_definition(
            "open_controller",
            f"Open {APP_TITLE}",
            "Open the interactive bakeoff controller in the default external browser.",
            _object_schema({}),
            read_only=True,
            idempotent=False,
        ),
        _tool_definition(
            "stop_port_process_and_open_controller",
            f"Stop port process and open {APP_TITLE}",
            (
                "After the user explicitly confirms, stop the exact process previously "
                "reported as occupying the controller port and open the controller."
            ),
            _object_schema(
                {
                    "confirmed": {"type": "boolean", "const": True},
                    "confirmation_token": {
                        "type": "string",
                        "minLength": 32,
                        "description": "Short-lived token returned by open_controller.",
                    },
                },
                ["confirmed", "confirmation_token"],
            ),
            read_only=False,
            idempotent=False,
        ),
    ]


def _text_result(message: str, structured: Mapping[str, Any] | None = None) -> dict[str, Any]:
    bounded = message.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    result: dict[str, Any] = {"content": [{"type": "text", "text": bounded}]}
    if structured is not None:
        result["structuredContent"] = dict(structured)
    return result


def _argument_object(params: Any) -> dict[str, Any]:
    if not isinstance(params, Mapping):
        return {}
    arguments = params.get("arguments")
    return dict(arguments) if isinstance(arguments, Mapping) else {}


def _controller_port() -> int:
    raw = os.environ.get("CODEX_BAKEOFF_CONTROLLER_PORT", str(DEFAULT_CONTROLLER_PORT))
    try:
        port = int(raw)
    except ValueError as error:
        raise ControllerError("CODEX_BAKEOFF_CONTROLLER_PORT must be an integer.") from error
    if port < 1024 or port > 65_535:
        raise ControllerError("CODEX_BAKEOFF_CONTROLLER_PORT must be from 1024 through 65535.")
    return port


def _controller_origin(port: int) -> str:
    return f"http://{CONTROLLER_HOST}:{port}"


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_controller_runtime() -> dict[str, Any]:
    try:
        if CONTROLLER_RUNTIME_PATH.stat().st_size > 64 * 1024:
            return {}
        payload = json.loads(CONTROLLER_RUNTIME_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _port_accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection((CONTROLLER_HOST, port), timeout=0.25):
            return True
    except OSError:
        return False


def _port_listener(port: int) -> dict[str, Any]:
    executable = shutil.which("lsof")
    if executable is None and Path("/usr/sbin/lsof").is_file():
        executable = "/usr/sbin/lsof"
    if executable is None:
        raise ControllerError(
            f"Port {port} is in use, but the listening process could not be identified safely."
        )
    completed = subprocess.run(
        [
            executable,
            "-nP",
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
            "-Fpc",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    listeners: dict[int, str] = {}
    current_pid: int | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current_pid = int(line[1:])
            listeners.setdefault(current_pid, "unknown process")
        elif line.startswith("c") and current_pid is not None and line[1:].strip():
            listeners[current_pid] = line[1:].strip()
    if len(listeners) != 1:
        raise ControllerError(
            f"Port {port} is in use, but its listening process could not be identified safely."
        )
    pid, process_name = next(iter(listeners.items()))
    if pid == os.getpid():
        raise ControllerError("The controller cannot stop its own MCP server process.")
    return {"port": port, "pid": pid, "process_name": process_name}


def _remember_port_conflict(error: PortConflictError) -> str:
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _pending_port_conflicts_lock:
        expired = [
            key
            for key, value in _pending_port_conflicts.items()
            if float(value.get("expires_at", 0)) <= now
        ]
        for key in expired:
            _pending_port_conflicts.pop(key, None)
        if len(_pending_port_conflicts) >= MAX_PREPARE_TOKENS:
            oldest = min(
                _pending_port_conflicts,
                key=lambda key: float(
                    _pending_port_conflicts[key].get("created_at", 0)
                ),
            )
            _pending_port_conflicts.pop(oldest, None)
        _pending_port_conflicts[token] = {
            "port": error.port,
            "pid": error.pid,
            "process_name": error.process_name,
            "created_at": now,
            "expires_at": now + 600,
        }
    return token


def _consume_port_confirmation(token: str, port: int) -> dict[str, Any]:
    with _pending_port_conflicts_lock:
        conflict = _pending_port_conflicts.pop(token, None)
    if conflict is None or float(conflict.get("expires_at", 0)) <= time.monotonic():
        raise ControllerError(
            "The port-process confirmation expired. Open the controller and confirm again."
        )
    if conflict.get("port") != port:
        raise ControllerError(
            "The configured controller port changed. Open the controller and confirm again."
        )
    return conflict


def _stop_confirmed_port_listener(
    port: int,
    confirmed: Mapping[str, Any],
) -> None:
    listener = _port_listener(port)
    if (
        listener["pid"] != confirmed.get("pid")
        or listener["process_name"] != confirmed.get("process_name")
    ):
        raise PortConflictError(
            port,
            int(listener["pid"]),
            str(listener["process_name"]),
        )
    pid = int(listener["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise ControllerError(
            f"Permission was denied while stopping PID {pid} on port {port}."
        ) from error

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _port_accepts_connections(port):
            return
        time.sleep(0.05)

    listener = _port_listener(port)
    if listener["pid"] != pid:
        raise PortConflictError(
            port,
            int(listener["pid"]),
            str(listener["process_name"]),
        )
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise ControllerError(
            f"Permission was denied while killing PID {pid} on port {port}."
        ) from error

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _port_accepts_connections(port):
            return
        time.sleep(0.05)
    raise ControllerError(f"PID {pid} did not release port {port}.")


def _http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 1.0,
) -> tuple[int | None, bytes]:
    request = urllib.request.Request(
        url,
        data=data,
        headers=dict(headers or {}),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, b""


def _probe_controller(port: int) -> tuple[str, dict[str, Any]]:
    runtime = _read_controller_runtime()
    control_token = runtime.get("control_token")
    challenge = secrets.token_urlsafe(24)
    status, body = _http_request(
        "GET",
        f"{_controller_origin(port)}/health?{urllib.parse.urlencode({'challenge': challenge})}",
    )
    if status is None:
        return ("foreign", {}) if _port_accepts_connections(port) else ("absent", {})
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "foreign", {}
    expected_proof = (
        hmac.new(
            control_token.encode("utf-8"),
            challenge.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if isinstance(control_token, str) and len(control_token) >= 32
        else ""
    )
    supplied_proof = payload.get("proof") if isinstance(payload, Mapping) else None
    if (
        status == 200
        and isinstance(payload, Mapping)
        and payload.get("server") == SERVER_NAME
        and payload.get("protocol_version") == CONTROLLER_PROTOCOL_VERSION
        and isinstance(supplied_proof, str)
        and bool(expected_proof)
        and hmac.compare_digest(supplied_proof, expected_proof)
    ):
        return "compatible", dict(payload)
    return "foreign", dict(payload) if isinstance(payload, Mapping) else {}


def _runtime_control_token(port: int) -> str:
    runtime = _read_controller_runtime()
    token = runtime.get("control_token")
    if runtime.get("port") != port or not isinstance(token, str) or len(token) < 32:
        raise ControllerError(
            "The local controller is running, but its private launch state is unavailable."
        )
    return token


def _control_request(
    port: int,
    path: str,
    *,
    timeout: float = 2.0,
) -> tuple[int | None, dict[str, Any]]:
    token = _runtime_control_token(port)
    status, body = _http_request(
        "POST",
        f"{_controller_origin(port)}{path}",
        headers={
            "Content-Type": "application/json",
            "X-Codex-Bakeoff-Control": token,
        },
        data=b"{}",
        timeout=timeout,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        payload = {}
    return status, dict(payload) if isinstance(payload, Mapping) else {}


def _stop_outdated_controller(port: int) -> bool:
    status, payload = _control_request(port, "/api/shutdown")
    if status == 200:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _probe_controller(port)[0] == "absent":
                return True
            time.sleep(0.05)
        raise ControllerError("The previous local controller did not stop.")
    if status == 409 and payload.get("active_run") is True:
        return False
    raise ControllerError("The previous local controller could not be replaced safely.")


def _spawn_controller_daemon() -> subprocess.Popen[bytes]:
    if not APP_HTML.is_file():
        raise ControllerError("The external controller HTML is unavailable.")
    CONTROLLER_CACHE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONTROLLER_LOG_PATH.touch(mode=0o600, exist_ok=True)
    CONTROLLER_LOG_PATH.chmod(0o600)
    with CONTROLLER_LOG_PATH.open("ab", buffering=0) as log:
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--http"],
            cwd=PLUGIN_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def _ensure_controller_daemon(
    *,
    confirmation_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    port = _controller_port()
    CONTROLLER_CACHE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with CONTROLLER_LOCK_PATH.open("a+", encoding="utf-8") as lock:
        CONTROLLER_LOCK_PATH.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        confirmed_conflict = (
            _consume_port_confirmation(confirmation_token, port)
            if confirmation_token is not None
            else None
        )
        status, health = _probe_controller(port)
        if status == "foreign":
            listener = _port_listener(port)
            if confirmed_conflict is None:
                raise PortConflictError(
                    port,
                    int(listener["pid"]),
                    str(listener["process_name"]),
                )
            _stop_confirmed_port_listener(port, confirmed_conflict)
            status, health = _probe_controller(port)
            if status != "absent":
                raise ControllerError(f"Port {port} was not released.")
        if status == "compatible":
            if health.get("version") == SERVER_VERSION:
                return port, health
            if not _stop_outdated_controller(port):
                return port, health

        process = _spawn_controller_daemon()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, health = _probe_controller(port)
            if status == "compatible":
                return port, health
            if status == "foreign":
                raise ControllerError(
                    f"Port {port} was claimed by another service while the controller started."
                )
            return_code = process.poll()
            if return_code is not None:
                raise ControllerError(
                    "The external controller could not start. "
                    f"See {CONTROLLER_LOG_PATH} for details."
                )
            time.sleep(0.05)
    raise ControllerError("The external controller did not become ready.")


def _request_browser_login(port: int) -> str:
    status, payload = _control_request(port, "/api/launch")
    path = payload.get("path")
    if status != 200 or not isinstance(path, str) or not path.startswith("/auth?token="):
        raise ControllerError("The external controller could not create a browser session.")
    return f"{_controller_origin(port)}{path}"


def _open_external_controller(
    *,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    try:
        port, health = _ensure_controller_daemon(
            confirmation_token=confirmation_token,
        )
    except PortConflictError as error:
        token = _remember_port_conflict(error)
        return _text_result(
            (
                f"Port {error.port} is in use by {error.process_name} "
                f"(PID {error.pid}). Ask the user to confirm whether this exact "
                "process should be stopped."
            ),
            {
                "opened": False,
                "requires_confirmation": True,
                "port": error.port,
                "pid": error.pid,
                "process_name": error.process_name,
                "confirmation_token": token,
            },
        )
    launch_url = _request_browser_login(port)
    if not webbrowser.open(launch_url, new=2, autoraise=True):
        raise ControllerError("No external browser was available to open the controller.")
    return _text_result(
        "The Codex Bakeoff controller opened in your external browser.",
        {
            "opened": True,
            "origin": _controller_origin(port),
            "controller_version": health.get("version"),
        },
    )


class _ControllerHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        control_token: str,
    ) -> None:
        self.control_token = control_token
        self.login_tokens: dict[str, float] = {}
        self.login_lock = threading.Lock()
        self.session_tokens: dict[str, float] = {}
        self.session_lock = threading.Lock()
        super().__init__(server_address, _ControllerHTTPRequestHandler)
        port = int(self.server_address[1])
        self.origin = _controller_origin(port)
        self.expected_host = f"{CONTROLLER_HOST}:{port}"

    def issue_login(self) -> str:
        token = secrets.token_urlsafe(48)
        now = time.monotonic()
        with self.login_lock:
            self.login_tokens = {
                value: expiry for value, expiry in self.login_tokens.items() if expiry > now
            }
            self.login_tokens[token] = now + 60
        return token

    def consume_login(self, token: str) -> bool:
        with self.login_lock:
            expiry = self.login_tokens.pop(token, 0)
        return expiry > time.monotonic()

    def issue_session(self) -> str:
        token = secrets.token_urlsafe(48)
        now = time.monotonic()
        with self.session_lock:
            self.session_tokens = {
                value: expiry for value, expiry in self.session_tokens.items() if expiry > now
            }
            while len(self.session_tokens) >= MAX_BROWSER_SESSIONS:
                self.session_tokens.pop(next(iter(self.session_tokens)))
            self.session_tokens[token] = now + (24 * 60 * 60)
        return token

    def authorize_session(self, token: str) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self.session_lock:
            expiry = self.session_tokens.get(token, 0)
            if expiry <= now:
                self.session_tokens.pop(token, None)
                return False
            self.session_tokens[token] = now + (24 * 60 * 60)
        return True


class _ControllerHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    server: _ControllerHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes = b"",
        *,
        content_type: str = "text/plain; charset=utf-8",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            (
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(
            status,
            body,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == self.server.expected_host

    def _control_authorized(self) -> bool:
        supplied = self.headers.get("X-Codex-Bakeoff-Control", "")
        return bool(supplied) and secrets.compare_digest(
            supplied,
            self.server.control_token,
        )

    def _session_authorized(self) -> bool:
        return self.server.authorize_session(self.headers.get(CONTROLLER_SESSION_HEADER, ""))

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as error:
            raise ControllerError("A valid Content-Length header is required.") from error
        if length < 2 or length > MAX_HTTP_BODY_BYTES:
            raise ControllerError("The controller request body is invalid.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ControllerError("The controller request must contain JSON.") from error
        if not isinstance(payload, Mapping):
            raise ControllerError("The controller request must be an object.")
        return dict(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if not self._valid_host():
            self._send(421, b"Invalid host.")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            challenge = urllib.parse.parse_qs(parsed.query).get("challenge", [""])[0]
            with _jobs_lock:
                active_runs = len(_jobs)
            self._send_json(
                200,
                {
                    "server": SERVER_NAME,
                    "protocol_version": CONTROLLER_PROTOCOL_VERSION,
                    "version": SERVER_VERSION,
                    "pid": os.getpid(),
                    "active_runs": active_runs,
                    "proof": (
                        hmac.new(
                            self.server.control_token.encode("utf-8"),
                            challenge.encode("utf-8"),
                            hashlib.sha256,
                        ).hexdigest()
                        if challenge
                        else ""
                    ),
                },
            )
            return
        if parsed.path == "/auth":
            token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            if not token or not self.server.consume_login(token):
                self._send(401, b"This controller link is invalid or expired.")
                return
            session_token = self.server.issue_session()
            bootstrap = (
                '<!doctype html><meta charset="utf-8"><title>Opening controller</title>'
                "<script>"
                f"localStorage.setItem({json.dumps(CONTROLLER_SESSION_STORAGE_KEY)},"
                f"{json.dumps(session_token)});"
                "location.replace('/');"
                "</script>"
            ).encode("utf-8")
            self._send(
                200,
                bootstrap,
                content_type="text/html; charset=utf-8",
            )
            return
        if parsed.path == "/favicon.ico":
            self._send(204)
            return
        if parsed.path != "/":
            self._send(404, b"Not found.")
            return
        if not APP_HTML.is_file():
            self._send(500, b"The controller HTML is unavailable.")
            return
        self._send(
            200,
            APP_HTML.read_bytes(),
            content_type="text/html; charset=utf-8",
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if not self._valid_host():
            self._send(421, b"Invalid host.")
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/launch":
            if not self._control_authorized():
                self._send_json(401, {"error": "Unauthorized."})
                return
            token = self.server.issue_login()
            self._send_json(200, {"path": f"/auth?token={token}"})
            return
        if path == "/api/shutdown":
            if not self._control_authorized():
                self._send_json(401, {"error": "Unauthorized."})
                return
            with _jobs_lock:
                jobs_active = bool(_jobs)
            with _active_processes_lock:
                processes_active = bool(_active_processes)
            if jobs_active or processes_active:
                self._send_json(
                    409,
                    {"active_run": True, "error": "A bakeoff is still running."},
                )
                return
            self._send_json(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path not in {"/api/call", "/api/download"}:
            self._send_json(404, {"error": "Not found."})
            return
        if not self._session_authorized():
            self._send_json(401, {"error": "Unauthorized."})
            return
        if self.headers.get("Origin") != self.server.origin:
            self._send_json(403, {"error": "Invalid origin."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "Expected application/json."})
            return
        try:
            payload = self._read_json_body()
            if path == "/api/download":
                run_id = payload.get("run_id")
                artifact_format = payload.get("format")
                if not isinstance(run_id, str):
                    raise ControllerError("run_id is required.")
                if artifact_format not in {"json", "html"}:
                    raise ControllerError("format must be json or html.")
                artifact_path = _safe_run_directory(run_id) / f"report.{artifact_format}"
                if not artifact_path.is_file():
                    raise ControllerError("The bakeoff report is not ready.")
                self._send(
                    200,
                    artifact_path.read_bytes(),
                    content_type={
                        "json": "application/json; charset=utf-8",
                        "html": "text/html; charset=utf-8",
                    }[artifact_format],
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="codex-bakeoff-{run_id}-report.'
                            f'{artifact_format}"'
                        )
                    },
                )
                return
            name = payload.get("name")
            arguments = payload.get("arguments")
            if name not in HTTP_TOOL_NAMES:
                raise ControllerError("Unknown controller action.")
            if not isinstance(arguments, Mapping):
                raise ControllerError("Controller action arguments must be an object.")
            result = _call_tool({"name": name, "arguments": dict(arguments)})
        except ControllerError as error:
            self._send_json(
                400,
                {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            )
            return
        except Exception:
            self._send_json(
                500,
                {
                    "content": [
                        {"type": "text", "text": "The local controller encountered an error."}
                    ],
                    "isError": True,
                },
            )
            return
        self._send_json(200, result)


def _remove_runtime_if_owned(control_token: str) -> None:
    runtime = _read_controller_runtime()
    if runtime.get("pid") == os.getpid() and runtime.get("control_token") == control_token:
        try:
            CONTROLLER_RUNTIME_PATH.unlink()
        except FileNotFoundError:
            pass


def run_http() -> int:
    port = _controller_port()
    control_token = secrets.token_urlsafe(48)
    try:
        RUN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        RUN_ROOT.chmod(0o700)
    except OSError as error:
        print(f"Cannot secure the bakeoff run directory: {error}", file=sys.stderr)
        return 1
    try:
        server = _ControllerHTTPServer((CONTROLLER_HOST, port), control_token)
    except OSError as error:
        print(f"Cannot bind the controller to {_controller_origin(port)}: {error}", file=sys.stderr)
        return 1
    _write_private_json(
        CONTROLLER_RUNTIME_PATH,
        {
            "server": SERVER_NAME,
            "protocol_version": CONTROLLER_PROTOCOL_VERSION,
            "version": SERVER_VERSION,
            "pid": os.getpid(),
            "port": port,
            "control_token": control_token,
        },
    )
    _reconcile_interrupted_runs()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        _remove_runtime_if_owned(control_token)
    return 0


def _thread_id(arguments: Mapping[str, Any]) -> str:
    raw = arguments.get("thread_id") or arguments.get("imported_thread_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ControllerError("Choose an imported Claude thread.")
    return raw.strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerError("Expected an integer.")
    if value < minimum or value > maximum:
        raise ControllerError(f"Expected a value from {minimum} through {maximum}.")
    return value


def _string_list(arguments: Mapping[str, Any], key: str) -> list[str]:
    raw = arguments.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SELECTION_ITEMS:
        raise ControllerError(f"{key} must be a bounded array.")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ControllerError(f"{key} must contain non-empty paths.")
        value = item.strip()
        if value not in result:
            result.append(value)
    return result


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with _active_processes_lock:
        _active_processes.add(process)
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        process.communicate()
        raise
    finally:
        with _active_processes_lock:
            _active_processes.discard(process)
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _engine(
    command: str,
    arguments: Sequence[str] = (),
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    bounded_timeout = _bounded_int(
        timeout,
        default=180,
        minimum=1,
        maximum=MAX_COMMAND_TIMEOUT,
    )
    completed = _run_process(
        [sys.executable, str(RUNNER), command, *arguments, "--json"],
        cwd=PLUGIN_ROOT,
        timeout=bounded_timeout,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        detail = (completed.stderr or completed.stdout or "No output.").strip()
        raise ControllerError(
            f"The bakeoff engine returned invalid output: {detail[:500]}"
        ) from error
    if not isinstance(payload, dict):
        raise ControllerError("The bakeoff engine returned an invalid response.")
    if completed.returncode != 0 or payload.get("status") == "error":
        raise ControllerError(str(payload.get("error") or "The bakeoff command failed."))
    return payload


def _normalized_configuration(arguments: Mapping[str, Any]) -> dict[str, Any]:
    model = arguments.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ControllerError("Choose a Codex model.")
    repo = arguments.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise ControllerError("repo must be a non-empty path.")
    return {
        "thread_id": _thread_id(arguments),
        "model": model.strip(),
        "timeout_seconds": _bounded_int(
            arguments.get("timeout_seconds"),
            default=1800,
            minimum=1,
            maximum=14_400,
        ),
        "repo": repo.strip() if isinstance(repo, str) else None,
        "claude_output_files": _string_list(arguments, "claude_output_files"),
        "created_by_claude": _string_list(arguments, "created_by_claude"),
        "excluded_files": _string_list(arguments, "excluded_files"),
        "confirm_file_selection": arguments.get("confirm_file_selection") is True,
    }


def _configuration_fingerprint(configuration: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(configuration),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _configuration_arguments(arguments: Mapping[str, Any]) -> list[str]:
    configuration = _normalized_configuration(arguments)
    result = [
        "--imported-thread-id",
        str(configuration["thread_id"]),
        "--model",
        str(configuration["model"]),
        "--timeout-seconds",
        str(configuration["timeout_seconds"]),
    ]
    repo = configuration["repo"]
    if isinstance(repo, str):
        result.extend(("--repo", repo))
    for key, flag in (
        ("claude_output_files", "--claude-output-file"),
        ("created_by_claude", "--created-by-claude"),
        ("excluded_files", "--exclude-file"),
    ):
        for item in configuration[key]:
            result.extend((flag, item))
    if configuration["confirm_file_selection"] is True:
        result.append("--confirm-file-selection")
    return result


def _prepare_payload(arguments: Mapping[str, Any]) -> dict[str, Any]:
    configuration = _normalized_configuration(arguments)
    payload = _engine("prepare", _configuration_arguments(arguments))
    ready = payload.get("status") == "ready_for_approval"
    prepare_token: str | None = None
    if ready:
        prepare_token = secrets.token_urlsafe(32)
        with _jobs_lock:
            while len(_prepared_runs) >= MAX_PREPARE_TOKENS:
                _prepared_runs.pop(next(iter(_prepared_runs)))
            _prepared_runs[prepare_token] = {
                "fingerprint": _configuration_fingerprint(configuration),
                "run_id": None,
                "starting": False,
            }
    return {
        **payload,
        "ready": ready,
        "blockers": list(payload.get("blocking_reasons") or []),
        "prepare_token": prepare_token,
        "approval": {
            "required": True,
            "prompt": payload.get("approval_prompt"),
            "prepare_token": prepare_token,
        },
        "run_config": configuration,
    }


def _safe_run_directory(run_id: str) -> Path:
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ControllerError("The run ID is invalid.")
    path = (RUN_ROOT / run_id).resolve()
    if path.parent != RUN_ROOT:
        raise ControllerError("The run directory is outside the configured run root.")
    if not path.is_dir():
        raise ControllerError("The bakeoff run is unavailable.")
    return path


def _state_path(run_directory: Path) -> Path:
    return run_directory / STATE_NAME


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path, *, maximum: int = MAX_STATE_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > maximum:
            raise ControllerError(f"{path.name} is too large to display.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ControllerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ControllerError(f"Cannot read {path.name}: {error}") from error
    if not isinstance(payload, dict):
        raise ControllerError(f"{path.name} does not contain an object.")
    return payload


def _initial_state(
    run_directory: Path,
    *,
    prepare_token: str | None = None,
    configuration_fingerprint: str | None = None,
) -> dict[str, Any]:
    phases = [
        {
            "id": phase_id,
            "label": label,
            "status": "complete" if phase_id == "preparing" else "waiting",
        }
        for phase_id, label in PHASES
    ]
    now = _utc_now()
    return {
        "schema_version": 2,
        "id": run_directory.name,
        "run_id": run_directory.name,
        "run_directory": str(run_directory),
        "controller_pid": os.getpid(),
        "prepare_token_hash": (
            hashlib.sha256(prepare_token.encode("utf-8")).hexdigest()
            if prepare_token is not None
            else None
        ),
        "configuration_fingerprint": configuration_fingerprint,
        "status": "running",
        "phase": "creating_workspace",
        "phases": phases,
        "events": [
            {
                "at": now,
                "phase": "preparing",
                "status": "complete",
                "summary": "Configuration approved.",
            }
        ],
        "started_at": now,
        "updated_at": now,
        "error": None,
    }


def _update_state(
    run_directory: Path,
    *,
    phase: str | None = None,
    status: str | None = None,
    summary: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = _state_path(run_directory)
    with _jobs_lock:
        state = _read_json(path)
        if phase is not None:
            previous = str(state.get("phase") or "")
            rows = state.get("phases")
            phases = rows if isinstance(rows, list) else []
            found = False
            for item in phases:
                if not isinstance(item, dict):
                    continue
                if item.get("id") == phase:
                    item["status"] = "running" if status in {None, "running"} else status
                    found = True
                elif item.get("id") == previous and item.get("status") == "running":
                    item["status"] = "complete"
            if not found:
                raise ControllerError(f"Unknown run phase: {phase}")
            state["phase"] = phase
        if status is not None:
            state["status"] = status
            if status in {"completed", "failed", "cancelled"}:
                rows = state.get("phases")
                if isinstance(rows, list):
                    for item in rows:
                        if isinstance(item, dict) and item.get("id") == state.get("phase"):
                            item["status"] = "complete" if status == "completed" else status
                state["completed_at"] = _utc_now()
        if details:
            state.update(dict(details))
        if summary:
            events = state.setdefault("events", [])
            if not isinstance(events, list):
                events = []
                state["events"] = events
            events.append(
                {
                    "at": _utc_now(),
                    "phase": phase or state.get("phase"),
                    "status": status or "running",
                    "summary": summary[:1_000],
                }
            )
            if len(events) > 200:
                del events[:-200]
        state["updated_at"] = _utc_now()
        _write_json(path, state)
        return state


def _subprocess(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    completed = _run_process(
        command,
        cwd=cwd,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "No output.").strip()
        raise ControllerError(f"{Path(command[0]).name} failed: {detail[:1_500]}")
    return completed


def _materialize_workspace(run_directory: Path, target: Mapping[str, Any]) -> Path:
    workspace = run_directory / "workspaces" / "codex"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    target_type = target.get("type")
    if target_type == "projectless":
        workspace.mkdir()
        return workspace.resolve()
    if target_type != "project":
        raise ControllerError("The bakeoff task has an unsupported workspace target.")
    repository_raw = target.get("project")
    if not isinstance(repository_raw, str) or not repository_raw:
        raise ControllerError("The historical Git repository is missing.")
    repository = Path(repository_raw).expanduser().resolve()
    if not repository.is_dir():
        raise ControllerError("The historical Git repository is unavailable.")
    environment = target.get("environment")
    starting = environment.get("startingState") if isinstance(environment, Mapping) else None
    commit = starting.get("branchName") if isinstance(starting, Mapping) else None
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise ControllerError("The historical Git commit is invalid.")
    _subprocess(
        ["git", "clone", "--shared", "--no-checkout", str(repository), str(workspace)],
        cwd=run_directory,
        timeout=300,
    )
    _subprocess(
        ["git", "-C", str(workspace), "checkout", "--detach", commit],
        cwd=run_directory,
        timeout=180,
    )
    return workspace.resolve()


def _worker_request(
    request: Mapping[str, Any],
    *,
    working_directory: Path,
    read_only: bool,
) -> dict[str, Any]:
    model = request.get("model")
    prompt = request.get("prompt")
    if not isinstance(model, str) or not model:
        raise ControllerError("The worker request has no model.")
    if not isinstance(prompt, str) or not prompt:
        raise ControllerError("The worker request has no prompt.")
    timeout = request.get("timeout_seconds")
    timeout_seconds = (
        timeout
        if isinstance(timeout, int) and not isinstance(timeout, bool)
        else (600 if read_only else 1800)
    )
    payload: dict[str, Any] = {
        "type": "run",
        "requestId": secrets.token_hex(8),
        "model": model,
        "prompt": prompt,
        "workingDirectory": str(working_directory),
        "timeoutSeconds": min(max(timeout_seconds, 1), 14_400),
        "sandboxMode": "read-only" if read_only else "workspace-write",
        "networkAccess": not read_only,
    }
    expected_schema = request.get("expected_schema")
    if isinstance(expected_schema, Mapping):
        payload["outputSchema"] = dict(expected_schema)
    return payload


def _request_timeout_seconds(
    request: Mapping[str, Any],
    *,
    default: int,
) -> int:
    return _bounded_int(
        request.get("timeout_seconds"),
        default=default,
        minimum=1,
        maximum=14_400,
    )


def _long_command_timeout(
    request: Mapping[str, Any],
    *,
    default: int,
) -> int:
    return min(
        _request_timeout_seconds(request, default=default) + 300,
        MAX_COMMAND_TIMEOUT,
    )


def _run_worker(
    request: Mapping[str, Any],
    *,
    working_directory: Path,
    read_only: bool,
) -> dict[str, Any]:
    if not WORKER.is_file():
        raise ControllerError("The packaged Codex worker is unavailable.")
    payload = _worker_request(
        request,
        working_directory=working_directory,
        read_only=read_only,
    )
    timeout = int(payload["timeoutSeconds"]) + 90
    completed = _run_process(
        [_node_runtime(), str(WORKER)],
        input_text=json.dumps(payload, ensure_ascii=False) + "\n",
        cwd=PLUGIN_ROOT,
        timeout=timeout,
    )
    records: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    final = next(
        (
            item
            for item in reversed(records)
            if item.get("type") in {"result", "completed"} or item.get("status") == "completed"
        ),
        None,
    )
    failure = next(
        (item for item in reversed(records) if item.get("type") in {"failed", "canceled"}),
        None,
    )
    if completed.returncode != 0 or final is None:
        failure_message = (
            f"{failure.get('code')}: {failure.get('message')}"
            if isinstance(failure, Mapping) and failure.get("message")
            else None
        )
        detail = failure_message or (final or {}).get("error") or completed.stderr.strip()
        detail = detail or completed.stdout.strip() or "The Codex worker exited without a result."
        raise ControllerError(str(detail)[:2_000])
    raw_result = final.get("result")
    result = dict(raw_result) if isinstance(raw_result, Mapping) else dict(final)
    if result.get("status") not in {None, "completed"}:
        raise ControllerError(str(result.get("error") or "The Codex worker failed."))
    thread_id = result.get("threadId") or result.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ControllerError("The Codex worker did not return a thread ID.")
    return {
        **result,
        "thread_id": thread_id,
        "worktree": str(working_directory),
        "events": records[-100:],
    }


def _collect_result(
    run_directory: Path,
    worker: Mapping[str, Any],
    *,
    evaluator: str | None = None,
    normalization_for: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    arguments = [
        "--run-dir",
        str(run_directory),
        "--thread-id",
        str(worker["thread_id"]),
        "--worktree",
        str(worker["worktree"]),
    ]
    if evaluator is not None:
        arguments.extend(("--evaluator", evaluator))
    if normalization_for is not None:
        arguments.extend(("--normalization-for", normalization_for))
    return _engine("collect-native-result", arguments, timeout=timeout)


def _run_review_requests(
    run_directory: Path,
    requests: Sequence[Any],
    *,
    normalization: bool = False,
) -> list[Path]:
    results: list[Path] = []
    workspace_parent = run_directory / "review-workspaces"
    workspace_parent.mkdir(parents=True, exist_ok=True)
    for raw in requests:
        if not isinstance(raw, Mapping):
            continue
        evaluator = raw.get("normalization_for") if normalization else raw.get("evaluator")
        if not isinstance(evaluator, str) or not evaluator:
            raise ControllerError("A review request has no evaluator.")
        request = dict(raw)
        with tempfile.TemporaryDirectory(
            prefix="normalization-" if normalization else "review-",
            dir=workspace_parent,
        ) as temporary:
            workspace = Path(temporary).resolve()
            if not normalization:
                raw_paths = raw.get("candidate_paths")
                if not isinstance(raw_paths, list) or len(raw_paths) != 2:
                    raise ControllerError("A review request must contain two candidate files.")
                prompt = raw.get("prompt")
                if not isinstance(prompt, str):
                    raise ControllerError("A review request has no prompt.")
                isolated_paths: list[str] = []
                artifact_directory = (run_directory / "reviews").resolve()
                for raw_path in raw_paths:
                    if not isinstance(raw_path, str):
                        raise ControllerError("A review candidate path is invalid.")
                    source = Path(raw_path).resolve()
                    if (
                        source.parent != artifact_directory
                        or source.name not in {"candidate-a.json", "candidate-b.json"}
                        or not source.is_file()
                    ):
                        raise ControllerError(
                            "A review candidate is outside the anonymous artifacts."
                        )
                    destination = workspace / source.name
                    shutil.copyfile(source, destination)
                    prompt = prompt.replace(raw_path, str(destination))
                    isolated_paths.append(str(destination))
                request["prompt"] = prompt
                request["candidate_paths"] = isolated_paths
            worker = _run_worker(
                request,
                working_directory=workspace,
                read_only=True,
            )
            collected = _collect_result(
                run_directory,
                worker,
                evaluator=None if normalization else evaluator,
                normalization_for=evaluator if normalization else None,
                timeout=_long_command_timeout(raw, default=600),
            )
        key = "native_result_path"
        path = collected.get(key)
        if not isinstance(path, str):
            raise ControllerError("A reviewer result was not recorded.")
        results.append(Path(path).resolve())
    return results


def _coordinator(run_directory: Path, task_request: Mapping[str, Any]) -> None:
    try:
        long_timeout = _long_command_timeout(task_request, default=1800)
        _update_state(
            run_directory,
            phase="creating_workspace",
            summary="Creating an isolated historical workspace.",
        )
        target = task_request.get("target")
        if not isinstance(target, Mapping):
            raise ControllerError("The implementation task has no workspace target.")
        workspace = _materialize_workspace(run_directory, target)
        _update_state(
            run_directory,
            phase="implementing",
            summary="The approved Codex implementation is running.",
            details={"workspace": str(workspace)},
        )
        worker = _run_worker(task_request, working_directory=workspace, read_only=False)
        _update_state(
            run_directory,
            phase="collecting",
            summary="The completed Codex result is being captured.",
            details={
                "implementation_thread_id": worker["thread_id"],
                "worker_events": worker.get("events", []),
            },
        )
        collected = _collect_result(
            run_directory,
            worker,
            timeout=long_timeout,
        )
        native_result = collected.get("native_result_path")
        if not isinstance(native_result, str):
            raise ControllerError("The implementation result was not recorded.")
        _engine(
            "complete-run",
            ["--run-dir", str(run_directory), "--native-result", native_result],
        )
        _update_state(
            run_directory,
            phase="verifying",
            summary="Running identical baseline-owned checks.",
        )
        verification = _engine(
            "verify",
            ["--run-dir", str(run_directory)],
            timeout=long_timeout,
        )
        _update_state(
            run_directory,
            phase="reviewing",
            summary="Running the available blinded Codex review.",
            details={"verification_status": verification.get("status")},
        )
        evaluation = _engine(
            "evaluate",
            ["--run-dir", str(run_directory), "--evaluator", "codex"],
            timeout=long_timeout,
        )
        requests = evaluation.get("task_requests")
        if isinstance(requests, list) and requests:
            review_paths = _run_review_requests(run_directory, requests)
            combined = _engine(
                "collect-native-results",
                [
                    "--run-dir",
                    str(run_directory),
                    *[
                        argument
                        for path in review_paths
                        for argument in ("--native-result", str(path))
                    ],
                ],
                timeout=long_timeout,
            )
            combined_path = combined.get("native_results_path")
            if not isinstance(combined_path, str):
                raise ControllerError("The reviewer results were not combined.")
            completed = _engine(
                "complete-evaluation",
                [
                    "--run-dir",
                    str(run_directory),
                    "--native-results",
                    combined_path,
                ],
                timeout=long_timeout,
            )
            normalization_requests = completed.get("task_requests")
            if (
                completed.get("status") == "native_task_required"
                and isinstance(normalization_requests, list)
                and normalization_requests
            ):
                normalized = _run_review_requests(
                    run_directory,
                    normalization_requests,
                    normalization=True,
                )
                _engine(
                    "complete-evaluation",
                    [
                        "--run-dir",
                        str(run_directory),
                        "--native-results",
                        combined_path,
                        *[
                            argument
                            for path in normalized
                            for argument in ("--normalized-result", str(path))
                        ],
                    ],
                    timeout=long_timeout,
                )
        _update_state(
            run_directory,
            phase="reporting",
            summary="Finalizing the comparison report.",
        )
        report_paths = _engine(
            "report",
            ["--run-dir", str(run_directory)],
            timeout=long_timeout,
        )
        report = _read_json(
            Path(str(report_paths["report_json"])),
            maximum=MAX_REPORT_BYTES,
        )
        _update_state(
            run_directory,
            phase="reporting",
            status="completed",
            summary="The bakeoff report is ready.",
            details={
                "report_html": report_paths.get("report_html"),
                "report_json": report_paths.get("report_json"),
                "report_summary": {
                    "winner": report.get("winner"),
                    "evaluation": report.get("evaluation"),
                },
            },
        )
    except Exception as error:  # noqa: BLE001 - the durable state must record every failure.
        try:
            _update_state(
                run_directory,
                status="failed",
                summary=f"Bakeoff stopped: {error}",
                details={"error": str(error)[:2_000]},
            )
        except Exception as state_error:  # noqa: BLE001
            print(
                f"Codex Bakeoff coordinator failed to record state: {state_error}",
                file=sys.stderr,
            )
    finally:
        with _jobs_lock:
            _jobs.pop(run_directory.name, None)


def _persisted_run_for_token(
    prepare_token: str,
    configuration_fingerprint: str,
) -> dict[str, Any] | None:
    if not RUN_ROOT.is_dir():
        return None
    token_hash = hashlib.sha256(prepare_token.encode("utf-8")).hexdigest()
    for run_directory in RUN_ROOT.iterdir():
        state_path = _state_path(run_directory)
        if not run_directory.is_dir() or not state_path.is_file():
            continue
        try:
            state = _read_json(state_path)
        except ControllerError:
            continue
        if state.get("prepare_token_hash") != token_hash:
            continue
        if state.get("configuration_fingerprint") != configuration_fingerprint:
            raise ControllerError(
                "The approved configuration changed. Prepare and approve it again."
            )
        return state
    return None


def _start_run(arguments: Mapping[str, Any]) -> dict[str, Any]:
    if arguments.get("approved") is not True:
        raise ControllerError("Explicit approval is required before starting a bakeoff.")
    prepare_token = arguments.get("prepare_token")
    if not isinstance(prepare_token, str) or len(prepare_token) < 32:
        raise ControllerError("Prepare and approve this exact configuration before starting.")
    configuration = _normalized_configuration(arguments)
    fingerprint = _configuration_fingerprint(configuration)
    with _jobs_lock:
        receipt = _prepared_runs.get(prepare_token)
        if receipt is None:
            persisted = _persisted_run_for_token(prepare_token, fingerprint)
            if persisted is not None:
                return {
                    "run_id": persisted["run_id"],
                    "run": persisted,
                    "idempotent": True,
                }
            raise ControllerError(
                "The prepare token is missing or expired. Prepare and approve the run again."
            )
        if receipt.get("fingerprint") != fingerprint:
            raise ControllerError(
                "The approved configuration changed. Prepare and approve it again."
            )
        run_id = receipt.get("run_id")
        if isinstance(run_id, str):
            state = _read_json(_state_path(_safe_run_directory(run_id)))
            return {"run_id": run_id, "run": state, "idempotent": True}
        if receipt.get("starting") is True:
            raise ControllerError("This approved run is already being started. Retry shortly.")
        receipt["starting"] = True

    try:
        command_arguments = _configuration_arguments(configuration)
        payload = _engine(
            "run",
            [*command_arguments, "--approve", "--run-root", str(RUN_ROOT)],
        )
    except Exception:
        with _jobs_lock:
            receipt["starting"] = False
        raise
    run_directory_raw = payload.get("run_directory")
    task_request = payload.get("task_request")
    if not isinstance(run_directory_raw, str) or not isinstance(task_request, Mapping):
        with _jobs_lock:
            receipt["starting"] = False
        raise ControllerError("The bakeoff engine did not return an implementation task.")
    run_directory = Path(run_directory_raw).resolve()
    if run_directory.parent != RUN_ROOT:
        with _jobs_lock:
            receipt["starting"] = False
        raise ControllerError("The bakeoff engine returned an unexpected run directory.")
    with _jobs_lock:
        receipt["run_id"] = run_directory.name
        receipt["starting"] = False
    state = _initial_state(
        run_directory,
        prepare_token=prepare_token,
        configuration_fingerprint=fingerprint,
    )
    _write_json(_state_path(run_directory), state)
    thread = threading.Thread(
        target=_coordinator,
        args=(run_directory, dict(task_request)),
        name=f"codex-bakeoff-{run_directory.name}",
        daemon=True,
    )
    with _jobs_lock:
        if run_directory.name in _jobs:
            raise ControllerError("This bakeoff run is already active.")
        _jobs[run_directory.name] = thread
    thread.start()
    return {"run_id": run_directory.name, "run": state, "idempotent": False}


def _recent_runs(limit: int = 12) -> list[dict[str, Any]]:
    if not RUN_ROOT.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(RUN_ROOT.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        state_path = _state_path(path)
        if not state_path.is_file():
            continue
        try:
            state = _read_json(state_path)
        except ControllerError:
            continue
        results.append(state)
        if len(results) >= limit:
            break
    return results


def _inspect_thread(arguments: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = _thread_id(arguments)
    session_args = ["--imported-thread-id", thread_id]
    repo = arguments.get("repo")
    baseline_args = list(session_args)
    if repo is not None:
        if not isinstance(repo, str) or not repo.strip():
            raise ControllerError("repo must be a non-empty path.")
        baseline_args.extend(("--repo", repo.strip()))
    replay = _engine("replay", session_args)
    capabilities = _engine("capabilities", session_args)
    baseline = _engine("baseline", baseline_args)
    models = _engine("models")
    file_selection = baseline.get("file_selection")
    selection = file_selection if isinstance(file_selection, Mapping) else {}
    baseline_value = baseline.get("baseline")
    baseline_record = baseline_value if isinstance(baseline_value, Mapping) else {}
    thread = replay.get("replay")
    thread_record = thread if isinstance(thread, Mapping) else {}
    return {
        "thread": dict(thread_record),
        "replay": dict(thread_record),
        "baseline": dict(baseline_record),
        "capabilities": capabilities,
        "file_selection": dict(selection),
        "workspace": {
            "kind": selection.get("source_kind") or baseline_record.get("source_kind"),
            "files": list(selection.get("candidates") or []),
            "requires_empty_attestation": selection.get("source_kind") == "non_git",
            "requires_confirmation": bool(selection.get("requires_confirmation")),
            "complete": bool(selection.get("complete")),
        },
        "models": list(models.get("options") or []),
        "questions": list(baseline.get("questions") or []),
        "blockers": list(baseline.get("blocking_reasons") or []),
    }


def _call_tool(params: Any) -> dict[str, Any]:
    if not isinstance(params, Mapping):
        raise ControllerError("Tool call params must be an object.")
    name = str(params.get("name") or "")
    arguments = _argument_object(params)
    if name == "open_controller":
        runtime_error = _python_runtime_error_result()
        if runtime_error is not None:
            return runtime_error
        return _open_external_controller()
    if name == "stop_port_process_and_open_controller":
        runtime_error = _python_runtime_error_result()
        if runtime_error is not None:
            return runtime_error
        if arguments.get("confirmed") is not True:
            raise ControllerError("Explicit user confirmation is required.")
        token = arguments.get("confirmation_token")
        if not isinstance(token, str) or len(token) < 32:
            raise ControllerError("A valid port-process confirmation token is required.")
        return _open_external_controller(confirmation_token=token)
    if name == "get_state":
        models = _engine("models")
        state = {
            "plugin_version": SERVER_VERSION,
            "models": list(models.get("options") or []),
            "recent_runs": _recent_runs(),
        }
        return _text_result("Codex Bakeoff is ready.", {"state": state})
    if name == "list_threads":
        offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=100_000)
        limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=100)
        query = arguments.get("query")
        if query is not None and not isinstance(query, str):
            raise ControllerError("query must be a string.")
        if isinstance(query, str) and query.strip():
            payload = _engine("sessions", ["--limit", "100", "--offset", "0"])
            needle = query.casefold().strip()
            raw = payload.get("sessions")
            sessions = raw if isinstance(raw, list) else []
            filtered = [
                item
                for item in sessions
                if isinstance(item, Mapping)
                and needle
                in " ".join(
                    str(item.get(key) or "") for key in ("title", "project_dir", "claude_model")
                ).casefold()
            ]
            threads = filtered[offset : offset + limit]
            response = {
                **payload,
                "offset": offset,
                "total": len(filtered),
                "has_more": offset + len(threads) < len(filtered),
                "sessions": threads,
            }
        else:
            response = _engine(
                "sessions",
                ["--limit", str(limit), "--offset", str(offset)],
            )
        sessions = response.get("sessions")
        threads = list(sessions) if isinstance(sessions, list) else []
        return _text_result(
            f"Loaded {len(threads)} imported Claude thread(s).",
            {**response, "threads": threads},
        )
    if name == "inspect_thread":
        return _text_result("Imported thread inspected.", _inspect_thread(arguments))
    if name == "prepare_run":
        return _text_result("Bakeoff configuration prepared.", _prepare_payload(arguments))
    if name == "start_run":
        return _text_result("The approved bakeoff started.", _start_run(arguments))
    if name == "get_run":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ControllerError("run_id is required.")
        state = _read_json(_state_path(_safe_run_directory(run_id)))
        return _text_result(
            f"Bakeoff {run_id} is {state.get('status', 'unknown')}.",
            {"run": state},
        )
    if name == "get_report":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ControllerError("run_id is required.")
        run_directory = _safe_run_directory(run_id)
        report_path = run_directory / "report.json"
        if not report_path.is_file():
            raise ControllerError("The bakeoff report is not ready.")
        report = _read_json(report_path, maximum=MAX_REPORT_BYTES)
        return _text_result(
            "The bakeoff report is ready.",
            {
                "report": report,
                "report_json": str(report_path),
                "report_html": str(run_directory / "report.html"),
            },
        )
    raise ControllerError(f"Unknown Codex Bakeoff tool: {name}")


def _negotiated_protocol_version(params: Any) -> str:
    if isinstance(params, Mapping):
        version = params.get("protocolVersion")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return "2025-11-25"


def _handle_request(method: Any, params: Any) -> tuple[Any, dict[str, Any] | None]:
    if method == "initialize":
        return {
            "protocolVersion": _negotiated_protocol_version(params),
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "title": APP_TITLE,
                "version": SERVER_VERSION,
            },
        }, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": tool_definitions()}, None
    if method == "tools/call":
        try:
            return _call_tool(params), None
        except Exception as error:  # noqa: BLE001 - always answer an MCP tool request.
            return {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }, None
    if method == "resources/list":
        return {"resources": []}, None
    if method == "resources/templates/list":
        return {"resourceTemplates": []}, None
    if method == "resources/read":
        uri = params.get("uri") if isinstance(params, Mapping) else None
        return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
    if method == "prompts/list":
        return {"prompts": []}, None
    return None, {"code": -32601, "message": f"Method not found: {method}"}


def _handle_rpc_line(line: str) -> dict[str, Any] | None:
    request = json.loads(line)
    if not isinstance(request, Mapping):
        raise ValueError("MCP request must be an object.")
    if request.get("id") is None:
        return None
    result, error = _handle_request(request.get("method"), request.get("params"))
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def _pid_is_alive(raw_pid: Any) -> bool:
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
        return False
    try:
        os.kill(raw_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _mark_interrupted(run_directory: Path, summary: str) -> None:
    try:
        state = _read_json(_state_path(run_directory))
        if state.get("status") != "running":
            return
        _update_state(
            run_directory,
            status="failed",
            summary=summary,
            details={
                "error": summary,
                "interrupted": True,
                "interruption_reason": "controller_stopped",
            },
        )
    except ControllerError:
        return


def _reconcile_interrupted_runs() -> None:
    if not RUN_ROOT.is_dir():
        return
    for run_directory in RUN_ROOT.iterdir():
        state_path = _state_path(run_directory)
        if not run_directory.is_dir() or not state_path.is_file():
            continue
        try:
            state = _read_json(state_path)
        except ControllerError:
            continue
        if state.get("status") != "running":
            continue
        if _pid_is_alive(state.get("controller_pid")):
            continue
        _mark_interrupted(
            run_directory,
            "The controller stopped before this bakeoff finished.",
        )


def _stop_jobs() -> None:
    _shutdown.set()
    with _active_processes_lock:
        processes = list(_active_processes)
    for process in processes:
        _terminate_process_group(process)
    with _jobs_lock:
        run_ids = list(_jobs)
    for run_id in run_ids:
        run_directory = RUN_ROOT / run_id
        if run_directory.is_dir():
            _mark_interrupted(
                run_directory,
                "The controller shut down before this bakeoff finished.",
            )


atexit.register(_stop_jobs)


def _handle_shutdown(_signum: int, _frame: Any) -> None:
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _handle_shutdown)


def run_stdio() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = _handle_rpc_line(line)
        except Exception as error:  # noqa: BLE001 - keep stdout protocol-clean.
            print(f"Codex Bakeoff MCP request failed: {error}", file=sys.stderr)
            continue
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)


def main() -> int:
    if sys.argv[1:] == ["--http"]:
        return run_http()
    if sys.argv[1:]:
        print("Usage: server.py [--http]", file=sys.stderr)
        return 2
    try:
        run_stdio()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
