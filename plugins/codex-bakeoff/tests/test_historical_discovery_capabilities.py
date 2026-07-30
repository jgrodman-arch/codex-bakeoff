"""Tests for lean capability matching."""

from __future__ import annotations

import importlib
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
        self.patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def test_native_tools_are_ready_and_unknown_tools_are_best_effort(self) -> None:
        result = capabilities.inspect_capabilities({"observed_tools": ["Bash", "ImaginaryTool"]})
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_id["tool:Bash"]["status"], "codex_native_equivalent")
        self.assertEqual(by_id["tool:ImaginaryTool"]["status"], "best_effort")
        self.assertEqual(result["unresolved_blockers"], [])

    def test_exact_local_skill_is_ready_without_copying_it(self) -> None:
        skill = self.codex_home / "skills" / "game-design"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: game-design\n---\n", encoding="utf-8")
        result = capabilities.inspect_capabilities({"observed_skills": ["game-design"]})
        self.assertEqual(result["items"][0]["status"], "available_and_ready")
        self.assertNotIn("tree_sha256", result["items"][0])

    def test_missing_skill_has_one_import_action(self) -> None:
        result = capabilities.inspect_capabilities({"observed_skills": ["missing-skill"]})
        self.assertEqual(len(result["resolution_actions"]), 1)
        self.assertEqual(result["resolution_actions"][0]["action"], "import_from_claude")

    def test_connector_becomes_ready_after_observed_verification(self) -> None:
        (self.codex_home / "config.toml").write_text(
            "[mcp_servers.slack]\ncommand = 'slack'\n", encoding="utf-8"
        )
        blocked = capabilities.inspect_capabilities({"connector_names": ["slack"]})
        action_id = blocked["resolution_actions"][0]["id"]
        ready = capabilities.inspect_capabilities(
            {"connector_names": ["slack"]},
            verified_action_ids=[action_id],
        )
        self.assertEqual(ready["items"][0]["status"], "available_and_ready")
        self.assertEqual(ready["unresolved_blockers"], [])

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
