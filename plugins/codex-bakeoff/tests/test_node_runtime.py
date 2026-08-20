"""Node runtime discovery under restricted MCP server environments."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_mcp_server import load_server


class NodeRuntimeTests(unittest.TestCase):
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
