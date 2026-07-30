"""Lean plugin packaging and workflow contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
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
        self.assertIn("external browser", payload["interface"]["shortDescription"])

    def test_skill_opens_the_controller_without_running_a_chat_workflow(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        flat = " ".join(text.split())
        self.assertIn("mcp__codex_bakeoff.open_controller", text)
        self.assertIn("external browser", flat.lower())
        self.assertIn("controller owns", flat.lower())
        self.assertIn("After the tool confirms the external browser opened, stop", flat)
        self.assertIn("stop_port_process_and_open_controller", text)
        self.assertIn("Pause until the user explicitly confirms", flat)
        self.assertNotIn("RUNNER", text)
        self.assertNotIn("--approve", text)
        self.assertNotIn("Do not open a browser automatically", text)

    def test_non_git_baseline_contract_requires_an_empty_start(self) -> None:
        text = BASELINE_HANDLING.read_text(encoding="utf-8")
        flat = " ".join(text.split())
        non_git = text.split("## Non-Git directories", 1)[1].split(
            "## Preparation and approval", 1
        )[0]
        headings = [line for line in non_git.splitlines() if line.startswith("### ")]
        self.assertEqual(
            headings,
            [
                "### Created by Claude",
                "### Exclude",
            ],
        )
        self.assertIn("live source paths", flat)
        self.assertIn("completed Codex workspace", flat)
        self.assertIn("unchanged and available through `complete-run`", flat)
        self.assertIn("excluded symmetrically from both candidate", flat)
        self.assertIn("comparison limitation", flat)
        self.assertIn("directory was empty before Claude", flat)
        self.assertIn("If any file existed before Claude, stop", flat)
        self.assertNotIn("--registered-baseline-project", text)
        self.assertNotIn("--registered-baseline-project-id", text)
        self.assertIn("original Claude directory is never modified", text)

    def test_agent_prompt_opens_the_controller(self) -> None:
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("open the external-browser controller now", text)
        self.assertIn("Do not run the workflow in chat", text)

    def test_runner_and_assets_are_packaged(self) -> None:
        self.assertTrue(RUNNER.is_file())
        self.assertTrue((PLUGIN_ROOT / "assets" / "model-pricing.json").is_file())
        self.assertTrue((PLUGIN_ROOT / "assets" / "icon.svg").is_file())

    def test_external_browser_controller_is_packaged(self) -> None:
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        server = config["mcpServers"]["codex-bakeoff"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["./mcp/server.py"])
        self.assertEqual(server["cwd"], ".")
        self.assertIn("CLAUDE_BAKEOFF_CONTROLLER_PORT", server["env_vars"])
        self.assertTrue(MCP_SERVER.is_file())
        self.assertTrue(MCP_CONTROLLER.is_file())
        self.assertTrue(MCP_WORKER.is_file())
        controller = MCP_CONTROLLER.read_text(encoding="utf-8")
        self.assertIn('fetch("/api/call"', controller)
        self.assertIn("localStorage", controller)
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
