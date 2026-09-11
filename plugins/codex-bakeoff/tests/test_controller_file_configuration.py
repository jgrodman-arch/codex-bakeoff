"""File controls remain reachable after editing historical state."""

from __future__ import annotations

import subprocess
import unittest

from test_controller_range_ui import CONTROLLER, HARNESS

FILE_SCENARIOS = (
    HARNESS.split("async function main() {", 1)[0]
    + r"""
async function main() {
  const c = await configured();
  const original = c.state.inspection;
  if (process.argv[2] === "visible") {
    c.state.configurationDetailsOpen = false;
    const html = c.renderConfigureStep();
    assert.ok(html.indexOf('data-file-kind="non_git"') < html.indexOf('id="configuration-details"'));
    assert.deepEqual(c.reviewPayload().created_by_claude, ["seed.txt", "result.txt"]);
    c.state.classifications["result.txt"] = "exclude";
    assert.deepEqual(c.reviewPayload().created_by_claude, ["seed.txt"]);
    assert.deepEqual(c.reviewPayload().excluded_files, ["result.txt"]);
  } else {
    Object.assign(c.state.inspection, {
      baseline: {...original.baseline, ending_kind: "git", ending_commit: "abc1234"},
      file_selection: {source_kind: "git", candidates: [], classifications: {}},
    });
    c.state.reviewDraft.ending_kind = "git";
    c.state.reviewDraft.ending_commit = "abc1234";
    assert.equal(c.selectionNeedsRefresh(), false);
    c.state.reviewDraft.ending_commit = "def5678";
    assert.equal(c.selectionNeedsRefresh(), true);
    c.state.reviewDraft.ending_kind = "non_git";
    c.state.reviewDraft.ending_commit = "";
    assert.ok(c.renderConfigureStep().includes('data-action="refresh-files"'));
    let refreshed;
    c.setTool(async (name, args) => {
      assert.equal(name, "prepare_run");
      refreshed = args;
      return {...inspection(args), ready: false};
    });
    await c.refreshAttributionAndContinue();
    assert.equal(refreshed.ending_kind, "non_git");
    assert.equal(refreshed.confirm_file_selection, false);
    assert.equal(c.selectionNeedsRefresh(), false);
    assert.ok(c.renderConfigureStep().includes('data-file-kind="non_git"'));
    assert.deepEqual(c.reviewPayload().created_by_claude, ["seed.txt", "result.txt"]);
    assert.equal(c.state.preparation, null);
    assert.equal(c.state.runId, "");
  }
}
main().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });
"""
)


class ControllerFileConfigurationTests(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        result = subprocess.run(
            ["node", "-e", FILE_SCENARIOS, str(CONTROLLER), scenario],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inferred_non_git_outputs_can_be_edited_without_opening_details(self) -> None:
        self.run_scenario("visible")

    def test_git_to_non_git_edit_refreshes_empty_file_discovery_without_starting(self) -> None:
        self.run_scenario("refresh")
