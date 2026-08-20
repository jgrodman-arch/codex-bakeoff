"""Tests for authentic Claude Code task definitions and checkout-safe verification."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_claude_sample.py"
SPEC = importlib.util.spec_from_file_location("claude_sample_verification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verification
SPEC.loader.exec_module(verification)


class ClaudeSampleTaskManifestTests(unittest.TestCase):
    def test_manifest_contains_exact_requested_models_and_original_tasks(self) -> None:
        manifest = json.loads((ROOT / "assets" / "claude-code-sample-tasks.json").read_text())

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(
            [(model["id"], model["label"]) for model in manifest["models"]],
            [
                ("claude-fable-5", "Claude Fable 5"),
                ("claude-opus-5", "Claude Opus 5"),
                ("claude-sonnet-5", "Claude Sonnet 5"),
                ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
            ],
        )
        self.assertEqual(
            [(task["id"], task["baseline_commit"]) for task in manifest["tasks"]],
            [
                ("jqlang__jq-2157", "f88c4e5888d6d125695444d044df4bb55ad75888"),
                ("jqlang__jq-2919", "7f547827e47b5ade563a293329deb4226496d72f"),
            ],
        )
        for task in manifest["tasks"]:
            self.assertTrue(task["prompt"].startswith(task["title"] + "\n\n"))
            self.assertEqual(task["repository"], "jqlang/jq")
            self.assertNotIn("patch", task)
            self.assertNotIn("opponents", task)


class ClaudeSampleVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        source = self.repository / "src"
        source.mkdir(parents=True)
        (source / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        (source / "builtin.c").write_text("/* fixture */\n", encoding="utf-8")
        (source / "builtin.jq").write_text('def quote: "a\\b";\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_uri_check_detects_real_regression_without_modifying_checkout(self) -> None:
        before = {
            path.relative_to(self.repository): path.read_bytes()
            for path in self.repository.rglob("*")
            if path.is_file()
        }
        temporary_builds: list[Path] = []

        def run(
            command: list[str], **kwargs: str | bool | None
        ) -> subprocess.CompletedProcess[str]:
            if command[0] == "cc":
                build_directory = Path(command[2][2:])
                temporary_builds.append(build_directory)
                self.assertFalse(build_directory.is_relative_to(self.repository))
                self.assertTrue((build_directory / "src" / "version.h").is_file())
                self.assertIn('\\"a\\\\b\\"', (build_directory / "src" / "builtin.inc").read_text())
                return subprocess.CompletedProcess(command, 0, "", "")

            source_input = kwargs["input"]
            outputs = {
                "-_.~!'()*\n": "-_.~!'()*\n",
                "μ\n": "%CE%BC\n",
                "hello world\n": "hello%20world\n",
            }
            return subprocess.CompletedProcess(command, 0, outputs[str(source_input)], "")

        with mock.patch.object(verification.subprocess, "run", side_effect=run):
            result = verification.verify_task(verification.URI_TASK_ID, self.repository)

        self.assertFalse(result["passed"])
        self.assertEqual([check["passed"] for check in result["checks"]], [False, True, True])
        self.assertEqual(result["checks"][0]["expected_stdout"], "-_.~%21%27%28%29%2A")
        self.assertEqual(
            before,
            {
                path.relative_to(self.repository): path.read_bytes()
                for path in self.repository.rglob("*")
                if path.is_file()
            },
        )
        self.assertTrue(temporary_builds)
        self.assertFalse(temporary_builds[0].exists())

    def test_argument_check_reports_fixed_behavior_and_preserves_existing_behavior(self) -> None:
        outputs = iter(("bar\n", "1\n", "1\n"))

        def run(command: list[str], **_: str | bool | None) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command, 0, "" if command[0] == "cc" else next(outputs), ""
            )

        with mock.patch.object(verification.subprocess, "run", side_effect=run):
            result = verification.verify_task(verification.ARGUMENT_TASK_ID, str(self.repository))

        self.assertTrue(result["passed"])
        self.assertEqual(
            [check["name"] for check in result["checks"]],
            ["script_after_separator", "script_before_separator", "plain_filter_after_separator"],
        )
        self.assertIn("-ljq", result["build"]["command"])
        self.assertNotIn("-lonig", result["build"]["command"])

    def test_compile_failure_is_structured_and_skips_behavior_checks(self) -> None:
        failed = subprocess.CompletedProcess(["cc"], 1, "", "compiler failure")

        with mock.patch.object(verification.subprocess, "run", return_value=failed) as run:
            result = verification.verify_task(verification.URI_TASK_ID, self.repository)

        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"], [])
        self.assertEqual(result["build"]["stderr"], "compiler failure")
        run.assert_called_once()

    def test_missing_compiler_is_reported_as_a_build_failure(self) -> None:
        with mock.patch.object(verification.subprocess, "run", side_effect=FileNotFoundError("cc")):
            result = verification.verify_task(verification.ARGUMENT_TASK_ID, self.repository)

        self.assertFalse(result["passed"])
        self.assertEqual(result["build"]["returncode"], 127)
        self.assertIn("cc", result["build"]["stderr"])

    def test_rejects_unknown_tasks_and_missing_repositories(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Claude Code sample task"):
            verification.verify_task("unknown-task", self.repository)

        with self.assertRaisesRegex(ValueError, "repository does not exist"):
            verification.verify_task(verification.URI_TASK_ID, self.root / "missing")

    def test_cli_prints_structured_json_and_returns_the_verification_result(self) -> None:
        result = {
            "task_id": verification.ARGUMENT_TASK_ID,
            "repository": str(self.repository),
            "passed": True,
            "build": {"command": [], "returncode": 0, "stdout": "", "stderr": ""},
            "checks": [],
        }
        output = io.StringIO()

        with (
            mock.patch.object(verification, "verify_task", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            status = verification.main(
                ("--task", verification.ARGUMENT_TASK_ID, "--repository", str(self.repository))
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
