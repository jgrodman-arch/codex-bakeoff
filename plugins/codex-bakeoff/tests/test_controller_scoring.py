"""Regression coverage for the controller's comparison score display."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

CONTROLLER_PATH = Path(__file__).resolve().parent.parent / "mcp" / "controller.html"


def _render_outcome_summary(
    evaluation: dict[str, object],
    *,
    schema_version: int = 3,
    costs: dict[str, object] | None = None,
) -> dict[str, object]:
    harness = r"""
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const extract = (start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  if (first < 0 || last <= first) throw new Error(`Missing controller code: ${start}`);
  return source.slice(first, last);
};
const render = new Function([
  extract("      const isObject =", "      const safeJson ="),
  extract("      const formatCost =", "      function unwrapToolResult"),
  extract("      function outcomeSummary(evaluation,", "      function comparisonClass"),
  "return outcomeSummary;",
].join("\n"))();
process.stdout.write(JSON.stringify(render(
  JSON.parse(process.argv[2]), Number(process.argv[3]), JSON.parse(process.argv[4])
)));
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            harness,
            str(CONTROLLER_PATH),
            json.dumps(evaluation),
            str(schema_version),
            json.dumps(costs or {}),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


class ControllerScoringTests(unittest.TestCase):
    def test_controller_outcome_uses_average_dimension_percentages(self) -> None:
        evaluation = {
            "candidate_mapping": {"A": "claude", "B": "codex"},
            "totals": {"A": 0.821, "B": 0.319},
        }

        outcome = _render_outcome_summary(evaluation)

        self.assertEqual(outcome["headline"], "LLM judge: Historical Claude")
        self.assertEqual(outcome["score"], "Historical Claude: 82%\nCodex replay: 32%")
        self.assertIn("average applicable dimension percentages", outcome["detail"])
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": None, "B": None}})["score"],
            "No validated score",
        )
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": 0.821, "B": 0.824}})["headline"],
            "LLM judge: Tie",
        )
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": 0.125, "B": 0.124}})["score"],
            "Historical Claude: 13%\nCodex replay: 12%",
        )

    def test_controller_preserves_legacy_dimension_win_counts(self) -> None:
        outcome = _render_outcome_summary({"totals": {"A": 1, "B": 2}}, schema_version=2)

        self.assertEqual(outcome["headline"], "LLM judge: Codex replay")
        self.assertEqual(outcome["score"], "Historical Claude: 1\nCodex replay: 2")
        self.assertIn("Each dimension win awards one point", outcome["detail"])
        self.assertNotIn("%", outcome["score"])
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": 0, "B": 1}}, schema_version=2)["score"],
            "Historical Claude: 0\nCodex replay: 1",
        )

    def test_judge_and_cost_winners_are_reported_independently(self) -> None:
        cases = (
            (
                {"A": 1, "B": 1},
                {"claude": {"usd": 6.68}, "codex": {"usd": 0.86}},
                "LLM judge: Tie",
                "Cost winner: Codex replay ($6.68 vs $0.86)",
            ),
            (
                {"A": 0.4, "B": 0.9},
                {"claude": {"usd": 0.14}, "codex": {"usd": 0.81}},
                "LLM judge: Codex replay",
                "Cost winner: Historical Claude ($0.14 vs $0.81)",
            ),
            (
                {"A": 0.9, "B": 0.4},
                {"claude": {"usd": 0}, "codex": {"usd": 0}},
                "LLM judge: Historical Claude",
                "Cost winner: Tie ($0.0000 vs $0.0000)",
            ),
        )
        for totals, costs, expected_judge, expected_cost in cases:
            with self.subTest(totals=totals, costs=costs):
                outcome = _render_outcome_summary({"totals": totals}, costs=costs)

                self.assertEqual(outcome["headline"], expected_judge)
                self.assertEqual(outcome["cost"], expected_cost)

    def test_cost_winner_is_unavailable_without_two_valid_costs(self) -> None:
        for costs in ({}, {"claude": {"usd": 1}}, {"claude": {"usd": -1}, "codex": {"usd": 1}}):
            with self.subTest(costs=costs):
                outcome = _render_outcome_summary({"totals": {"A": 1, "B": 1}}, costs=costs)

                self.assertEqual(outcome["cost"], "Cost winner: unavailable")
