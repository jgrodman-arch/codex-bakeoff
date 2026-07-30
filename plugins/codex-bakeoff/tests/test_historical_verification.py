"""Tests for immediate shared checks."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "historical_verification.py"
SPEC = importlib.util.spec_from_file_location("lean_verification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verification
SPEC.loader.exec_module(verification)


class LeanVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repo"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "user.email",
                "test@example.com",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.name", "Test"],
            check=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _commit(self) -> str:
        subprocess.run(["git", "-C", str(self.repository), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-qm", "baseline"], check=True)
        return subprocess.check_output(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"], text=True
        ).strip()

    def test_discovery_uses_baseline_owned_tests_without_a_plan_hash(self) -> None:
        tests = self.repository / "tests"
        tests.mkdir()
        (tests / "sample.test.mjs").write_text(
            "import test from 'node:test';\n"
            "import assert from 'node:assert/strict';\n"
            "test('ok', () => assert.equal(1, 1));\n",
            encoding="utf-8",
        )
        commit = self._commit()
        discovered = verification.discover_checks(
            {
                "kind": "git_commit",
                "repository": str(self.repository),
                "commit": commit,
            }
        )
        self.assertEqual(discovered["status"], "ready")
        self.assertEqual(discovered["checks"][0]["command"][0:2], ["node", "--test"])
        self.assertNotIn("plan_sha256", discovered)

    def test_verify_runs_the_same_current_checks_for_all_sides(self) -> None:
        tests = self.repository / "tests"
        tests.mkdir()
        (tests / "sample.test.mjs").write_text(
            "import test from 'node:test';\n"
            "import assert from 'node:assert/strict';\n"
            "test('ok', () => assert.equal(1, 1));\n",
            encoding="utf-8",
        )
        commit = self._commit()
        result = verification.verify_candidates(
            baseline={
                "kind": "git_commit",
                "repository": str(self.repository),
                "commit": commit,
            },
            candidate_patches={"claude": "", "codex": ""},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["baseline"]["status"], "passed")
        self.assertEqual(result["candidates"]["claude"]["status"], "passed")
        self.assertEqual(result["candidates"]["codex"]["status"], "passed")
        self.assertNotIn("plan_sha256", result)

    def test_empty_baseline_is_not_verifiable(self) -> None:
        result = verification.verify_candidates(
            baseline={
                "kind": "empty_directory",
                "repository": str(self.repository),
                "commit": None,
            },
            candidate_patches={"claude": "", "codex": ""},
        )
        self.assertEqual(result["status"], "not_verifiable")


if __name__ == "__main__":
    unittest.main()
