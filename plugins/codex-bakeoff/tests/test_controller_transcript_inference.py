"""Behavioral coverage for transcript-backed file attribution visibility."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

CONTROLLER_PATH = Path(__file__).resolve().parent.parent / "mcp" / "controller.html"
HARNESS = r"""
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
require(require("node:path").join(require("node:path").dirname(process.argv[1]), "controller-ranges.js"));
const extract = (start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  if (first < 0 || last <= first) throw new Error("Missing controller code");
  return source.slice(first, last);
};
const render = new Function("document", [
  extract("      const STEPS =", '      app.addEventListener("click"'),
  "return (values) => { Object.assign(state, values); return renderConfigureStep(); };",
].join("\n"))({getElementById: () => null});
process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map(render)));
"""


class ControllerTranscriptInferenceTests(unittest.TestCase):
    def test_non_git_outputs_are_visible_and_git_additions_stay_in_details(self) -> None:
        draft = {
            "thread_id": "thread-1",
            "source_path": "/tmp/transcript.jsonl",
            "message_uuid": "message-1",
            "request": "Fix the task",
            "repo": "/tmp/project",
            "beginning_kind": "non_git",
            "ending_kind": "non_git",
            "baseline_commit": "",
            "ending_commit": "",
            "models": ["gpt-5.6-sol"],
        }
        inferred_selection = {
            "candidates": [
                {"path": "created.html", "selectable": True},
                {"path": "local.txt", "selectable": True},
            ],
            "transcript_inferred_files": ["created.html"],
            "transcript_inferred_file_count": 1,
        }
        inputs = [
            {
                "reviewDraft": draft,
                "inspection": {
                    "replay": {"message_uuid": "message-1"},
                    "baseline": {
                        "beginning_kind": "non_git",
                        "ending_kind": "non_git",
                        "repository": "/tmp/project",
                    },
                    "file_selection": {
                        **inferred_selection,
                        "source_kind": "non_git",
                        "classifications": {
                            "created_by_claude": [{"path": "created.html"}],
                            "exclude": [{"path": "local.txt"}],
                        },
                    },
                },
                "classifications": {"created.html": "claude", "local.txt": "exclude"},
            },
            {
                "reviewDraft": {
                    **draft,
                    "beginning_kind": "git",
                    "ending_kind": "git",
                    "baseline_commit": "abc123",
                    "ending_commit": "def456",
                },
                "inspection": {
                    "replay": {"message_uuid": "message-1"},
                    "baseline": {
                        "beginning_kind": "git",
                        "ending_kind": "git",
                        "repository": "/tmp/project",
                        "commit": "abc123",
                        "ending_commit": "def456",
                    },
                    "file_selection": {
                        **inferred_selection,
                        "source_kind": "git",
                        "claude_output_changes": [{"path": "created.html"}],
                    },
                },
                "classifications": {"created.html": "claude", "local.txt": "exclude"},
            },
            {
                "reviewDraft": {
                    **draft,
                    "beginning_kind": "git",
                    "ending_kind": "git",
                    "baseline_commit": "abc123",
                    "ending_commit": "def456",
                },
                "inspection": {
                    "replay": {"message_uuid": "message-1"},
                    "baseline": {
                        "beginning_kind": "git",
                        "ending_kind": "git",
                        "repository": "/tmp/project",
                        "commit": "abc123",
                        "ending_commit": "def456",
                    },
                    "file_selection": {
                        "source_kind": "git",
                        "candidates": [],
                        "transcript_inferred_files": [],
                        "transcript_inferred_file_count": 0,
                    },
                },
                "classifications": {},
            },
        ]
        for values in inputs:
            values.update(
                {
                    "models": [{"id": "gpt-5.6-sol", "label": "Sol"}],
                    "promptGeneration": "llm_synthesis",
                    "workingDirectoryLoading": False,
                }
            )
        result = subprocess.run(
            ["node", "-e", HARNESS, str(CONTROLLER_PATH), json.dumps(inputs)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        rendered = json.loads(result.stdout)
        for index, html in enumerate(rendered[:2]):
            inline, details = html.split('<details id="configuration-details"', 1)
            self.assertIn("Included 1 file inferred from the Claude transcript.", inline)
            self.assertEqual('id="files-heading"' in inline, index == 0)
            self.assertEqual('id="files-heading"' in details, index == 1)
            self.assertNotIn(" open>", details.split("<summary", 1)[0])
        self.assertNotIn("inferred from the Claude transcript", rendered[2])
        self.assertNotIn('id="files-heading"', rendered[2])


if __name__ == "__main__":
    unittest.main()
