"""Regression coverage for version-aware Replay controller launches."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_MCP_FILES = (
    "server.py",
    "controller.html",
    "controller.css",
    "controller-ranges.js",
    "replay_configuration.py",
    "replay_batch.py",
    "controller_constants.py",
    "final_results_receipt.py",
)


def load_server():
    spec = importlib.util.spec_from_file_location(
        "replay_installation_mcp_server", PLUGIN_ROOT / "mcp" / "server.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the MCP server.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ControllerInstallationTests(unittest.TestCase):
    def test_installed_controller_preserves_manifest_git_runtime_environment(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("Git is required for the local runtime probe.")
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_root = root / "installed"
            (plugin_root / "mcp").mkdir(parents=True)
            (plugin_root / "scripts").mkdir()
            (plugin_root / ".codex-plugin").mkdir()
            for name in REQUIRED_MCP_FILES:
                shutil.copyfile(PLUGIN_ROOT / "mcp" / name, plugin_root / "mcp" / name)
            shutil.copyfile(PLUGIN_ROOT / ".mcp.json", plugin_root / ".mcp.json")
            shutil.copyfile(
                PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
                plugin_root / ".codex-plugin" / "plugin.json",
            )
            helpers = root / "git-core"
            helpers.mkdir()
            helper = helpers / "git-replay-runtime-probe"
            helper.write_text(
                '#!/bin/sh\nset -eu\ngit init --quiet "$1"\n'
                "git config --system --get replay.runtime\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            templates = root / "templates"
            templates.mkdir()
            (templates / "runtime-marker").write_text("preserved", encoding="utf-8")
            system_config = root / "gitconfig"
            system_config.write_text("[replay]\n\truntime = preserved\n", encoding="utf-8")
            repository = root / "repository"
            # A fixture engine exercises the installed controller's actual Git subprocess
            # without downloading a sample or invoking a model.
            (plugin_root / "scripts" / "historical_bakeoff.py").write_text(
                "import json, os, subprocess\n"
                "result = subprocess.run(['git', 'replay-runtime-probe', "
                f"{str(repository)!r}], check=True, capture_output=True, text=True)\n"
                "print(json.dumps({'options': [{'id': result.stdout.strip(), "
                "'managed_environment': {name: os.environ.get(name) for name in "
                "('OG_GIT_EXECUTABLE', 'CODEX_PREFERRED_GIT_EXECUTABLE')}}]}))\n",
                encoding="utf-8",
            )
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            host_environment = {
                **os.environ,
                "CODEX_HOME": str(root / "codex-home"),
                "CODEX_BAKEOFF_RUN_ROOT": str(root / "runs"),
                "CODEX_BAKEOFF_CONTROLLER_PORT": str(port),
                "GIT_EXEC_PATH": str(helpers),
                "GIT_TEMPLATE_DIR": str(templates),
                "GIT_CONFIG_SYSTEM": str(system_config),
            }
            manifest = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
            declaration = manifest["mcpServers"]["codex-bakeoff"]
            environment = {
                name: host_environment[name]
                for name in declaration["env_vars"]
                if name in host_environment
            }
            session = "e" * 32
            runtime_path = root / "controllers" / session / "controller-server.json"
            try:
                launched = subprocess.run(
                    [sys.executable, *declaration["args"]],
                    cwd=plugin_root,
                    env=environment,
                    input=json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "open_controller",
                                "arguments": {"controller_session_id": session},
                            },
                        }
                    )
                    + "\n",
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=20,
                )
                response = json.loads(launched.stdout)["result"]
                self.assertNotIn("isError", response, response)
                url = response["structuredContent"]["launch_url"]
                status, body = server._http_request(
                    "POST",
                    url + "api/call",
                    headers={"Origin": url.rstrip("/"), "Content-Type": "application/json"},
                    data=json.dumps({"name": "get_state", "arguments": {}}).encode(),
                    timeout=10,
                )
                self.assertEqual(status, 200, body)
                state = json.loads(body)["structuredContent"]["state"]
                self.assertEqual(state["diagnostics"], [])
                self.assertEqual(state["models"][0]["id"], "preserved")
                self.assertEqual(
                    (repository / ".git" / "runtime-marker").read_text(encoding="utf-8"),
                    "preserved",
                )
                self.assertEqual(
                    state["models"][0]["managed_environment"],
                    {
                        name: host_environment.get(name)
                        for name in ("OG_GIT_EXECUTABLE", "CODEX_PREFERRED_GIT_EXECUTABLE")
                    },
                )
            finally:
                if runtime_path.is_file():
                    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                    server._http_request(
                        "POST",
                        f"http://127.0.0.1:{runtime['port']}/api/shutdown",
                        headers={server.CONTROLLER_CONTROL_HEADER: runtime["control_token"]},
                    )

    def test_newest_enabled_installation_uses_semantic_versions_and_ignores_disabled(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            (codex_home / "config.toml").write_text(
                '[plugins."codex-bakeoff@older"]\nenabled = true\n'
                '[plugins."codex-bakeoff@newer"]\nenabled = true\n'
                '[plugins."codex-bakeoff@incomplete"]\nenabled = true\n'
                '[plugins."codex-bakeoff@disabled"]\nenabled = false\n',
                encoding="utf-8",
            )

            def install(marketplace: str, version: str) -> Path:
                root = codex_home / "plugins" / "cache" / marketplace / "codex-bakeoff" / version
                (root / ".codex-plugin").mkdir(parents=True)
                (root / "mcp").mkdir()
                (root / ".codex-plugin" / "plugin.json").write_text(
                    json.dumps({"name": "codex-bakeoff", "version": version}), encoding="utf-8"
                )
                for name in REQUIRED_MCP_FILES:
                    (root / "mcp" / name).touch()
                return root

            install("older", "1.0.9")
            newest = install("newer", "1.0.10")
            incomplete = install("incomplete", "1.0.11")
            (incomplete / "mcp" / "replay_batch.py").unlink()
            install("disabled", "9.0.0")

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                self.assertEqual(
                    server._plugin_installations().latest_enabled_plugin_root(
                        PLUGIN_ROOT, "codex-bakeoff", "1.0.8"
                    ),
                    newest.resolve(),
                )

    def test_controller_daemon_launches_the_newest_enabled_plugin_installation(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_root = root / "newest"
            (plugin_root / "mcp").mkdir(parents=True)
            (plugin_root / "mcp" / "server.py").touch()
            (plugin_root / "mcp" / "controller.html").touch()
            with (
                socket.socket() as reservation,
                mock.patch.object(server, "RUN_ROOT", root / "runs"),
                mock.patch.object(server.subprocess, "Popen") as spawn,
            ):
                reservation.bind(("127.0.0.1", 0))
                server._spawn_controller_daemon(
                    reservation=reservation,
                    controller_session_id="a" * 32,
                    plugin_root=plugin_root,
                )

            self.assertEqual(
                spawn.call_args.args[0],
                [sys.executable, str(plugin_root / "mcp" / "server.py"), "--http"],
            )
            self.assertEqual(spawn.call_args.kwargs["cwd"], plugin_root)

    def test_older_mcp_process_starts_a_live_newer_controller(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                '[plugins."codex-bakeoff@newer"]\nenabled = true\n', encoding="utf-8"
            )
            plugin_root = codex_home / "plugins" / "cache" / "newer" / "codex-bakeoff" / "1.0.10"
            (plugin_root / ".codex-plugin").mkdir(parents=True)
            (plugin_root / "mcp").mkdir()
            (plugin_root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": "codex-bakeoff", "version": "1.0.10"}), encoding="utf-8"
            )
            for name in REQUIRED_MCP_FILES:
                shutil.copyfile(PLUGIN_ROOT / "mcp" / name, plugin_root / "mcp" / name)

            port = None
            session = None
            processes = []
            original_spawn = server._spawn_controller_daemon

            def record_spawn(**arguments):
                process = original_spawn(**arguments)
                processes.append(process)
                return process

            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", root / "runs"),
                    mock.patch.object(server, "SERVER_VERSION", "1.0.8"),
                    mock.patch.object(server, "_spawn_controller_daemon", side_effect=record_spawn),
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}),
                ):
                    port, health = server._ensure_controller_daemon()
                    session = health["controller_session_id"]
                    self.assertEqual(health["version"], "1.0.10")
                    status, _ = server._control_request(
                        port, "/api/shutdown", controller_session_id=session
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(processes[0].wait(timeout=5), 0)
                    port = None
            finally:
                if port is not None and session is not None:
                    with mock.patch.object(server, "RUN_ROOT", root / "runs"):
                        server._control_request(
                            port, "/api/shutdown", controller_session_id=session
                        )

    def test_stale_controller_cleanup_preserves_active_and_current_sessions(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = {
                "a" * 32: (43101, "1.0.5", 0),
                "b" * 32: (43102, "1.0.5", 1),
                "c" * 32: (43103, server.SERVER_VERSION, 0),
                "d" * 32: (43104, "1.0.5", 0),
            }
            for session, (port, _, _) in sessions.items():
                instance = root / "controllers" / session
                instance.mkdir(parents=True)
                (instance / "controller-server.json").write_text(
                    json.dumps({"port": port, "control_token": "x" * 40}), encoding="utf-8"
                )

            def probe(port: int, *, controller_session_id: str):
                _, version, active_runs = sessions[controller_session_id]
                status = "unverified" if controller_session_id == "d" * 32 else "compatible"
                return status, {
                    "version": version,
                    "controller_session_id": controller_session_id,
                    "active_runs": active_runs,
                }

            with (
                mock.patch.object(server, "RUN_ROOT", root / "runs"),
                mock.patch.object(server, "_probe_controller", side_effect=probe),
                mock.patch.object(server, "_control_request") as shutdown,
            ):
                server._retire_stale_idle_controllers(PLUGIN_ROOT)

            shutdown.assert_called_once_with(43101, "/api/shutdown", controller_session_id="a" * 32)


if __name__ == "__main__":
    unittest.main()
