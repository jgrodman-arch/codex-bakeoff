"""Multiple-thread behavior over the controller's existing configuration and reports."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "replay_range_test_harness", Path(__file__).with_name("test_controller_range_ui.py")
)
if _spec is None or _spec.loader is None:
    raise AssertionError("Cannot load the local controller test harness.")
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
CONTROLLER = _harness.CONTROLLER
HARNESS = (
    _harness.HARNESS.replace(
        "return {state, initialize, selectThread,",
        "return {state, initialize, selectThread, toggleThreadSelection, configureSelectedThreads, "
        "selectBatchThread, startThreadBatch, aggregateModelResults, selectThreadResult, "
        "renderAggregateResults, renderThreadStep,",
    )
    .replace(
        "schedulePoll = () => {};",
        "schedulePoll = () => { state.scheduled = (state.scheduled || 0) + 1; };",
    )
    .replace("async function main() {", "async function rangeMain() {")
    .replace(
        "main().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });", ""
    )
    + r"""
const threads = [thread, {...thread, imported_thread_id: "thread-2", title: "Second thread", source_path: "/tmp/second.jsonl"}];
let batchStarts = 0, reportReads = 0;
function batchController() {
  const c = make();
  const original = c.setTool(async (name, args) => {
    if (name === "list_threads") return {threads, total: threads.length};
    if (name === "prepare_run" && args.configurations) {
      return {ready: true, run_config: args, prepare_token: "batch-token"};
    }
    if (name === "start_run" && args.configurations) {
      batchStarts += 1;
      const launched = args.configurations.flatMap(config => config.models.map(model => {
        const run = {...config, model, run_id: `batch-${++runCounter}`, controller_session_id: "session-1",
          batch_id: "batch-1", status: "running", phase: "queued"};
        runs.set(run.run_id, run);
        return {run, model};
      }));
      return {runs: launched};
    }
    if (name === "get_report") {
      reportReads += 1;
      return {report: {original_request: runs.get(args.run_id).request,
        evaluation: {totals: {A: .9, B: .8}, candidate_mapping: {A: "codex", B: "claude"}},
        estimated_cost: {claude: {usd: 2}, codex: {usd: 1}}}};
    }
    const result = await original(name, args);
    if (["inspect_thread", "prepare_run"].includes(name)) {
      result.replay = {...result.replay, ...threads.find(item => item.imported_thread_id === args.thread_id)};
    }
    return result;
  });
  return c;
}
async function selected() {
  const c = batchController();
  await c.initialize();
  c.toggleThreadSelection("thread-1", 1);
  c.toggleThreadSelection("thread-2", 2);
  await c.configureSelectedThreads();
  return c;
}
async function main() {
  const scenario = process.argv[2];
  if (scenario === "failed_reload") {
    runs.set("failed-child", {run_id: "failed-child", controller_session_id: "session-1", batch_id: "failed-batch",
      thread_id: "thread-1", model: "model-1", status: "failed", launch_failed: true});
    const c = batchController();
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "get_state") result.state.batch = {id: "failed-batch", thread_count: 2,
        models: ["model-1"], starting: false,
        threads: threads.map(item => ({thread_id: item.imported_thread_id, thread_title: item.title})),
        errors: threads.map(item => ({thread_id: item.imported_thread_id, thread_title: item.title, model: "model-1", error: "Launch failed"}))};
      return result;
    });
    await c.initialize();
    assert.equal(c.state.step, "run");
    assert.equal(c.state.batchId, "failed-batch");
    assert.equal(c.state.runErrors.length, 2);
    assert.equal(c.state.runs.length, 0);
    assert.equal(c.canNavigateTo("thread"), false);
    assert.equal(c.canNavigateTo("configure"), false);
    await c.startThreadBatch();
    assert.equal(batchStarts, 0);
    await c.refreshRun();
    assert.equal(c.state.batchId, "failed-batch");
    assert.equal(c.state.runs.length, 0);
    assert.equal(c.aggregateModelResults()[0].failed, 2);
    return;
  }
  if (scenario === "manifest_membership") {
    availableModels.push({id: "model-2", label: "Other model"});
    for (const [index, item] of threads.entries()) runs.set(`manifest-${index}`, {
      run_id: `manifest-${index}`, controller_session_id: "session-1", batch_id: "batch-1",
      thread_id: item.imported_thread_id, thread_title: item.title, model: "model-1",
      status: index ? "failed" : "completed", request: "Original request",
    });
    const c = batchController();
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "get_state") result.state.batch = {id: "batch-1", thread_count: 2,
        models: ["model-1", "model-2"], starting: false,
        threads: threads.map(item => ({thread_id: item.imported_thread_id, thread_title: item.title})),
        errors: threads.map(item => ({thread_id: item.imported_thread_id, thread_title: item.title, model: "model-2", error: "Launch failed"}))};
      return result;
    });
    await c.initialize();
    await c.refreshRun();
    assert.deepEqual(c.state.selectedModels, ["model-1", "model-2"]);
    assert.equal(c.state.runErrors.length, 2);
    assert.equal(c.state.selectedThreads.length, 2);
    const failed = c.aggregateModelResults().find(entry => entry.model === "model-2");
    assert.deepEqual([failed.total, failed.ready, failed.failed], [2, 0, 2]);
    assert.ok(c.renderAggregateResults().includes("2 of 2 threads finished"));
    assert.ok(c.renderAggregateResults().includes("1 of 2 comparisons ready"));
    assert.equal(c.renderAggregateResults().includes("1 of 1 comparisons ready"), false);
    return;
  }
  if (scenario === "selection") {
    const c = batchController();
    await c.initialize();
    await c.configureSelectedThreads();
    assert.equal(c.state.step, "thread");
    assert.equal(calls.some(item => item.name === "inspect_thread"), false);
    c.toggleThreadSelection("thread-1", 1);
    await c.configureSelectedThreads();
    assert.equal(c.state.selectedThreadId, "thread-1");
    assert.ok(c.renderConfigureStep().includes("Split into chunks"));
    await c.changeReplayMode("chunks");
    c.state.step = "thread";
    await c.configureSelectedThreads();
    assert.equal(c.state.replayMode, "chunks");
    c.state.step = "thread";
    c.toggleThreadSelection("thread-1", 1);
    await c.configureSelectedThreads();
    assert.equal(c.state.step, "thread");
    assert.equal(c.state.selectedThreads.length, 0);
    assert.equal(c.canNavigateTo("configure"), false);
    return;
  }
  if (scenario === "chunks_to_batch") {
    const c = batchController();
    await c.initialize();
    c.toggleThreadSelection("thread-1", 1);
    await c.configureSelectedThreads();
    c.state.reviewDraft.request = "Edited whole thread task";
    await c.changeReplayMode("chunks");
    await c.toggleDivider("u2");
    await c.editReplayUnit(c.replayUnits()[1].key);
    c.state.reviewDraft.request = "Only the later chunk";
    assert.equal(c.state.reviewDraft.message_uuid, "u2");
    c.state.step = "thread";
    c.toggleThreadSelection("thread-2", 2);
    await c.configureSelectedThreads();
    assert.equal(c.state.replayMode, "whole");
    assert.equal(c.state.reviewDraft.request, "Edited whole thread task");
    assert.equal(c.state.reviewDraft.message_uuid, "u1");
    await c.startThreadBatch();
    assert.equal(c.state.runs[0].run.request, "Edited whole thread task");
    assert.equal(c.state.runs[0].run.message_uuid, "u1");
    assert.equal("start_message_uuid" in c.state.runs[0].run, false);
    return;
  }
  const c = await selected();
  assert.equal(c.state.selectedThreads.length, 2);
  assert.equal(c.renderConfigureStep().includes("Split into chunks"), false);
  c.state.reviewDraft.request = "Edited first request";
  c.state.classifications["seed.txt"] = "exclude";
  c.state.attributionEdits = ["seed.txt"];
  await c.selectBatchThread("thread-2");
  c.state.reviewDraft.request = "Edited second request";
  await c.selectBatchThread("thread-1");
  assert.equal(c.state.reviewDraft.request, "Edited first request");
  assert.equal(c.state.classifications["seed.txt"], "exclude");
  assert.deepEqual(c.state.selectedModels, ["model-1"]);
  if (scenario === "inspection_failure") {
    const original = c.setTool(async (name, args) => {
      if (name === "inspect_thread" && args.thread_id === "thread-2") throw new Error("Temporary inspection failure");
      return original(name, args);
    });
    await c.selectBatchThread("thread-2");
    assert.equal(c.state.reviewDraft.request, "Edited second request");
    assert.ok(c.state.error.includes("Temporary inspection failure"));
    c.saveDraft();
    const reloaded = batchController();
    const reloadTool = reloaded.setTool(async (name, args) => {
      if (name === "inspect_thread") throw new Error("Inspection still unavailable after reload");
      return reloadTool(name, args);
    });
    await reloaded.initialize();
    assert.equal(reloaded.state.reviewDraft.request, "Edited second request");
    assert.deepEqual(reloaded.state.reviewDraft.models, ["model-1"]);
    return;
  }
  if (scenario === "draft_reload") {
    c.saveDraft();
    const reloaded = batchController();
    await reloaded.initialize();
    assert.equal(reloaded.state.selectedThreads.length, 2);
    assert.equal(reloaded.state.reviewDraft.request, "Edited first request");
    await reloaded.selectBatchThread("thread-2");
    assert.equal(reloaded.state.reviewDraft.request, "Edited second request");
    return;
  }
  if (scenario === "blocked") {
    const original = c.setTool(async (name, args) => {
      if (name === "prepare_run" && args.configurations) return {ready: false,
        preparations: [{thread_id: "thread-1", preparation: {ready: true}},
          {thread_id: "thread-2", preparation: {ready: false, blockers: ["Historical files changed"]}}]};
      return original(name, args);
    });
    await c.startThreadBatch();
    assert.equal(batchStarts, 0);
    assert.equal(c.state.selectedThreadId, "thread-2");
    assert.ok(c.renderConfigureStep().includes("Historical files changed"));
    return;
  }
  if (scenario === "lost_launch") {
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "start_run") throw new Error("Lost launch response");
      if (name === "get_state") result.state.batch = {id: "batch-1", thread_count: 2, models: ["model-1"], starting: false,
        threads: threads.map(item => ({thread_id: item.imported_thread_id, thread_title: item.title})), errors: []};
      return result;
    });
    await c.startThreadBatch();
    assert.equal(c.state.batchId, "batch-1");
    assert.equal(c.state.step, "run");
    await c.refreshRun();
    assert.equal(c.state.runs.length, 2);
    await c.startThreadBatch();
    assert.equal(batchStarts, 1);
    return;
  }
  if (scenario === "sibling_review") {
    c.state.models.push({id: "model-2", label: "Other model"});
    c.setSelectedModels(["model-1", "model-2"]);
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "get_report") {
        const run = runs.get(args.run_id);
        const siblingFinished = [...runs.values()].some(other => other.thread_id === run.thread_id &&
          other.model === "model-2" && other.status === "completed");
        if (siblingFinished) result.report.evaluation.totals = {A: .7, B: .8};
      }
      return result;
    });
    await c.startThreadBatch();
    const [first, sibling, unrelated] = c.state.runs;
    const complete = entry => runs.set(entry.id, {...runs.get(entry.id), status: "completed"});
    complete(first);
    await c.refreshRun();
    c.selectThreadResult("thread-1");
    assert.equal(first.report.evaluation.totals.A, .9);
    assert.equal(reportReads, 1);
    complete(unrelated);
    await c.refreshRun();
    assert.equal(reportReads, 2);
    assert.equal(first.report.evaluation.totals.A, .9);
    complete(sibling);
    await c.refreshRun();
    assert.equal(reportReads, 4);
    assert.equal(first.report.evaluation.totals.A, .7);
    assert.equal(c.state.runId, first.id);
    await c.refreshRun();
    assert.equal(reportReads, 4);
    const fourth = c.state.runs[3];
    let failOnce = true;
    const beforeRetry = c.state.scheduled || 0;
    const beforeFailure = c.setTool(async (name, args) => {
      if (name === "get_report" && args.run_id === unrelated.id && failOnce) {
        failOnce = false;
        throw new Error("Temporary report read failure");
      }
      return beforeFailure(name, args);
    });
    complete(fourth);
    await c.refreshRun({continuePolling: true});
    assert.equal(unrelated.needsReportRefresh, true);
    assert.ok(c.state.scheduled > beforeRetry);
    assert.ok(c.state.error.includes("Retrying automatically"));
    await c.refreshRun();
    assert.equal(unrelated.needsReportRefresh, false);
    assert.equal(c.state.error, "");
    return;
  }
  await c.startThreadBatch();
  assert.equal(batchStarts, 1);
  assert.equal(c.state.runs.length, 2);
  assert.deepEqual(c.state.runs.map(entry => entry.run.request), ["Edited first request", "Edited second request"]);
  assert.equal(c.state.batchId, "batch-1");
  const first = c.state.runs[0], second = c.state.runs[1];
  runs.get(first.id).status = "completed";
  await c.refreshRun();
  assert.equal(c.state.step, "run");
  assert.equal(c.canNavigateTo("results"), true);
  assert.equal(reportReads, 1);
  assert.ok(c.renderAggregateResults().includes("1 of 2 threads finished"));
  let aggregate = c.aggregateModelResults()[0];
  assert.deepEqual([aggregate.ready, aggregate.paired, aggregate.claude, aggregate.codex], [1, 1, 2, 1]);
  assert.deepEqual(aggregate.quality, {wins: 1, ties: 0, losses: 0, unavailable: 0});
  c.selectThreadResult("thread-1");
  assert.equal(c.state.step, "results");
  assert.equal(c.state.runId, first.id);
  await c.refreshRun();
  assert.equal(reportReads, 1);
  assert.equal(c.state.runId, first.id);
  if (scenario === "partial_reload") {
    const reloaded = batchController();
    await reloaded.initialize();
    assert.equal(reloaded.state.batchId, "batch-1");
    assert.equal(reloaded.state.runs.length, 2);
    await reloaded.refreshRun();
    assert.equal(reloaded.canNavigateTo("results"), true);
    assert.ok(reloaded.renderAggregateResults().includes("1 of 2 threads finished"));
    reloaded.selectThreadResult("thread-1");
    assert.ok(reloaded.renderResultsStep().includes("Edited first request"));
    assert.equal(batchStarts, 1);
    return;
  }
  runs.get(second.id).status = "completed";
  await c.refreshRun();
  assert.equal(reportReads, 2);
  assert.equal(c.state.runId, first.id);
  assert.equal(c.state.batchView, "individual");
  assert.ok(c.renderResultsStep().includes("Edited first request"));
  assert.ok(c.renderAggregateResults().includes("2 of 2 threads finished"));
  assert.deepEqual(c.aggregateModelResults()[0].quality, {wins: 2, ties: 0, losses: 0, unavailable: 0});
  second.report.historical_usage_shared = true;
  first.report.estimated_cost.codex.usd = null;
  aggregate = c.aggregateModelResults()[0];
  assert.equal(aggregate.paired, 0);
  assert.equal(c.renderAggregateResults().includes("$0.0000"), false);
  first.report.estimated_cost = {claude: {usd: 0}, codex: {usd: 0}};
  assert.equal(c.aggregateModelResults()[0].paired, 1);
  assert.ok(c.renderAggregateResults().includes("Cost difference</dt><dd>Unavailable"));
  first.report.evaluation = {totals: {A: .899, B: .901}, candidate_mapping: {A: "codex", B: "claude"}};
  second.report.evaluation = {totals: {A: .8, B: .9}, candidate_mapping: {A: "codex", B: "claude"}};
  assert.deepEqual(c.aggregateModelResults()[0].quality, {wins: 0, ties: 1, losses: 1, unavailable: 0});
}
main().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });
"""
)


class ControllerBatchTests(unittest.TestCase):
    def check_scenario(self, scenario: str) -> None:
        result = subprocess.run(
            ["node", "-e", HARNESS, str(CONTROLLER), scenario],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_one_thread_keeps_chunking_and_zero_selection_cannot_continue(self) -> None:
        self.check_scenario("selection")

    def test_per_thread_edits_survive_switching_and_reload(self) -> None:
        self.check_scenario("draft_reload")

    def test_batch_blocker_opens_the_affected_thread_without_launching(self) -> None:
        self.check_scenario("blocked")

    def test_partial_reports_and_aggregates_update_without_changing_the_open_report(self) -> None:
        self.check_scenario("results")

    def test_reload_recovers_partial_results_without_starting_again(self) -> None:
        self.check_scenario("partial_reload")

    def test_reload_retains_all_failed_batch_errors_and_does_not_start_again(self) -> None:
        self.check_scenario("failed_reload")

    def test_lost_launch_response_recovers_runs_without_starting_again(self) -> None:
        self.check_scenario("lost_launch")

    def test_late_model_sibling_refreshes_its_shared_review_without_reloading_other_threads(
        self,
    ) -> None:
        self.check_scenario("sibling_review")

    def test_manifest_keeps_models_with_only_launch_failures_after_reload(self) -> None:
        self.check_scenario("manifest_membership")

    def test_failed_reinspection_preserves_edited_thread_draft(self) -> None:
        self.check_scenario("inspection_failure")

    def test_converting_chunks_to_multiple_threads_restores_the_whole_thread_draft(self) -> None:
        self.check_scenario("chunks_to_batch")


if __name__ == "__main__":
    unittest.main()
