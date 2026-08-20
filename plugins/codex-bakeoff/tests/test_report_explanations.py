"""Regression coverage for evidence-backed explanations in exported reports."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "report_explanations_execution", ROOT / "scripts" / "historical_execution.py"
)
assert SPEC is not None and SPEC.loader is not None
execution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = execution
SPEC.loader.exec_module(execution)


def _review_ballot(*, include_explanations: bool = True) -> dict[str, object]:
    dimensions: dict[str, object] = {}
    for dimension, checks in execution.REVIEW_DIMENSION_CHECKS.items():
        candidates: dict[str, object] = {}
        for label in ("A", "B"):
            candidate: dict[str, object] = {"checks": {check: 1 for check in checks}}
            if include_explanations:
                candidate["explanations"] = {
                    check: f"Candidate {label} supplied evidence for {check}." for check in checks
                }
            candidates[label] = candidate
        dimensions[dimension] = {"candidates": candidates}
    return {"dimensions": dimensions}


class ReportExplanationTests(unittest.TestCase):
    def _report(self, ballot: dict[str, object]) -> dict[str, object]:
        evaluation = execution.aggregate_reviews(
            [{"evaluator": "codex", "model": "gpt-test", "ballot": ballot}]
        )
        evaluation["candidate_mapping"] = {"A": "claude", "B": "codex"}
        return {"schema_version": 3, "evaluation": evaluation}

    def test_exported_report_renders_escaped_explanations_beneath_each_result(self) -> None:
        ballot = _review_ballot()
        candidates = ballot["dimensions"]["request_fulfillment"]["candidates"]
        candidates["A"]["checks"]["required_behavior"] = 0
        candidates["A"]["explanations"]["required_behavior"] = (
            'Missing <script>alert("unsafe")</script> behavior.'
        )
        candidates["B"]["explanations"]["required_behavior"] = (
            "Implemented the requested behavior in app.py."
        )
        candidates["A"]["checks"]["stated_constraints"] = None
        candidates["A"]["explanations"]["stated_constraints"] = (
            "The request did not establish an applicable constraint."
        )

        rendered = execution.render_report_html(self._report(ballot))

        self.assertIn(
            '<strong>Fail</strong><span class="review-explanation">'
            "Missing &lt;script&gt;alert(&quot;unsafe&quot;)&lt;/script&gt; behavior.</span>",
            rendered,
        )
        self.assertIn(
            '<strong>Pass</strong><span class="review-explanation">'
            "Implemented the requested behavior in app.py.</span>",
            rendered,
        )
        self.assertIn(
            '<strong>N/A</strong><span class="review-explanation">'
            "The request did not establish an applicable constraint.</span>",
            rendered,
        )
        self.assertNotIn('<script>alert("unsafe")</script>', rendered)

    def test_legacy_ballots_render_without_explanation_markup(self) -> None:
        rendered = execution.render_report_html(
            self._report(_review_ballot(include_explanations=False))
        )

        self.assertIn("Required behavior implemented: <strong>Pass</strong></li>", rendered)
        self.assertNotIn('<span class="review-explanation">', rendered)

    def test_exported_report_redacts_explanations_from_untrusted_report_data(self) -> None:
        report = self._report(_review_ballot())
        token = "sk-abcdefghijklmnopqrstuvwxyz"
        report["evaluation"]["reviews"][0]["ballot"]["dimensions"]["request_fulfillment"][
            "candidates"
        ]["A"]["explanations"]["required_behavior"] = f"Observed token {token}."

        rendered = execution.render_report_html(report)

        self.assertIn("Observed token [REDACTED_API_KEY].", rendered)
        self.assertNotIn(token, rendered)

    def test_exported_report_redacts_private_paths_and_agent_identities(self) -> None:
        for private_path in (
            "/Users/alice/private/repository.py",
            "/root/code/openai/private.py",
            "/usr/local/share/private.py",
            r"C:\Users\alice\private.py",
            "D:/source/private.py",
        ):
            with self.subTest(private_path=private_path):
                report = self._report(_review_ballot())
                report["evaluation"]["reviews"][0]["ballot"]["dimensions"]["request_fulfillment"][
                    "candidates"
                ]["A"]["explanations"][
                    "required_behavior"
                ] = f"Codex modified {private_path} while implementing the request."

                rendered = execution.render_report_html(report)

                self.assertIn(
                    "[REDACTED_AGENT] modified [REDACTED_PATH] while implementing the request.",
                    rendered,
                )
                self.assertNotIn(private_path, rendered)
                self.assertNotIn("Codex modified", rendered)


if __name__ == "__main__":
    unittest.main()
