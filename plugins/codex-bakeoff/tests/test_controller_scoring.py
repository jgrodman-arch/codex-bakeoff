"""Regression coverage for the controller's comparison score display."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import TypedDict

CONTROLLER_PATH = Path(__file__).resolve().parent.parent / "mcp" / "controller.html"


class _MetricSummary(TypedDict):
    winner: str | None
    values: list[dict[str, str]]
    detail: str


class _OutcomeSummary(TypedDict):
    price: _MetricSummary
    quality: _MetricSummary
    summary: str
    score: str
    detail: str


def _render_outcome_summary(
    evaluation: dict[str, object],
    *,
    schema_version: int = 3,
    costs: dict[str, object] | None = None,
) -> _OutcomeSummary:
    harness = r"""
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
require(require("node:path").join(require("node:path").dirname(process.argv[1]), "controller-ranges.js"));
const extract = (start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  if (first < 0 || last <= first) throw new Error(`Missing controller code: ${start}`);
  return source.slice(first, last);
};
const render = new Function([
  extract("      const isObject =", "      const safeJson ="),
  extract("      const formatCost =", "      function unwrapToolResult"),
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

        self.assertEqual(outcome["quality"]["winner"], "Historical Claude")
        self.assertEqual(outcome["score"], "Historical Claude: 82%\nCodex replay: 32%")
        self.assertIn("average applicable dimension percentages", outcome["detail"])
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": None, "B": None}})["score"],
            "No validated score",
        )
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": 0.821, "B": 0.824}})["quality"]["winner"],
            "Tie",
        )
        self.assertEqual(
            _render_outcome_summary({"totals": {"A": 0.125, "B": 0.124}})["score"],
            "Historical Claude: 13%\nCodex replay: 12%",
        )

    def test_controller_preserves_legacy_dimension_win_counts(self) -> None:
        outcome = _render_outcome_summary({"totals": {"A": 1, "B": 2}}, schema_version=2)

        self.assertEqual(outcome["quality"]["winner"], "Codex replay")
        self.assertEqual(outcome["quality"]["detail"], "1 point higher")
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
                "Tie",
                "Codex replay",
                "One metric tied",
            ),
            (
                {"A": 0.4, "B": 0.9},
                {"claude": {"usd": 0.14}, "codex": {"usd": 0.81}},
                "Codex replay",
                "Historical Claude",
                "Split winners",
            ),
            (
                {"A": 0.9, "B": 0.4},
                {"claude": {"usd": 0}, "codex": {"usd": 0}},
                "Historical Claude",
                "Tie",
                "One metric tied",
            ),
        )
        for totals, costs, expected_judge, expected_cost, summary in cases:
            with self.subTest(totals=totals, costs=costs):
                outcome = _render_outcome_summary({"totals": totals}, costs=costs)

                self.assertEqual(outcome["quality"]["winner"], expected_judge)
                self.assertEqual(outcome["price"]["winner"], expected_cost)
                self.assertEqual(outcome["summary"], summary)

    def test_cost_winner_is_unavailable_without_two_valid_costs(self) -> None:
        for costs in (
            {},
            {"claude": {"usd": 1}},
            {"claude": {"usd": -1}, "codex": {"usd": 1}},
            {"claude": {"usd": "0.01"}, "codex": {"usd": 1}},
        ):
            with self.subTest(costs=costs):
                outcome = _render_outcome_summary({"totals": {"A": 1, "B": 1}}, costs=costs)

                self.assertIsNone(outcome["price"]["winner"])
                self.assertEqual(outcome["quality"]["winner"], "Tie")
                self.assertEqual(outcome["summary"], "Partial comparison")

    def test_shared_usage_does_not_rank_cost_even_with_a_numeric_estimate(self) -> None:
        outcome = _render_outcome_summary(
            {"totals": {"A": 1, "B": 1}},
            costs={"claude": {"status": "shared", "usd": 0}, "codex": {"usd": 1}},
        )
        self.assertEqual(outcome["quality"]["winner"], "Tie")
        self.assertIsNone(outcome["price"]["winner"])
        self.assertIn("No standalone usage", outcome["price"]["detail"])
        self.assertEqual(outcome["price"]["values"][0]["value"], "Shared usage")

    def test_split_winners_show_labeled_values_and_differences(self) -> None:
        outcome = _render_outcome_summary(
            {"totals": {"A": 1, "B": 0.8}},
            costs={"claude": {"usd": 0.26}, "codex": {"usd": 0.01}},
        )
        self.assertEqual(outcome["summary"], "Split winners")
        self.assertEqual(outcome["price"]["winner"], "Codex replay")
        self.assertEqual(outcome["price"]["detail"], "96% lower estimated cost")
        self.assertEqual(
            outcome["price"]["values"],
            [
                {"name": "Historical Claude", "value": "$0.26"},
                {"name": "Codex replay", "value": "$0.01"},
            ],
        )
        self.assertEqual(outcome["quality"]["winner"], "Historical Claude")
        self.assertEqual(outcome["quality"]["detail"], "20 percentage points higher")
        self.assertEqual(
            outcome["quality"]["values"],
            [
                {"name": "Historical Claude", "value": "100%"},
                {"name": "Codex replay", "value": "80%"},
            ],
        )

    def test_quality_uses_revealed_candidate_mapping(self) -> None:
        outcome = _render_outcome_summary(
            {"candidate_mapping": {"A": "codex", "B": "claude"}, "totals": {"A": 1, "B": 0.8}},
            costs={"claude": {"usd": 0.26}, "codex": {"usd": 0.01}},
        )
        self.assertEqual(outcome["quality"]["winner"], "Codex replay")
        self.assertEqual(outcome["quality"]["values"][0], {"name": "Codex replay", "value": "100%"})
        self.assertEqual(outcome["summary"], "Codex replay wins both")

    def test_unavailable_quality_does_not_hide_price_winner_or_partial_score(self) -> None:
        outcome = _render_outcome_summary(
            {"totals": {"A": None, "B": 0.8}},
            costs={"claude": {"usd": 0.26}, "codex": {"usd": 0.01}},
        )
        self.assertIsNone(outcome["quality"]["winner"])
        self.assertEqual(outcome["quality"]["values"][0]["value"], "Unavailable")
        self.assertEqual(outcome["quality"]["values"][1]["value"], "80%")
        self.assertEqual(outcome["price"]["winner"], "Codex replay")
        self.assertEqual(outcome["summary"], "Partial comparison")

    def test_both_metrics_can_tie_including_zero_cost(self) -> None:
        outcome = _render_outcome_summary(
            {"totals": {"A": 0.801, "B": 0.804}},
            costs={"claude": {"usd": 0}, "codex": {"usd": 0}},
        )
        self.assertEqual(outcome["summary"], "Price and quality tied")
        self.assertEqual(outcome["price"]["detail"], "Same estimated cost")
        self.assertEqual(outcome["quality"]["detail"], "Same quality score")

    def test_cost_difference_rounding_does_not_claim_zero_or_total_savings(self) -> None:
        for replay, expected in ((0.999, "<1%"), (0.001, ">99%"), (0, "100%")):
            with self.subTest(replay=replay):
                outcome = _render_outcome_summary(
                    {}, costs={"claude": {"usd": 1}, "codex": {"usd": replay}}
                )
                self.assertEqual(outcome["price"]["winner"], "Codex replay")
                self.assertEqual(outcome["price"]["detail"], f"{expected} lower estimated cost")
