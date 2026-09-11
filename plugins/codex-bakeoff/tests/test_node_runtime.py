"""Node runtime discovery under restricted MCP server environments."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_mcp_server import load_server


class NodeRuntimeTests(unittest.TestCase):
    def test_packaged_worker_preserves_selected_node_for_its_wrapper(self) -> None:
        server = load_server()
        node_runtime = server._node_runtime()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_bin = root / "host-bin"
            host_bin.mkdir()
            stale_bin = root / "stale-bin"
            stale_bin.mkdir()
            stale_node = stale_bin / "node"
            stale_node.write_text(
                "#!/bin/sh\nprintf 'stale node selected\\n' >&2\nexit 91\n", encoding="utf-8"
            )
            stale_node.chmod(0o700)
            fake_codex = host_bin / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import json, os, sys",
                        "if sys.argv[1:] == ['--version']:",
                        "    print('codex fixture')",
                        "    raise SystemExit(0)",
                        "sys.stdin.read()",
                        "print(json.dumps({'type': 'thread.started', 'thread_id': 'runtime-thread'}))",
                        "environment = {name: os.environ[name] for name in ('PATH', 'GIT_EXEC_PATH')}",
                        "environment['CLI_EXECUTABLE'] = sys.argv[0]",
                        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(environment)}}))",
                        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            selected_bin = root / "selected-node-bin"
            selected_bin.mkdir()
            selected_node = selected_bin / "node"
            selected_node.symlink_to(node_runtime)
            alternate_codex = selected_bin / "codex"
            alternate_codex.write_text(fake_codex.read_text(encoding="utf-8"), encoding="utf-8")
            alternate_codex.chmod(0o700)
            node_directory = str(selected_bin)
            cases = {
                "missing-node": [str(host_bin)],
                "stale-node": [str(stale_bin), str(host_bin)],
                "selected-node-later": [
                    str(stale_bin),
                    str(host_bin),
                    node_directory,
                    node_directory,
                ],
                "automatic-cli": [str(host_bin), node_directory],
            }
            for name, path_entries in cases.items():
                with self.subTest(name=name):
                    run_directory = root / name
                    run_directory.mkdir()
                    environment = {
                        "PATH": os.pathsep.join(path_entries),
                        "CODEX_MCP_NODE_PATH": str(selected_node),
                        "GIT_EXEC_PATH": str(root / "host-git-helpers"),
                    }
                    if name != "automatic-cli":
                        environment["CODEX_CLI_PATH"] = str(fake_codex)
                    with (
                        mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(
                            server, "CODEX_CLI_PATH_HINT_PATH", root / "no-hint.json"
                        ),
                    ):
                        result = server._run_worker(
                            {"model": "gpt-5.6-sol", "prompt": "Check the worker runtime."},
                            run_directory=run_directory,
                            working_directory=root,
                            read_only=True,
                            log_label="runtime-test",
                            timeout=10,
                        )
                        self.assertEqual(dict(os.environ), environment)

                    self.assertEqual(result["thread_id"], "runtime-thread")
                    child_environment = json.loads(result["finalResponse"])
                    self.assertEqual(
                        child_environment["PATH"],
                        os.pathsep.join(
                            [node_directory, *[p for p in path_entries if p != node_directory]]
                        ),
                    )
                    self.assertEqual(
                        child_environment["GIT_EXEC_PATH"], environment["GIT_EXEC_PATH"]
                    )
                    self.assertEqual(child_environment["CLI_EXECUTABLE"], str(fake_codex))

    def test_uses_codex_runtime_hints_without_path(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (
                    {"CODEX_BROWSER_USE_NODE_PATH": str(root / "browser-node")},
                    root / "browser-node",
                ),
                (
                    {"CODEX_ELECTRON_RESOURCES_PATH": str(root / "resources")},
                    root / "resources" / "cua_node" / "bin" / "node",
                ),
                (
                    {"CODEX_CLI_PATH": str(root / "cli" / "codex")},
                    root / "cli" / "cua_node" / "bin" / "node",
                ),
            )
            for environment, node in cases:
                with self.subTest(environment=environment):
                    node.parent.mkdir(parents=True, exist_ok=True)
                    node.write_text("#!/bin/sh\nprintf 'v22.0.0\\n'\n", encoding="utf-8")
                    node.chmod(0o700)
                    with mock.patch.dict(os.environ, {"PATH": "", **environment}, clear=True):
                        self.assertEqual(server._node_runtime(), str(node))

    def test_uses_bundled_runtime_without_path(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary)
            node = cache_root / "codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
            node.parent.mkdir(parents=True)
            node.write_text("#!/bin/sh\nprintf 'v22.0.0\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(cache_root), "PATH": ""}, clear=True
            ):
                self.assertEqual(server._node_runtime(), str(node))

    def test_uses_homebrew_without_path(self) -> None:
        server = load_server()
        node = Path("/opt/homebrew/bin/node")
        with (
            mock.patch.dict(os.environ, {"PATH": ""}, clear=True),
            mock.patch.object(
                server.Path, "is_file", autospec=True, side_effect=lambda path: path == node
            ),
            mock.patch.object(server.os, "access", return_value=True),
            mock.patch.object(
                server.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="v22.0.0\n")
            ),
        ):
            self.assertEqual(server._node_runtime(), str(node))

    def test_skips_unsupported_fallback_runtime(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary)
            node = cache_root / "codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
            node.parent.mkdir(parents=True)
            node.write_text("#!/bin/sh\nprintf 'v16.20.0\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            with (
                mock.patch.dict(
                    os.environ, {"XDG_CACHE_HOME": str(cache_root), "PATH": ""}, clear=True
                ),
                mock.patch.object(
                    server.Path, "is_file", autospec=True, side_effect=lambda path: path == node
                ),
                mock.patch.object(server.shutil, "which", return_value="/existing/node"),
            ):
                self.assertEqual(server._node_runtime(), "/existing/node")
