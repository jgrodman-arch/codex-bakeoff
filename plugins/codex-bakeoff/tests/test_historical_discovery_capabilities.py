"""Tests for lean capability matching."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
capabilities = importlib.import_module("historical_discovery_capabilities")


class LeanCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary.name) / "codex"
        self.codex_home.mkdir()
        self.claude_home = Path(self.temporary.name) / "claude"
        self.claude_home.mkdir()
        self.patch = mock.patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                "CLAUDE_CONFIG_DIR": str(self.claude_home),
            },
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def test_native_tools_are_ready_and_unknown_tools_are_best_effort(self) -> None:
        result = capabilities.inspect_capabilities({"observed_tools": ["Bash", "ImaginaryTool"]})
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_id["tool:Bash"]["status"], "codex_native_equivalent")
        self.assertEqual(by_id["tool:ImaginaryTool"]["status"], "best_effort")
        self.assertEqual(result["unavailable_capabilities"], [])

    def test_claude_browser_uses_installed_codex_browser(self) -> None:
        plugin = (
            self.codex_home
            / "plugins"
            / "cache"
            / "openai-bundled"
            / "browser"
            / "1.0.0"
        )
        skill = plugin / "skills" / "control-in-app-browser"
        skill.mkdir(parents=True)
        (plugin / ".codex-plugin").mkdir()
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "browser",
                    "version": "1.0.0",
                    "skills": "./skills/",
                }
            ),
            encoding="utf-8",
        )
        (skill / "SKILL.md").write_text(
            "---\nname: control-in-app-browser\n---\n",
            encoding="utf-8",
        )

        result = capabilities.inspect_capabilities(
            {
                "observed_tools": [
                    "mcp__Claude_Browser__computer",
                    "mcp__Claude_Browser__javascript_tool",
                ],
                "connector_names": ["Claude_Browser"],
            }
        )
        by_name = {item["name"]: item for item in result["items"]}

        for name in (
            "mcp__Claude_Browser__computer",
            "mcp__Claude_Browser__javascript_tool",
            "Claude_Browser",
        ):
            self.assertEqual(by_name[name]["status"], "available_and_ready")
            self.assertEqual(by_name[name]["equivalent"], "Codex Browser")
        self.assertEqual(result["unavailable_capabilities"], [])
        self.assertEqual(result["resolution_actions"], [])

    def test_claude_browser_requires_installed_codex_browser(self) -> None:
        result = capabilities.inspect_capabilities(
            {
                "observed_tools": ["mcp__Claude_Browser__computer"],
                "connector_names": ["Claude_Browser"],
            }
        )
        by_name = {item["name"]: item for item in result["items"]}

        self.assertEqual(
            by_name["mcp__Claude_Browser__computer"]["status"],
            "best_effort",
        )
        self.assertEqual(by_name["Claude_Browser"]["status"], "not_available")
        self.assertEqual(result["resolution_actions"][0]["action"], "import_from_claude")

    def test_exact_local_skill_is_ready_without_copying_it(self) -> None:
        skill = self.codex_home / "skills" / "game-design"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: game-design\n---\n", encoding="utf-8")
        result = capabilities.inspect_capabilities({"observed_skills": ["game-design"]})
        self.assertEqual(result["items"][0]["status"], "available_and_ready")
        self.assertNotIn("tree_sha256", result["items"][0])

    def test_artifact_design_without_importable_source_has_no_import_action(self) -> None:
        result = capabilities.inspect_capabilities({"observed_skills": ["artifact-design"]})
        self.assertEqual(result["items"][0]["status"], "not_available")
        self.assertEqual(
            result["items"][0]["resolution"],
            "claude_managed_or_source_unavailable",
        )
        self.assertEqual(
            result["items"][0]["reason"],
            "Claude-managed or source unavailable.",
        )
        self.assertIsNone(result["items"][0]["guidance"])
        self.assertEqual(result["resolution_actions"], [])

    def test_artifact_design_uses_installed_codex_skill_equivalents(self) -> None:
        for name in ("sites-building", "visualize"):
            skill = self.codex_home / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

        result = capabilities.inspect_capabilities({"observed_skills": ["artifact-design"]})

        item = result["items"][0]
        self.assertEqual(item["status"], "codex_native_equivalent")
        self.assertEqual(
            item["equivalent"],
            "sites:sites-building + visualize:visualize",
        )
        self.assertEqual(result["resolution_actions"], [])

    def test_importable_claude_skill_has_one_import_action(self) -> None:
        skill = self.claude_home / "skills" / "claude-only-test-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: claude-only-test-skill\n---\n",
            encoding="utf-8",
        )

        result = capabilities.inspect_capabilities(
            {"observed_skills": ["claude-only-test-skill"]}
        )

        self.assertEqual(result["items"][0]["status"], "not_available")
        self.assertEqual(result["items"][0]["claude_source_status"], "importable")
        self.assertEqual(
            result["items"][0]["guidance"],
            "Go to Settings > Import to review and import this Claude skill.",
        )
        self.assertEqual(len(result["resolution_actions"]), 1)
        self.assertEqual(result["resolution_actions"][0]["action"], "import_from_claude")
        self.assertEqual(result["resolution_actions"][0]["status"], "optional")
        self.assertNotIn("required_steps", result["resolution_actions"][0])

    def test_connector_becomes_ready_after_observed_verification(self) -> None:
        (self.codex_home / "config.toml").write_text(
            "[mcp_servers.slack]\ncommand = 'slack'\n", encoding="utf-8"
        )
        unavailable = capabilities.inspect_capabilities({"connector_names": ["slack"]})
        self.assertEqual(unavailable["items"][0]["status"], "not_available")
        self.assertEqual(unavailable["resolution_actions"][0]["action"], "verify_access")
        action_id = unavailable["resolution_actions"][0]["id"]
        ready = capabilities.inspect_capabilities(
            {"connector_names": ["slack"]},
            verified_action_ids=[action_id],
        )
        self.assertEqual(ready["items"][0]["status"], "available_and_ready")
        self.assertEqual(ready["unavailable_capabilities"], [])

    def test_missing_connector_recommends_import_without_requiring_it(self) -> None:
        result = capabilities.inspect_capabilities({"connector_names": ["slack"]})
        item = result["items"][0]
        action = result["resolution_actions"][0]

        self.assertEqual(item["status"], "not_available")
        self.assertEqual(action["status"], "optional")
        self.assertEqual(action["action"], "import_from_claude")
        self.assertEqual(action["id"], "import:claude-connector:slack")
        self.assertEqual(action["suggested_steps"], ["import_from_claude", "verify_access"])
        self.assertEqual(
            action["remediation_action"],
            "Go to Settings > Import to review and import this Claude connector.",
        )

    def test_existing_agents_md_is_codex_native(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        agents = project / "AGENTS.md"
        agents.write_text("Use focused tests.\n", encoding="utf-8")

        result = capabilities.inspect_capabilities(
            {"observed_instruction_paths": [str(agents)]}
        )

        self.assertEqual(result["items"][0]["status"], "codex_native_equivalent")
        self.assertEqual(result["resolution_actions"], [])

    def test_relative_agents_md_uses_the_replay_project(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("Use focused tests.\n", encoding="utf-8")

        result = capabilities.inspect_capabilities(
            {
                "project_dir": str(project),
                "observed_instruction_paths": ["AGENTS.md"],
            }
        )

        self.assertEqual(result["items"][0]["status"], "codex_native_equivalent")

    def test_missing_agents_md_is_not_available_without_import_guidance(self) -> None:
        agents = Path(self.temporary.name) / "project" / "AGENTS.md"

        result = capabilities.inspect_capabilities(
            {"observed_instruction_paths": [str(agents)]}
        )

        self.assertEqual(result["items"][0]["status"], "not_available")
        self.assertIsNone(result["items"][0]["guidance"])
        self.assertEqual(result["resolution_actions"], [])

    def test_claude_md_recommends_import_as_agents_md(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        claude = project / "CLAUDE.md"
        claude.write_text("Use focused tests.\n", encoding="utf-8")

        result = capabilities.inspect_capabilities(
            {"observed_instruction_paths": [str(claude)]}
        )

        self.assertEqual(result["items"][0]["status"], "not_available")
        self.assertEqual(
            result["items"][0]["guidance"],
            "Go to Settings > Import to import this CLAUDE.md as AGENTS.md.",
        )
        self.assertEqual(result["resolution_actions"][0]["status"], "optional")

    def test_claude_md_with_existing_agents_md_does_not_recommend_import(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        claude = project / "CLAUDE.md"
        claude.write_text("Claude instructions.\n", encoding="utf-8")
        (project / "AGENTS.md").write_text("Codex instructions.\n", encoding="utf-8")

        result = capabilities.inspect_capabilities(
            {"observed_instruction_paths": [str(claude)]}
        )

        self.assertEqual(result["items"][0]["status"], "not_available")
        self.assertIsNone(result["items"][0]["guidance"])
        self.assertEqual(result["resolution_actions"], [])

    def test_connector_config_loads_without_stdlib_tomllib(self) -> None:
        (self.codex_home / "config.toml").write_text(
            '[mcp_servers."company.slack"]\ncommand = "slack"\n'
            "[mcpServers.github]\ncommand = 'github'\n",
            encoding="utf-8",
        )
        with mock.patch.object(capabilities, "tomllib", None):
            config = capabilities._read_codex_config()
        self.assertEqual(
            capabilities._configured_connectors(config),
            {"company.slack", "github"},
        )


if __name__ == "__main__":
    unittest.main()
