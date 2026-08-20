"""Focused browser-controller explanation and privacy regression coverage."""

from __future__ import annotations

import re
import unittest

from test_mcp_server import render_evaluation_table


class ControllerExplanationTests(unittest.TestCase):
    def test_controller_renders_escaped_check_explanations_below_each_candidate_status(
        self,
    ) -> None:
        historical_evidence = 'Summary cites <script>alert("unsafe")</script> & the patch.'
        replay_evidence = "The final response identifies the missing integration test."
        unavailable_evidence = "No execution results were captured."
        historical_token = "sk-1234567890abcdefghijklmnop"
        replay_token = "ghp_1234567890abcdefghijklmnop"
        local_path = "/Users/alice/work/project.py"
        oversized_evidence = f"Credential {historical_token} " + "x" * 320
        tampered_historical_evidence = (
            "Codex left password=hunter12345 in /private/var/work/repo.py"
        )
        sensitive_paths = (
            "/Users/alice",
            "/home/alice",
            "/private/secret",
            "/tmp/secret",
            "/var/secret",
            "/opt/secret",
            "/Applications/secret",
            "/Volumes/secret",
            "/workspace/secret",
            "/workspaces/secret",
            "/root/code/openai/secret",
            "/usr/local/share/secret",
            r"C:\Users\alice\secret",
            r"D:\workspace\secret",
            "E:/source/secret",
        )
        tampered_replay_evidence = (
            "OpenAI ChatGPT Claude Anthropic gpt-5.6-sol Sonnet "
            "API_KEY=private123 auth-token:hidden456 access_token=hidden789 secret=classified "
            + " ".join(sensitive_paths)
        )
        rendered = render_evaluation_table(
            {
                "candidate_mapping": {"A": "claude", "B": "codex"},
                "reviews": [
                    {
                        "evaluator": "codex",
                        "model": "gpt-review",
                        "ballot": {
                            "dimensions": {
                                "accurate_reporting": {
                                    "candidates": {
                                        "A": {
                                            "checks": {
                                                "truthful_summary": 1,
                                                "accurate_outcomes": None,
                                                "disclosed_limitations": 1,
                                                "supported_claims": 1,
                                            },
                                            "explanations": {
                                                "truthful_summary": historical_evidence,
                                                "accurate_outcomes": unavailable_evidence,
                                                "disclosed_limitations": oversized_evidence,
                                                "supported_claims": tampered_historical_evidence,
                                            },
                                            "score": 1,
                                        },
                                        "B": {
                                            "checks": {
                                                "truthful_summary": 0,
                                                "accurate_outcomes": None,
                                                "disclosed_limitations": 0,
                                                "supported_claims": 0,
                                            },
                                            "explanations": {
                                                "truthful_summary": replay_evidence,
                                                "accurate_outcomes": unavailable_evidence,
                                                "disclosed_limitations": (
                                                    f"Observed token {replay_token}; path {local_path}"
                                                ),
                                                "supported_claims": tampered_replay_evidence,
                                            },
                                            "explanation": "PRIVATE REVIEWER NARRATIVE",
                                            "score": 0,
                                        },
                                    },
                                    "explanation": "SENSITIVE REVIEWER NARRATIVE",
                                    "winner": "A",
                                }
                            }
                        },
                    }
                ],
            }
        )

        self.assertIn(
            "<td>Pass<small>Summary cites &lt;script&gt;alert(&quot;unsafe&quot;)"
            "&lt;/script&gt; &amp; the patch.</small></td>",
            rendered,
        )
        self.assertIn(f"<td>Fail<small>{replay_evidence}</small></td>", rendered)
        self.assertEqual(rendered.count(f"<td>N/A<small>{unavailable_evidence}</small></td>"), 2)
        redacted_evidence = ("Credential [REDACTED] " + "x" * 320)[:280]
        self.assertIn(f"<td>Pass<small>{redacted_evidence}</small></td>", rendered)
        self.assertIn(
            "<td>Fail<small>Observed token [REDACTED]; path [REDACTED]</small></td>",
            rendered,
        )
        self.assertIn(
            "<td>Pass<small>[REDACTED] left [REDACTED] in [REDACTED]</small></td>",
            rendered,
        )
        explanation_text = " ".join(re.findall(r"<small>(.*?)</small>", rendered))
        for sensitive_value in (
            "Codex",
            "OpenAI",
            "ChatGPT",
            "Claude",
            "Anthropic",
            "gpt-5.6-sol",
            "Sonnet",
            "hunter12345",
            "private123",
            "hidden456",
            "hidden789",
            "classified",
            *sensitive_paths,
        ):
            with self.subTest(sensitive_value=sensitive_value):
                self.assertNotIn(sensitive_value, explanation_text)
        self.assertNotIn(historical_token, rendered)
        self.assertNotIn(replay_token, rendered)
        self.assertNotIn(local_path, rendered)
        self.assertNotIn("x" * 280, rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("PRIVATE REVIEWER NARRATIVE", rendered)
        self.assertNotIn("SENSITIVE REVIEWER NARRATIVE", rendered)


if __name__ == "__main__":
    unittest.main()
