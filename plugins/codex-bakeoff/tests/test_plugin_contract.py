"""Lean plugin packaging and workflow contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
README = PLUGIN_ROOT / "README.md"
SKILL = PLUGIN_ROOT / "skills" / "codex-bakeoff" / "SKILL.md"
AGENT = PLUGIN_ROOT / "skills" / "codex-bakeoff" / "agents" / "openai.yaml"
BASELINE_HANDLING = PLUGIN_ROOT / "BASELINE_HANDLING.md"
RUNNER = PLUGIN_ROOT / "scripts" / "historical_bakeoff.py"
MCP_CONFIG = PLUGIN_ROOT / ".mcp.json"
MCP_SERVER = PLUGIN_ROOT / "mcp" / "server.py"
MCP_CONTROLLER = PLUGIN_ROOT / "mcp" / "controller.html"
MCP_WORKER = PLUGIN_ROOT / "mcp" / "codex-worker.mjs"


class PluginContractTests(unittest.TestCase):
    def test_manifest_is_valid_and_points_inside_plugin(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], "codex-bakeoff")
        self.assertEqual(payload["skills"], "./skills/")
        self.assertTrue((PLUGIN_ROOT / payload["skills"]).is_dir())
        self.assertEqual(payload["mcpServers"], "./.mcp.json")
        self.assertTrue((PLUGIN_ROOT / payload["mcpServers"]).is_file())
        self.assertEqual(payload["interface"]["displayName"], "Codex Bakeoff")
        self.assertIn("in-app browser", payload["interface"]["shortDescription"])
        self.assertIn("in-app browser", payload["description"])
        self.assertIn("external browser", payload["interface"]["shortDescription"])
        self.assertIn("external browser fallback", payload["description"])
        self.assertIn("available loopback port", payload["interface"]["longDescription"])
        self.assertIn(
            "Multiple replay sessions can run in parallel",
            payload["interface"]["longDescription"],
        )
        self.assertNotIn("embedded", json.dumps(payload).lower())
        self.assertNotIn("MCP App", json.dumps(payload))

    def test_skill_opens_the_controller_in_the_codex_in_app_browser(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        flat = " ".join(text.split())
        self.assertIn("mcp__codex_bakeoff.open_controller", text)
        self.assertIn("browser controller owns", flat.lower())
        self.assertIn("command -v codex", text)
        self.assertIn("codex_cli_path", text)
        self.assertIn("prepared: true", text)
        self.assertIn("opened: false", text)
        self.assertIn("launch_url", text)
        self.assertIn("open_in_codex", text)
        self.assertIn('{ target: { type: "browser", url: launch_url } }', flat)
        self.assertIn("Do not provide `threadId`", flat)
        self.assertIn("native call explicitly succeeds", flat)
        self.assertIn("`open_in_codex` is unavailable or the native call fails", flat)
        self.assertIn('`open "$launch_url"`', flat)
        self.assertIn('`xdg-open "$launch_url"`', flat)
        self.assertIn('`Start-Process "$launch_url"`', flat)
        self.assertIn("opened in an external browser only after that command succeeds", flat)
        self.assertNotIn("Never open an external browser automatically", flat)
        self.assertNotIn("short-lived", text)
        self.assertNotIn("authenticated", text)
        self.assertNotIn("open_replay_app", text)
        self.assertNotIn("RUNNER", text)
        self.assertNotIn("--approve", text)

    def test_skill_starts_an_independent_controller_without_stopping_a_listener(self) -> None:
        text = " ".join(SKILL.read_text(encoding="utf-8").split())
        self.assertIn("available loopback port", text)
        self.assertIn("fresh, independent controller for this invocation", text)
        self.assertIn("An occupied port is skipped", text)
        self.assertIn("Never stop an existing process", text)
        self.assertIn("Multiple replay sessions can run in parallel", text)
        self.assertIn("plain loopback URL", text)
        self.assertNotIn("requires_confirmation", text)
        self.assertNotIn("confirmation_token", text)
        self.assertNotIn("stop_port_process_and_open_controller", text)

    def test_beginning_and_end_state_document_contract(self) -> None:
        text = BASELINE_HANDLING.read_text(encoding="utf-8")
        flat = " ".join(text.split())
        self.assertIn("Git     -> Git", text)
        self.assertIn("Non-Git -> Git", text)
        self.assertIn("Non-Git -> Non-Git", text)
        self.assertIn("`Git -> Non-Git` is invalid", text)
        self.assertIn("--confirm-empty-beginning", text)
        self.assertIn("does not confirm the beginning state", text)
        self.assertIn("diffing the empty Git tree", text)
        self.assertIn("All committed files come from the ending commit", text)
        self.assertIn("live source paths", flat)
        self.assertIn("completed Codex workspace", flat)
        self.assertIn("unchanged and available through `complete-run`", flat)
        self.assertIn("excluded symmetrically from both candidate", flat)
        self.assertIn("comparison limitation", flat)
        self.assertIn("If any file existed before Claude, stop", flat)
        self.assertNotIn("--registered-baseline-project", text)
        self.assertNotIn("--registered-baseline-project-id", text)
        self.assertIn("original Claude directory is never modified", text)

    def test_packaged_engine_preserves_baseline_and_completion_contract(self) -> None:
        prepare_help = subprocess.run(
            [sys.executable, str(RUNNER), "prepare", "--help"],
            cwd=PLUGIN_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        complete_help = subprocess.run(
            [sys.executable, str(RUNNER), "complete-run", "--help"],
            cwd=PLUGIN_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout

        for option in (
            "--beginning-kind {git,non_git}",
            "--ending-kind {git,non_git}",
            "--baseline-commit",
            "--ending-commit",
            "--confirm-empty-beginning",
            "--confirm-file-selection",
            "--claude-output-file",
            "--created-by-claude",
            "--exclude-file",
        ):
            with self.subTest(option=option):
                self.assertIn(option, prepare_help)
        self.assertNotIn("--registered-baseline-project", prepare_help)
        self.assertIn("--run-dir", complete_help)
        self.assertIn("--native-result", complete_help)

        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn('beginning_kind == "git" and ending_kind == "non_git"', runner)
        self.assertIn("A Git beginning state requires a Git end state.", runner)
        self.assertIn("excluded symmetrically from both candidates", runner)
        self.assertIn('commands.add_parser("complete-run")', runner)

        source = MCP_SERVER.read_text(encoding="utf-8")
        for field in ("beginning_kind", "ending_kind", "baseline_commit", "ending_commit"):
            with self.subTest(mcp_configuration_field=field):
                self.assertIn(f'"{field}"', source)
        self.assertIn("A Git beginning state requires a Git end state.", source)

    def test_agent_prompt_opens_the_codex_in_app_browser(self) -> None:
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("open Codex Bakeoff in the Codex in-app browser now", text)
        self.assertIn("falling back to an external browser when unavailable", text)
        self.assertIn("independent session on an automatically available loopback port", text)
        self.assertIn("Do not run the workflow in chat", text)
        self.assertNotIn("embedded", text)

    def test_readme_describes_the_codex_in_app_browser_controller(self) -> None:
        text = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("local browser controller", text)
        self.assertIn("Codex in-app browser", text)
        self.assertIn("external browser when the in-app browser is unavailable", text)
        self.assertIn("independent controller on an automatically available loopback port", text)
        self.assertIn("Multiple replay sessions can run in parallel", text)
        self.assertIn("each controller resumes only its own runs", text)
        self.assertIn("Inactive controllers exit automatically", text)
        self.assertNotIn("embedded", text)
        self.assertNotIn("MCP App", text)

    def test_runner_and_assets_are_packaged(self) -> None:
        self.assertTrue(RUNNER.is_file())
        self.assertTrue((PLUGIN_ROOT / "assets" / "model-pricing.json").is_file())
        self.assertTrue((PLUGIN_ROOT / "assets" / "icon.svg").is_file())

    def test_browser_controller_is_packaged_with_direct_http_transport(self) -> None:
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        server = config["mcpServers"]["codex-bakeoff"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["./mcp/server.py"])
        self.assertEqual(server["cwd"], ".")
        self.assertIn("CODEX_MCP_NODE_PATH", server["env_vars"])
        self.assertIn("PATH", server["env_vars"])
        self.assertIn("CODEX_BAKEOFF_CONTROLLER_PORT", server["env_vars"])
        self.assertTrue(MCP_SERVER.is_file())
        self.assertTrue(MCP_CONTROLLER.is_file())
        self.assertLessEqual(
            MCP_CONTROLLER.stat().st_size,
            150_000,
            "Browser controller must satisfy the monorepo file-size limit.",
        )
        self.assertTrue(MCP_WORKER.is_file())
        controller = MCP_CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("/api/call", controller)
        self.assertIn("/api/download", controller)
        self.assertIn("localStorage", controller)
        self.assertNotIn("X-Codex-Replay-Session", controller)
        self.assertNotIn("controller-session", controller)
        self.assertNotIn('"ui/initialize"', controller)
        self.assertNotIn('"tools/call"', controller)
        self.assertNotIn("window.openai", controller)

    def test_removed_state_machine_modules_are_absent(self) -> None:
        for name in (
            "historical_execution_core.py",
            "historical_execution_filesystem.py",
            "historical_execution_review.py",
            "historical_execution_runtime.py",
            "historical_file_attribution.py",
        ):
            self.assertFalse((PLUGIN_ROOT / "scripts" / name).exists(), name)
        self.assertTrue((PLUGIN_ROOT / "scripts" / "historical_file_selection.py").is_file())


if __name__ == "__main__":
    unittest.main()
