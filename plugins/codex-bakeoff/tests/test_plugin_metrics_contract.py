"""Validate Codex's actual version-one numeric plugin metrics contract."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_MANIFEST = PLUGIN_ROOT / "analytics.yaml"
OPERATION_PATHS = {
    "metrics_probe": "./scripts/metrics/1_probe_metrics.py",
    "controller_launch": "./scripts/metrics/2_start_controller.py",
    "replay_start": "./scripts/metrics/3_report_start_metrics.py",
    "replay_metrics": "./scripts/metrics/4_report_final_metrics.py",
}

QUALITY_DIMENSIONS = (
    "request_fulfillment",
    "code_quality",
    "change_scope",
    "reliability",
    "safe_operations",
    "accurate_reporting",
)
MODEL_SLOTS = ("one", "two", "three", "four", "five", "six", "seven", "eight")
MEASUREMENTS = {
    "attempt",
    "attempt_model",
    "replay",
    "failure",
    "failure_detail",
    "failure_exit_code",
    "failure_retry_count",
    "failure_duration_seconds",
    "outcome",
    "codex_input_tokens",
    "codex_output_tokens",
    "codex_cached_input_tokens",
    "codex_cost_usd",
    "codex_duration_seconds",
    "codex_wall_clock_seconds",
    "codex_phase_duration_seconds",
    "claude_input_tokens",
    "claude_output_tokens",
    "claude_cached_input_tokens",
    "claude_cost_usd",
    "claude_duration_seconds",
    "claude_wall_clock_seconds",
    "evaluation",
    "codex_score",
    "claude_score",
    "codex_dimension_score",
    "claude_dimension_score",
    "dimension_outcome",
    "final_prestart_timeout",
    "measurements_omitted",
}


def _scalar(raw: str) -> object:
    if raw == "{}":
        return {}
    if raw.startswith("[") and raw.endswith("]"):
        values = raw[1:-1].strip()
        return [_scalar(value.strip()) for value in values.split(",")] if values else []
    if raw.isdecimal():
        return int(raw)
    return raw


def _manifest() -> dict[str, Any]:
    """Parse this manifest's mapping-only YAML subset without PyYAML."""
    root: dict[str, Any] = {}
    parents: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line in ANALYTICS_MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ValueError(f"Invalid manifest indentation: {line}")
        key, separator, raw = line.strip().partition(":")
        if not separator or not key:
            raise ValueError(f"Invalid manifest declaration: {line}")
        while parents[-1][0] >= indent:
            parents.pop()
        parent = parents[-1][1]
        if key in parent:
            raise ValueError(f"Duplicate manifest declaration: {key}")
        value = raw.strip()
        if value:
            parent[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            parents.append((indent, child))
    return root


class PluginMetricsContractTests(unittest.TestCase):
    def test_manifest_matches_strict_host_version_one_operation_contract(self) -> None:
        manifest = _manifest()

        self.assertEqual(set(manifest), {"version", "operations"})
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(set(manifest["operations"]), set(OPERATION_PATHS))
        expected_measurements = {
            "metrics_probe": {"metrics_probe", "plugin_version"},
            "controller_launch": {"controller_launch"},
            "replay_start": {
                "run_start",
                "selected_model_count",
                "run_start_timeout",
            },
            "replay_metrics": MEASUREMENTS,
        }
        for name, path in OPERATION_PATHS.items():
            with self.subTest(operation=name):
                operation = manifest["operations"][name]
                self.assertEqual(set(operation), {"path", "measurements"})
                self.assertEqual(operation["path"], path)
                self.assertEqual(set(operation["measurements"]), expected_measurements[name])
                self.assertTrue((PLUGIN_ROOT / path).is_file())

    def test_measurements_have_only_closed_enum_dimensions(self) -> None:
        operations = _manifest()["operations"]

        for operation_name, operation in operations.items():
            for name, declaration in operation["measurements"].items():
                with self.subTest(operation=operation_name, measurement=name):
                    self.assertRegex(name, r"\A[a-z][a-z0-9_]*\Z")
                    self.assertLessEqual(len(name), 64)
                    self.assertTrue(set(declaration) <= {"dimensions"})
                    dimensions = declaration.get("dimensions", {})
                    self.assertLessEqual(len(dimensions), 8)
                    for dimension, values in dimensions.items():
                        self.assertRegex(dimension, r"\A[a-z][a-z0-9_]*\Z")
                        self.assertTrue(values)
                        self.assertEqual(len(values), len(set(values)))
                        for value in values:
                            self.assertRegex(value, r"\A[a-z][a-z0-9_]*\Z")

        self.assertEqual(operations["replay_metrics"]["measurements"]["measurements_omitted"], {})

    def test_checkpoint_dimensions_preserve_separate_stage_accounting(self) -> None:
        operations = _manifest()["operations"]

        self.assertEqual(
            operations["controller_launch"]["measurements"]["controller_launch"],
            {"dimensions": {"outcome": ["ready", "startup_failure", "launch_unobserved"]}},
        )
        self.assertEqual(
            operations["replay_start"]["measurements"],
            {
                "run_start": {},
                "selected_model_count": {},
                "run_start_timeout": {},
            },
        )
        self.assertEqual(
            operations["replay_metrics"]["measurements"]["attempt"]["dimensions"][
                "reporting_complete"
            ],
            ["yes", "no"],
        )
        self.assertEqual(
            operations["replay_metrics"]["measurements"]["final_prestart_timeout"],
            {},
        )

    def test_model_slots_preserve_distinct_parallel_model_measurements(self) -> None:
        declarations = _manifest()["operations"]["replay_metrics"]["measurements"]
        contextual = {
            name
            for name, declaration in declarations.items()
            if "model_slot" in declaration.get("dimensions", {}) and name != "attempt_model"
        }

        self.assertIn("replay", contextual)
        self.assertIn("outcome", contextual)
        self.assertIn("codex_dimension_score", contextual)
        self.assertIn("claude_dimension_score", contextual)
        for name in contextual:
            with self.subTest(measurement=name):
                dimensions = declarations[name]["dimensions"]
                self.assertEqual(tuple(dimensions["model_slot"]), MODEL_SLOTS)
                self.assertEqual(dimensions["codex_model"], ["sol", "terra", "luna", "other"])
                self.assertEqual(dimensions["source"], ["imported", "sample"])

        for provider in ("codex", "claude"):
            dimensions = declarations[f"{provider}_dimension_score"]["dimensions"]
            self.assertEqual(tuple(dimensions["score_dimension"]), QUALITY_DIMENSIONS)

        evaluator = declarations["evaluation"]["dimensions"]
        self.assertEqual(len(evaluator), 8)
        self.assertEqual(evaluator["self_judged"], ["yes", "no"])

    def test_shared_claude_measurements_are_not_duplicated_per_model(self) -> None:
        declarations = _manifest()["operations"]["replay_metrics"]["measurements"]

        for name in (
            "claude_input_tokens",
            "claude_output_tokens",
            "claude_cached_input_tokens",
            "claude_cost_usd",
        ):
            with self.subTest(measurement=name):
                self.assertEqual(set(declarations[name]["dimensions"]), {"claude_model", "source"})

        timing = declarations["claude_duration_seconds"]["dimensions"]
        self.assertEqual(set(timing), {"claude_model", "source", "timing_basis"})
        self.assertEqual(timing["timing_basis"], ["model_request", "recorded_wall_clock"])


if __name__ == "__main__":
    unittest.main()
