"""Behavioral coverage for range selection over the existing controller flow."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parent.parent / "mcp" / "controller.html"
HARNESS = r"""
const fs = require("node:fs");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8");
require(require("node:path").join(require("node:path").dirname(process.argv[1]), "controller-ranges.js"));
const code = source.slice(source.indexOf("      const STEPS ="), source.indexOf('      app.addEventListener("click"'));
const turns = [
  {message_uuid: "u1", request: "Create seed.txt"},
  {message_uuid: "u2", request: "Read seed.txt and create result.txt"},
  {message_uuid: "u3", request: "Polish the result"},
];
const thread = {imported_thread_id: "thread-1", title: "Example", source_path: "/tmp/session.jsonl", project_dir: "/tmp/project"};
const storage = new Map(), calls = [], runs = new Map();
let runCounter = 0;
let availableModels = [{id: "model-1", label: "Model"}];
function inspection(args = {}) {
  const beginning = args.beginning_kind || "non_git", ending = args.ending_kind || "non_git";
  return {
    replay: {...thread, project_dir: args.repo || thread.project_dir,
      message_uuid: args.start_message_uuid || "u1", request: args.request || (args.start_message_uuid ? `Task for ${args.start_message_uuid}` : "Whole task"),
      request_generation: {method: "single_user_prompt"}, actionable_user_turns: turns},
    baseline: {kind: beginning === "git" ? "git_commit" : args.confirm_empty_beginning ? "empty_directory" : "unclassified_directory",
      proposed_kind: beginning === "non_git" ? "empty_directory" : "", beginning_kind: beginning,
      ending_kind: ending, commit: args.baseline_commit || "", ending_commit: args.ending_commit || "", attribution_root: args.repo || "/tmp/project"},
    file_selection: {source_kind: ending,
      candidates: [{path: "seed.txt", selectable: true}, {path: "result.txt", selectable: true}],
      classifications: {
        created_by_claude: args.start_message_uuid === "u1" ? ["seed.txt"] : ["seed.txt", "result.txt"],
        exclude: args.start_message_uuid === "u1" ? ["result.txt"] : [],
      }},
  };
}
function make() {
  const controller = new Function("document", "window", "localStorage", `${code}
    let renderObserver;
    render = () => { saveDraft(); if (renderObserver) renderObserver(); };
    schedulePoll = () => {};
    return {state, initialize, selectThread, changeReplayMode, toggleDivider, editReplayUnit,
      replayUnits, reviewPayload, draftSnapshot, saveDraft, startChunks, refreshRun, setSelectedModels,
      renderConfigureStep, earlierFileAttribution, rememberUnitDraft, unitConfigured, allUnitsConfigured, canNavigateTo,
      refreshAttributionAndContinue, selectionNeedsRefresh, advanceChunkQueue,
      renderResultsStep, renderRunVariants, selectChunkResult, activateRun, renderHistoricalOutput,
      renderGlobalMessages, chunkLaunchActive,
      setRenderObserver: (fn) => { renderObserver = fn; },
      setTool: (fn) => { const previous = callTool; callTool = fn; return previous; }};
  `)({getElementById: () => null}, {
    clearTimeout() {}, setTimeout() {}, clearInterval() {}, setInterval() {},
  }, {
    getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  });
  controller.setTool(async (name, args) => {
    calls.push({name, args});
    if (name === "get_state") return {state: {controller_session_id: "session-1", max_parallel_runs: 8, models: availableModels, recent_runs: [...runs.values()]}};
    if (name === "list_threads") return {threads: [thread], total: 1};
    if (name === "inspect_thread") return inspection(args);
    if (name === "infer_working_directory") return {working_directory: "/tmp/project"};
    if (name === "prepare_run") {
      const result = inspection(args);
      if (args.confirm_file_selection) result.file_selection.classifications = {
        created_by_claude: args.created_by_claude || [], exclude: args.excluded_files || [],
      };
      return {...result, ready: true, run_config: args, prepare_token: "approved"};
    }
    if (name === "start_run") {
      const launched = (args.models?.length ? args.models : [args.model || "model-1"]).map((model) => {
        const run = {run_id: `run-${++runCounter}`, controller_session_id: "session-1", status: "running", ...args, model};
        runs.set(run.run_id, run);
        return {run, model};
      });
      return launched.length === 1 ? launched[0] : {runs: launched};
    }
    if (name === "get_run") return {run: runs.get(args.run_id)};
    if (name === "get_report") return {report: {evaluation: {}}, report_json: "/tmp/report.json"};
    throw new Error(`Unexpected tool: ${name}`);
  });
  return controller;
}
async function configured() {
  const controller = make();
  await controller.initialize();
  await controller.selectThread("thread-1", 1);
  return controller;
}
async function main() {
  const c = await configured();
  assert.equal(c.state.replayMode, "whole");
  assert.equal(c.reviewPayload().message_uuid, "u1");
  assert.equal("start_message_uuid" in c.reviewPayload(), false);
  await c.changeReplayMode("chunks");
  assert.equal("selectedUnitKeys" in c.state, false);
  assert.equal(c.allUnitsConfigured(), true);
  await c.toggleDivider("u2");
  const [first, later] = c.replayUnits();
  assert.equal(first.start_message_uuid, "u1");
  assert.equal(first.end_message_uuid, "u1");
  assert.equal(later.start_message_uuid, "u2");
  assert.equal(later.end_message_uuid, "u3");
  assert.equal("selectedUnitKeys" in c.draftSnapshot(), false);
  assert.equal(c.renderConfigureStep().includes("data-unit-selection"), false);
  assert.equal(c.unitConfigured(first), true);
  assert.equal(c.unitConfigured(later), false);
  assert.equal(c.allUnitsConfigured(), false);

  if (process.argv[2] === "shared_usage") {
    const report = {
      historical_usage_shared: true, historical_model_request_seconds: null,
      estimated_cost: {claude: {status: "shared", usd: null}, codex: {usd: .0075}},
      normalized_usage: {claude: {total_input_tokens: 0}},
      codex_execution: {elapsed_seconds: 40},
      evaluation: {totals: {A: 1, B: 1}},
    };
    c.state.runs = [{id: "shared", unitNumber: 1, model: "alpha", report, run: {status: "completed"}}];
    c.activateRun("shared");
    for (const seconds of [null, 68]) {
      report.historical_model_request_seconds = seconds;
      const html = c.renderResultsStep();
      assert.ok(html.includes("No standalone usage"));
      assert.ok(html.includes("shared across chunks"));
      assert.ok(html.includes("$0.0075"));
      assert.ok(html.includes("40s"));
      assert.match(html, /id="price-heading"[^>]*>Price<\/h2>\s*<p[^>]*>Unavailable<\/p>/);
      assert.equal(html.includes("$0.0000"), false);
      assert.equal(html.includes(">0s<"), false);
      assert.equal(html.includes("1m 8s"), false);
      assert.equal(/class="[^"]*(?:metric--|v-)(?:better|worse)/.test(html), false);
    }
    report.historical_usage_shared = false;
    report.estimated_cost.claude = {status: "estimated", usd: 0};
    report.historical_model_request_seconds = null;
    let html = c.renderResultsStep();
    assert.match(html, /id="price-heading"[^>]*>Price winner<\/h2>\s*<p[^>]*>Historical Claude<\/p>/);
    assert.equal(html.includes(">0s<"), false);
    assert.ok(html.includes("Unavailable"));
    report.historical_model_request_seconds = 0;
    html = c.renderResultsStep();
    assert.ok(html.includes(">0s<"));
  } else if (["inspect_reload", "inspect_abort"].includes(process.argv[2])) {
    c.state.reviewDraft.request = "Edited first chunk";
    c.saveDraft();
    assert.equal(c.state.classifications["result.txt"], "exclude");
    await c.changeReplayMode("whole");
    let completeInspection, abortInspection;
    c.setTool((name, args) => {
      assert.equal(name, "inspect_thread");
      return new Promise((resolve, reject) => {
        completeInspection = () => resolve(inspection(args));
        abortInspection = () => reject(new Error("Navigation interrupted inspection"));
      });
    });
    const switching = c.changeReplayMode("chunks");
    assert.equal(c.state.busy, "inspection");
    if (process.argv[2] === "inspect_abort") {
      abortInspection();
      await switching;
      assert.ok(c.renderGlobalMessages().includes('class="banner banner--error"'));
    }
    const restored = make();
    await restored.initialize();
    assert.equal(restored.state.reviewDraft.request, "Edited first chunk");
    assert.deepEqual(restored.reviewPayload().created_by_claude, ["seed.txt"]);
    assert.deepEqual(restored.reviewPayload().excluded_files, ["result.txt"]);
    completeInspection();
    await switching;
  } else if (process.argv[2] === "persistence") {
    await c.editReplayUnit(later.key);
    Object.assign(c.state.reviewDraft, {request: "My edited handoff", repo: "/tmp/edited", beginning_kind: "git", ending_kind: "git", baseline_commit: "abc123", ending_commit: "def456", message_uuid: "disagreeing-id"});
    c.saveDraft();
    const payload = c.reviewPayload();
    assert.equal(payload.start_message_uuid, "u2");
    assert.equal(payload.end_message_uuid, "u3");
    assert.equal("message_uuid" in payload, false);
    assert.equal(c.renderConfigureStep().includes('id="review-message-uuid"'), false);
    await c.changeReplayMode("whole");
    assert.equal(c.reviewPayload().message_uuid, "u1");
    await c.changeReplayMode("chunks");
    assert.equal(c.state.reviewDraft.request, "My edited handoff");
    assert.equal(c.allUnitsConfigured(), true);
    const storageKey = [...storage.keys()].find((key) => key.includes("draft"));
    const oldDraft = JSON.parse(storage.get(storageKey));
    oldDraft.selectedUnitKeys = [first.key];
    storage.set(storageKey, JSON.stringify(oldDraft));
    const restored = make();
    await restored.initialize();
    assert.equal(restored.state.replayMode, "chunks");
    assert.deepEqual(restored.state.dividers, ["u2"]);
    assert.equal("selectedUnitKeys" in restored.state, false);
    assert.equal("selectedUnitKeys" in restored.draftSnapshot(), false);
    assert.equal(restored.allUnitsConfigured(), true);
    assert.equal(restored.state.reviewDraft.request, "My edited handoff");
    assert.equal(restored.state.reviewDraft.repo, "/tmp/edited");
    assert.equal(restored.state.reviewDraft.baseline_commit, "abc123");
    assert.equal(restored.state.reviewDraft.ending_commit, "def456");
  } else if (process.argv[2] === "changed_inference") {
    const explicit = ["manual-include.txt", "manual-exclude.txt"];
    c.state.inspection.file_selection.candidates.push(...explicit.map((path) => ({path, selectable: true})));
    Object.assign(c.state.classifications, {"manual-include.txt": "claude", "manual-exclude.txt": "exclude"});
    c.state.attributionEdits = explicit;
    c.state.reviewDraft.request = "Keep my handoff";
    c.saveDraft();
    function useChangedInference(controller) {
      const original = controller.setTool(async (name, args) => {
        const result = await original(name, args);
        if (name === "inspect_thread" && args.start_message_uuid === "u1") {
          result.file_selection.candidates.push(...explicit.map((path) => ({path, selectable: true})));
          result.file_selection.classifications = {
            created_by_claude: ["result.txt", "manual-exclude.txt"],
            exclude: ["seed.txt", "manual-include.txt"],
          };
        }
        return result;
      });
    }
    function verify(controller) {
      assert.deepEqual({...controller.state.classifications}, {
        "seed.txt": "exclude", "result.txt": "claude",
        "manual-include.txt": "claude", "manual-exclude.txt": "exclude",
      });
      assert.deepEqual(controller.state.attributionEdits, explicit);
      assert.equal(controller.state.reviewDraft.request, "Keep my handoff");
      assert.deepEqual(controller.reviewPayload().created_by_claude.sort(), ["manual-include.txt", "result.txt"]);
      assert.deepEqual(controller.reviewPayload().excluded_files.sort(), ["manual-exclude.txt", "seed.txt"]);
    }
    useChangedInference(c);
    await c.editReplayUnit(later.key);
    const downstream = calls.filter(({name}) => name === "inspect_thread").at(-1).args;
    assert.deepEqual(downstream.carried_forward_files, ["manual-include.txt"]);
    assert.deepEqual(downstream.excluded_files, ["manual-exclude.txt"]);
    await c.editReplayUnit(first.key);
    verify(c);
    await c.changeReplayMode("whole");
    await c.changeReplayMode("chunks");
    verify(c);
    const restored = make();
    useChangedInference(restored);
    await restored.initialize();
    verify(restored);
  } else if (process.argv[2] === "carry") {
    c.state.classifications = {"seed.txt": "claude", "ignored.txt": "exclude", "future.txt": "exclude"};
    c.state.attributionEdits = ["seed.txt", "ignored.txt"];
    await c.editReplayUnit(later.key);
    const last = calls.filter(({name}) => name === "inspect_thread").at(-1).args;
    assert.deepEqual(last.carried_forward_files, ["seed.txt"]);
    assert.deepEqual(last.excluded_files, ["ignored.txt"]);
    assert.deepEqual(c.reviewPayload().carried_forward_files, ["seed.txt"]);
    c.state.inspection.file_selection = {
      source_kind: "non_git", before_files: [{path: "seed.txt"}],
      candidates: [{path: "seed.txt", selectable: true}, {path: "result.txt", selectable: true}],
      classifications: {existed_before_claude: [{path: "seed.txt"}], created_by_claude: [{path: "result.txt"}]},
    };
    c.state.classifications = {"seed.txt": "exclude", "result.txt": "claude"};
    const selected = c.reviewPayload();
    assert.deepEqual(selected.created_by_claude, ["result.txt"]);
    assert.equal(selected.excluded_files.includes("seed.txt"), false);
    assert.equal(c.renderConfigureStep().includes('data-path="seed.txt"'), false);
  } else if (process.argv[2] === "readiness") {
    // Unvisited chunks are checked by Start; a concrete missing prompt still blocks all runs.
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "inspect_thread" && args.start_message_uuid === "u2") result.replay.request = "";
      return result;
    });
    await c.startChunks();
    assert.equal(c.state.activeUnitKey, later.key);
    assert.equal(c.state.runs.length, 0);
    assert.match(c.state.notice, /Chunk 2 needs attention/);
    assert.equal(c.allUnitsConfigured(), false);
    const restored = make();
    await restored.initialize();
    assert.equal(restored.state.reviewDraft.request, "");
    restored.state.reviewDraft.request = "A valid handoff";
    restored.saveDraft();
    await restored.startChunks();
    assert.equal(restored.state.runs.length, 2);
  } else if (process.argv[2] === "global_models") {
    availableModels = [{id: "model-1", label: "One"}, {id: "model-2", label: "Two"}];
    c.state.models = availableModels;
    await c.editReplayUnit(later.key);
    c.setSelectedModels(["model-1", "model-2"]);
    c.saveDraft();
    // Simulate a legacy chunk snapshot with a different selection.
    c.state.unitDrafts[first.key].reviewDraft.models = ["model-1"];
    await c.editReplayUnit(first.key);
    assert.deepEqual(c.state.selectedModels, ["model-1", "model-2"]);
    await c.changeReplayMode("whole");
    assert.deepEqual(c.reviewPayload().models, ["model-1", "model-2"]);
    await c.changeReplayMode("chunks");
    await c.toggleDivider("u3");
    assert.deepEqual(c.reviewPayload().models, ["model-1", "model-2"]);
    assert.equal(c.replayUnits().filter(c.unitConfigured).length, 1);
    const restored = make();
    await restored.initialize();
    assert.deepEqual(restored.state.selectedModels, ["model-1", "model-2"]);
    await restored.startChunks();
    const starts = calls.filter(({name}) => name === "start_run");
    assert.deepEqual(starts.map(({args}) => args.start_message_uuid), ["u1", "u2", "u3"]);
    assert.ok(starts.every(({args}) => JSON.stringify(args.models) === '["model-1","model-2"]'));
    assert.deepEqual(Object.values(restored.state.unitRuns).map((ids) => ids.length), [2, 2, 2]);
    for (const run of runs.values()) run.status = "completed";
    await restored.refreshRun();
    assert.equal(restored.state.runs.filter((entry) => entry.report).length, 6);
  } else if (process.argv[2] === "revalidate_carry") {
    await c.editReplayUnit(later.key);
    assert.equal(c.allUnitsConfigured(), true);
    await c.editReplayUnit(first.key);
    c.state.classifications["seed.txt"] = "exclude";
    c.state.attributionEdits = ["seed.txt"];
    c.saveDraft();
    assert.equal(c.unitConfigured(first), true);
    assert.equal(c.unitConfigured(later), false);
    await c.startChunks();
    assert.equal(c.state.runs.length, 2);
    assert.deepEqual(calls.filter(({name}) => name === "inspect_thread").at(-1).args.excluded_files, ["seed.txt"]);
    assert.equal(c.allUnitsConfigured(), true);
  } else if (process.argv[2] === "refresh_recovery") {
    await c.editReplayUnit(later.key);
    c.state.reviewDraft.repo = "/tmp/edited";
    c.saveDraft();
    c.setTool(async () => { throw new Error("Inspection unavailable"); });
    await c.editReplayUnit(later.key);
    const saved = JSON.stringify(c.state.classifications);
    for (const partial of [true, false]) {
      c.setTool(async (name, args) => {
        assert.equal(name, "prepare_run");
        return partial ? {baseline: inspection(args).baseline} : {
          ...inspection(args), diagnostics: [{step: "baseline", message: "Incomplete discovery"}],
        };
      });
      await c.refreshAttributionAndContinue();
      assert.equal(c.state.inspection, null);
      assert.equal(JSON.stringify(c.state.classifications), saved);
      assert.equal(c.allUnitsConfigured(), false);
    }
    c.setTool(async (name, args) => {
      assert.equal(name, "prepare_run");
      return {...inspection(args), ready: false};
    });
    await c.refreshAttributionAndContinue();
    assert.equal(c.selectionNeedsRefresh(), false);
    assert.equal(c.allUnitsConfigured(), true);
    assert.equal(c.state.runId, "");
  } else if (process.argv[2] === "non_git_to_git") {
    assert.equal(c.state.inspection.baseline.kind, "unclassified_directory");
    assert.equal(c.unitConfigured(first), true);
    await c.editReplayUnit(later.key);
    c.state.reviewDraft.ending_kind = "git";
    c.state.reviewDraft.ending_commit = "abc1234";
    c.saveDraft();
    assert.equal(c.allUnitsConfigured(), false);
    await c.editReplayUnit(later.key);
    assert.equal(c.state.inspection.baseline.kind, "unclassified_directory");
    assert.equal(c.state.reviewDraft.beginning_kind, "non_git");
    assert.equal(c.state.reviewDraft.ending_kind, "git");
    assert.equal(c.allUnitsConfigured(), true);
  } else if (process.argv[2] === "unused_git_hashes") {
    await c.editReplayUnit(later.key);
    Object.assign(c.state.reviewDraft, {beginning_kind: "git", ending_kind: "git", baseline_commit: "abc1234", ending_commit: "def5678"});
    await c.editReplayUnit(later.key);
    c.state.reviewDraft.beginning_kind = "non_git";
    c.state.reviewDraft.ending_kind = "non_git";
    c.saveDraft();
    c.setTool(async (name, args) => {
      assert.equal(name, "prepare_run");
      assert.equal(args.baseline_commit, "");
      assert.equal(args.ending_commit, "");
      const refreshed = inspection(args);
      return {...refreshed, baseline: {...refreshed.baseline, commit: null, ending_commit: null}};
    });
    await c.refreshAttributionAndContinue();
    assert.equal(c.state.reviewDraft.baseline_commit, "abc1234");
    assert.equal(c.state.reviewDraft.ending_commit, "def5678");
    assert.equal(c.allUnitsConfigured(), true);
    const restored = make();
    await restored.initialize();
    const inspected = calls.filter(({name}) => name === "inspect_thread").at(-1).args;
    assert.equal(inspected.beginning_kind, "non_git");
    assert.equal(inspected.ending_kind, "non_git");
    assert.equal("baseline_commit" in inspected, false);
    assert.equal("ending_commit" in inspected, false);
    assert.equal(restored.allUnitsConfigured(), true);
  } else if (process.argv[2] === "queue_reload_inspect") {
    await c.editReplayUnit(later.key);
    c.state.pendingUnitKeys = [first.key];
    await c.advanceChunkQueue();
    assert.equal([...runs.values()][0].status, "running");
    c.state.pendingUnitKeys = [later.key];
    c.saveDraft();
    c.setTool(() => new Promise(() => {}));
    void c.advanceChunkQueue();
    assert.equal(c.state.busy, "inspection");
    assert.deepEqual(c.state.pendingUnitKeys, [later.key]);
    const restored = make();
    await restored.initialize();
    await restored.refreshRun();
    const starts = calls.filter(({name}) => name === "start_run");
    assert.deepEqual(starts.map(({args}) => args.start_message_uuid), ["u1", "u2"]);
    assert.deepEqual(restored.state.pendingUnitKeys, []);
    assert.equal(restored.state.runs.every((entry) => entry.run.status === "running"), true);
  } else if (process.argv[2] === "preflight_reload") {
    await c.editReplayUnit(later.key);
    let reached;
    const paused = new Promise((resolve) => { reached = resolve; });
    const original = c.setTool((name, args) => {
      if (name === "inspect_thread" && args.start_message_uuid === "u2") {
        reached();
        return new Promise(() => {});
      }
      return original(name, args);
    });
    void c.startChunks();
    await paused;
    assert.equal(runs.size, 0);
    assert.deepEqual(c.state.pendingUnitKeys, [first.key, later.key]);
    const restored = make();
    await restored.initialize();
    assert.equal(restored.state.runs.length, 2);
    assert.equal(restored.state.runs.every((entry) => entry.run.status === "running"), true);
    assert.deepEqual(restored.state.pendingUnitKeys, []);
  } else if (process.argv[2] === "durable_midlaunch_reload") {
    await c.toggleDivider("u3");
    for (const unit of c.replayUnits()) await c.editReplayUnit(unit.key);
    const units = c.replayUnits();
    let reached;
    const paused = new Promise((resolve) => { reached = resolve; });
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "start_run" && args.start_message_uuid === "u2") {
        reached();
        return new Promise(() => {});
      }
      return result;
    });
    void c.startChunks();
    await paused;
    assert.equal(runs.size, 2);
    assert.equal(c.state.submittingUnitKey, units[1].key);
    assert.deepEqual(c.state.pendingUnitKeys, units.slice(1).map((unit) => unit.key));
    const restored = make();
    await restored.initialize();
    assert.deepEqual(calls.filter(({name}) => name === "start_run").map(({args}) => args.start_message_uuid), ["u1", "u2", "u3"]);
    assert.equal(restored.state.runs.length, 3);
    assert.equal(restored.state.runs.every((entry) => entry.run.status === "running"), true);
    assert.deepEqual(restored.state.pendingUnitKeys, []);
    assert.equal(restored.state.submittingUnitKey, "");
  } else if (process.argv[2] === "preflight_failure") {
    await c.editReplayUnit(later.key);
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      return name === "prepare_run" && args.start_message_uuid === "u2"
        ? {...result, ready: false, blockers: ["The second chunk needs review"]} : result;
    });
    await c.startChunks();
    assert.equal(calls.filter(({name}) => name === "prepare_run").length, 2);
    assert.equal(calls.filter(({name}) => name === "start_run").length, 0);
    assert.equal(c.chunkLaunchActive(), false);
    assert.deepEqual(c.state.pendingUnitKeys, []);
  } else if (process.argv[2] === "approval_eviction") {
    await c.editReplayUnit(later.key);
    const approvals = new Map(), validated = new Set();
    let tokenNumber = 0;
    const original = c.setTool(async (name, args) => {
      if (name === "start_run") {
        assert.deepEqual([...validated], ["u1", "u2"]);
        const approval = approvals.get(args.prepare_token);
        assert.ok(approval, "The launch must use an approval still in the cache");
        for (const field of ["start_message_uuid", "end_message_uuid", "request"]) assert.equal(args[field], approval[field]);
      }
      const result = await original(name, args);
      if (name === "prepare_run") {
        validated.add(args.start_message_uuid);
        const token = `token-${++tokenNumber}`;
        approvals.clear(); // A bounded cache can evict every earlier preflight approval.
        approvals.set(token, args);
        return {...result, prepare_token: token};
      }
      return result;
    });
    await c.startChunks();
    assert.equal(c.state.runs.length, 2);
    assert.equal(c.state.runs.every((entry) => entry.run.status === "running"), true);
    assert.deepEqual(c.state.pendingUnitKeys, []);
    assert.equal(c.state.error, "");
  } else if (process.argv[2] === "launch_refresh_failure") {
    await c.editReplayUnit(later.key);
    const preparedCounts = new Map();
    const original = c.setTool(async (name, args) => {
      const result = await original(name, args);
      if (name === "prepare_run") {
        const count = (preparedCounts.get(args.start_message_uuid) || 0) + 1;
        preparedCounts.set(args.start_message_uuid, count);
        if (args.start_message_uuid === "u2" && count > 1) return {...result, ready: false, blockers: ["Historical files changed"]};
      }
      return result;
    });
    await c.startChunks();
    assert.deepEqual(calls.filter(({name}) => name === "start_run").map(({args}) => args.start_message_uuid), ["u1"]);
    assert.equal(c.state.runs[0].run.status, "running");
    assert.equal(c.state.activeUnitKey, later.key);
    assert.equal(c.state.step, "configure");
    assert.equal(c.state.busy, "");
    assert.equal(c.state.submittingUnitKey, "");
    assert.equal(c.chunkLaunchActive(), false);
    assert.deepEqual(c.state.pendingUnitKeys, []);
    assert.deepEqual(c.state.preparation.blockers, ["Historical files changed"]);
  } else if (process.argv[2] === "parallel_stress") {
    for (let index = 4; index <= 12; index++) turns.push({message_uuid: `u${index}`, request: `Task ${index}`});
    c.state.actionableTurns = turns;
    c.state.dividers = turns.slice(1).map((turn) => turn.message_uuid);
    c.state.activeUnitKey = "";
    c.state.reviewDraft = null;
    for (const unit of c.replayUnits()) {
      await c.editReplayUnit(unit.key);
      c.state.reviewDraft.request = `Edited handoff ${unit.number}`;
      c.saveDraft();
    }
    assert.equal(c.allUnitsConfigured(), true);
    calls.length = 0;
    let inspections = 0;
    c.setRenderObserver(() => {
      if (c.state.busy === "inspection") {
        inspections++;
        assert.equal(c.renderConfigureStep().includes('class="banner banner--warning"'), false);
      }
    });
    function verifyLaunches(controller) {
      const original = controller.setTool(async (name, args) => {
        if (name === "start_run") {
          const preparations = calls.filter((call) => call.name === "prepare_run");
          assert.equal(new Set(preparations.map((call) => call.args.start_message_uuid)).size, 12);
          assert.equal(preparations.at(-1).args.start_message_uuid, args.start_message_uuid);
          assert.ok([...runs.values()].filter((run) => run.status === "running").length + args.models.length <= 8);
        }
        return original(name, args);
      });
    }
    const preparationCalls = () => calls.filter(({name}) => ["inspect_thread", "prepare_run"].includes(name)).length;
    const finishTwo = () => [...runs.values()].filter((run) => run.status === "running").slice(0, 2).forEach((run) => { run.status = "completed"; });
    verifyLaunches(c);
    await c.startChunks();
    assert.equal(c.state.dividers.length, 11);
    assert.equal(inspections >= 12, true);
    assert.equal(c.state.runs.length, 8);
    assert.equal(c.state.runs.every((entry) => entry.run.status === "running"), true);
    assert.equal(c.state.pendingUnitKeys.length, 4);
    const fullCapacityCalls = preparationCalls();
    for (let index = 0; index < 3; index++) await c.refreshRun({continuePolling: true});
    assert.equal(preparationCalls(), fullCapacityCalls);
    finishTwo();
    await c.refreshRun();
    assert.equal(c.state.runs.length, 10);
    assert.equal(preparationCalls(), fullCapacityCalls + 2);
    const restored = make();
    verifyLaunches(restored);
    await restored.initialize();
    assert.equal(preparationCalls(), fullCapacityCalls + 2);
    assert.equal(restored.state.pendingUnitKeys.length, 2);
    finishTwo();
    await restored.refreshRun();
    const starts = calls.filter(({name}) => name === "start_run");
    assert.equal(starts.length, 12);
    assert.equal(restored.state.runs.length, 12);
    assert.equal(restored.state.runs.filter((entry) => entry.run.status === "running").length, 8);
    assert.deepEqual(starts.map(({args}) => args.request), turns.map((_, index) => `Edited handoff ${index + 1}`));
    assert.deepEqual(starts.map(({args}) => args.start_message_uuid), turns.map((turn) => turn.message_uuid));
    assert.deepEqual(restored.state.pendingUnitKeys, []);
    for (const run of runs.values()) run.status = "completed";
    await restored.refreshRun();
    assert.equal(restored.state.step, "results");
    assert.equal(restored.state.runs.filter((entry) => entry.report).length, 12);
  } else if (process.argv[2] === "multiple_model_capacity") {
    availableModels = [1, 2, 3].map((number) => ({id: `model-${number}`}));
    c.state.models = availableModels;
    await c.toggleDivider("u3");
    for (const unit of c.replayUnits()) {
      await c.editReplayUnit(unit.key);
      c.setSelectedModels(availableModels.map(({id}) => id));
      c.saveDraft();
    }
    const original = c.setTool(async (name, args) => {
      if (name === "start_run") {
        assert.equal(args.models.length, 3);
        assert.ok([...runs.values()].filter((run) => run.status === "running").length + args.models.length <= 8);
      }
      return original(name, args);
    });
    await c.startChunks();
    assert.equal(c.state.maxParallelRuns, 8);
    assert.equal(c.state.runs.length, 6);
    assert.equal(c.state.pendingUnitKeys.length, 1);
    const beforePoll = calls.filter(({name}) => ["inspect_thread", "prepare_run"].includes(name)).length;
    await c.refreshRun();
    assert.equal(calls.filter(({name}) => ["inspect_thread", "prepare_run"].includes(name)).length, beforePoll);
    [...runs.values()][0].status = "completed";
    await c.refreshRun();
    assert.equal(c.state.runs.length, 9);
    assert.equal(c.state.runs.filter((entry) => entry.run.status === "running").length, 8);
    assert.deepEqual(Object.values(c.state.unitRuns).map((ids) => ids.length), [3, 3, 3]);
    assert.deepEqual(c.state.pendingUnitKeys, []);
    for (const run of runs.values()) run.status = "completed";
    await c.refreshRun();
    assert.equal(c.state.step, "results");
    assert.equal(c.state.runs.filter((entry) => entry.report).length, 9);
  } else if (process.argv[2] === "chunk_results") {
    function entry(id, unitNumber, model, historical, replay) {
      return {
        id, unitNumber, model, run: {run_id: id, model, status: "completed"},
        reportPaths: {report_json: `/reports/${id}.json`},
        report: {schema_version: 3, original_request: `request-${id}`,
          baseline: {repository: `/historical/${id}`}, codex_execution: {worktree: `/codex/${id}`},
          candidates: {claude: {diff: `historical-patch-${id}`}, codex: {model, diff: `codex-patch-${id}`}},
          evaluation: {candidate_mapping: {A: "claude", B: "codex"}, totals: {A: historical, B: replay}},
        },
      };
    }
    c.state.runs = [entry("first-alpha", 1, "alpha", .9, .1), entry("first-beta", 1, "beta", .7, .3), entry("later-gamma", 2, "gamma", .2, .8)];
    c.state.runErrors = [{unitNumber: 1, model: "delta", error: "first-chunk-error"}, {unitNumber: 2, model: "epsilon", error: "later-chunk-error"}];
    c.activateRun("later-gamma");
    c.selectChunkResult(1);
    assert.equal(c.state.runId, "first-alpha");
    let html = c.renderResultsStep();
    assert.ok(html.indexOf('aria-label="Chunk results"') < html.indexOf('class="result-hero"'));
    assert.ok(html.includes('data-unit-number="1" aria-pressed="true"'));
    assert.ok(html.includes('data-unit-number="2" aria-pressed="false"'));
    assert.ok(html.includes('data-run-id="first-alpha"'));
    assert.ok(html.includes('data-run-id="first-beta"'));
    assert.ok(html.includes("first-chunk-error"));
    assert.equal(html.includes("later-gamma"), false);
    assert.equal(html.includes("later-chunk-error"), false);
    c.activateRun("first-beta");
    assert.equal(c.state.report.original_request, "request-first-beta");
    c.selectChunkResult(2);
    html = c.renderResultsStep();
    assert.equal(c.state.runId, "later-gamma");
    assert.equal(c.state.reportPaths.report_json, "/reports/later-gamma.json");
    assert.match(html, /id="quality-heading"[^>]*>Quality winner<\/h2>\s*<p[^>]*>Codex replay<\/p>/);
    assert.match(html, /<dt>Historical Claude<\/dt><dd>20%<\/dd>/);
    assert.match(html, /<dt>Codex replay<\/dt><dd>80%<\/dd>/);
    assert.ok(html.includes("request-later-gamma"));
    assert.ok(html.includes("historical-patch-later-gamma"));
    assert.equal(html.includes("first-alpha"), false);
    assert.equal(html.includes("first-beta"), false);
    assert.equal(html.includes("first-chunk-error"), false);
    assert.ok(c.renderRunVariants().includes('data-run-id="first-alpha"'));
    c.state.runs[2].report = null;
    c.state.runs[2].run = {status: "failed", error: "later chunk failed"};
    c.selectChunkResult(1);
    c.selectChunkResult(2);
    html = c.renderResultsStep();
    assert.equal(c.state.report, null);
    assert.ok(html.includes("later chunk failed"));
    assert.equal(html.includes("request-first-alpha"), false);
    assert.equal(html.includes('id="quality-heading"'), false);
  } else if (process.argv[2] === "file_controls") {
    for (const ending of ["non_git", "git"]) {
      c.state.reviewDraft.ending_kind = ending;
      c.state.reviewDraft.ending_commit = ending === "git" ? "abc1234" : "";
      Object.assign(c.state.inspection.baseline, {ending_kind: ending, ending_commit: c.state.reviewDraft.ending_commit});
      c.state.inspection.file_selection.source_kind = ending;
      const selection = c.state.inspection.file_selection;
      const html = c.renderHistoricalOutput(c.state.reviewDraft, selection, selection.candidates, ending);
      assert.equal(html.includes('class="banner'), false);
      assert.ok(html.includes(`data-file-kind="${ending}"`));
      assert.ok(html.includes('data-path="seed.txt"'));
      assert.ok(html.includes('data-path="result.txt"'));
    }
  } else if (process.argv[2] === "fresh_browser_results") {
    storage.clear();
    const records = [
      {run_id: "20260101-000001-a-later", started_at: "2026-01-01T00:00:01.100003+00:00", model: "gamma", start_message_uuid: "u2", end_message_uuid: "u3", prepare_token_hash: "later-token"},
      {run_id: "20260101-000001-y-first", created_at: "2026-01-01T00:00:01.100002+00:00", model: "beta", start_message_uuid: "u1", end_message_uuid: "u1", prepare_token_hash: "first-token"},
      {run_id: "20260101-000001-z-first", started_at: "2026-01-01T00:00:01.100001+00:00", model: "alpha", start_message_uuid: "u1", end_message_uuid: "u1", prepare_token_hash: "first-token"},
    ].map((run) => ({...run, thread_id: "thread-1", controller_session_id: "session-1", status: "completed"}));
    const fresh = make(), restoredCalls = [];
    fresh.setTool(async (name, args) => {
      restoredCalls.push(name);
      if (name === "get_state") return {state: {controller_session_id: "session-1", recent_runs: records, models: [{id: "alpha"}, {id: "beta"}, {id: "gamma"}]}};
      if (name === "list_threads") return {threads: [], total: 0};
      if (name === "get_report") return {report: {original_request: `request-${args.run_id}`, evaluation: {totals: {A: .2, B: .8}}}, report_json: `/reports/${args.run_id}.json`};
      throw new Error(`Fresh results should not call ${name}`);
    });
    await fresh.initialize();
    await fresh.refreshRun();
    assert.equal(fresh.state.replayMode, "chunks");
    assert.equal(fresh.canNavigateTo("configure"), false);
    assert.equal(fresh.state.runs.length, 3);
    assert.equal(fresh.state.runs.find((entry) => entry.model === "gamma").unitNumber, 2);
    assert.equal(fresh.state.runs.find((entry) => entry.model === "alpha").unitNumber, 1);
    assert.equal(restoredCalls.filter((name) => name === "get_report").length, 3);
    assert.equal(restoredCalls.includes("inspect_thread"), false);
    assert.equal(restoredCalls.includes("infer_working_directory"), false);
    fresh.selectChunkResult(1);
    let html = fresh.renderResultsStep();
    assert.ok(html.includes('data-unit-number="1" aria-pressed="true"'));
    assert.ok(html.includes('data-run-id="20260101-000001-z-first"'));
    assert.ok(html.includes('data-run-id="20260101-000001-y-first"'));
    assert.equal(html.includes("20260101-000001-a-later"), false);
    fresh.selectChunkResult(2);
    html = fresh.renderResultsStep();
    assert.ok(html.includes("request-20260101-000001-a-later"));
    assert.equal(html.includes("20260101-000001-z-first"), false);
    assert.equal(html.includes("NaN"), false);
  } else if (process.argv[2] === "queue") {
    await c.editReplayUnit(later.key);
    await c.startChunks();
    assert.equal(calls.filter(({name}) => name === "start_run").length, 2);
    assert.equal(calls.find(({name}) => name === "start_run").args.start_message_uuid, "u1");
    assert.deepEqual(c.state.pendingUnitKeys, []);
    assert.equal(c.state.runs.every((entry) => entry.run.status === "running"), true);
    await c.refreshRun();
    assert.equal(calls.filter(({name}) => name === "start_run").length, 2);
    // Durable launches are reconciled before attempting any restored pending key.
    c.state.pendingUnitKeys = [first.key, later.key];
    c.saveDraft();
    const restored = make();
    await restored.initialize();
    await restored.refreshRun();
    const starts = calls.filter(({name}) => name === "start_run");
    assert.equal(starts.length, 2);
    assert.deepEqual(starts.map(({args}) => args.start_message_uuid), ["u1", "u2"]);
    assert.equal(restored.state.runs.length, 2);
    assert.deepEqual(restored.state.pendingUnitKeys, []);
    for (const run of runs.values()) run.status = "completed";
    await restored.refreshRun();
    assert.equal(restored.state.step, "results");
    assert.equal(restored.state.runs.filter((entry) => entry.report).length, 2);
  } else if (process.argv[2] === "ambiguous") {
    await c.editReplayUnit(later.key);
    c.setTool(async (name, args) => {
      if (name === "inspect_thread") return inspection(args);
      if (name === "prepare_run") return {...inspection(args), ready: true, run_config: args, prepare_token: "approved"};
      if (name === "start_run") throw new Error("Connection closed after submission");
      throw new Error(`Unexpected ${name}`);
    });
    await c.startChunks();
    assert.equal(c.state.submittingUnitKey, first.key);
    assert.deepEqual(c.state.pendingUnitKeys, []);
    await c.startChunks();
    assert.deepEqual(c.state.pendingUnitKeys, []);
    const restored = make();
    await restored.initialize();
    assert.equal(calls.filter(({name}) => name === "start_run").length, 0);
  }
}
main().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });
"""


class ControllerRangeTests(unittest.TestCase):
    def test_shared_usage_is_explained_and_not_compared_in_results_or_variants(self) -> None:
        self.run_scenario("shared_usage")

    def run_scenario(self, scenario: str) -> None:
        result = subprocess.run(
            ["node", "-e", HARNESS, str(CONTROLLER), scenario],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chunks_persist_configuration_and_boundaries_and_ignore_old_selection(self) -> None:
        self.run_scenario("persistence")

    def test_reload_during_range_inspection_preserves_file_attribution(self) -> None:
        self.run_scenario("inspect_reload")

    def test_aborted_range_inspection_preserves_saved_file_attribution(self) -> None:
        self.run_scenario("inspect_abort")

    def test_later_range_carries_earlier_reviewed_files(self) -> None:
        self.run_scenario("carry")

    def test_reinspection_updates_inferred_classes_and_preserves_explicit_file_edits(self) -> None:
        self.run_scenario("changed_inference")

    def test_missing_prompt_pauses_automatic_preflight_and_can_be_resolved(self) -> None:
        self.run_scenario("readiness")

    def test_global_models_survive_switches_dividers_reload_and_run_unvisited_chunks(self) -> None:
        self.run_scenario("global_models")

    def test_upstream_file_edits_trigger_automatic_downstream_reinspection(self) -> None:
        self.run_scenario("revalidate_carry")

    def test_refresh_recovers_failed_inspection_without_accepting_partial_state(self) -> None:
        self.run_scenario("refresh_recovery")

    def test_reload_during_next_chunk_inspection_keeps_that_chunk_queued(self) -> None:
        self.run_scenario("queue_reload_inspect")

    def test_non_git_beginning_with_git_end_accepts_inferred_file_classification(self) -> None:
        self.run_scenario("non_git_to_git")

    def test_non_git_reload_omits_retained_git_hashes_after_state_edit(self) -> None:
        self.run_scenario("unused_git_hashes")

    def test_chunk_results_switch_reports_and_only_show_that_chunks_models(self) -> None:
        self.run_scenario("chunk_results")

    def test_git_and_non_git_file_controls_do_not_show_inventory_warning(self) -> None:
        self.run_scenario("file_controls")

    def test_fresh_browser_recovers_all_chunk_reports_without_transcript_inspection(self) -> None:
        self.run_scenario("fresh_browser_results")

    def test_parallel_ranges_reuse_prepare_start_and_reconcile_durable_runs(self) -> None:
        self.run_scenario("queue")

    def test_reload_before_first_launch_revalidates_and_resumes_approved_chunks(self) -> None:
        self.run_scenario("preflight_reload")

    def test_invalid_last_preparation_prevents_every_chunk_launch(self) -> None:
        self.run_scenario("preflight_failure")

    def test_chunk_launch_refreshes_evicted_preflight_approvals(self) -> None:
        self.run_scenario("approval_eviction")

    def test_failed_launch_preparation_pauses_remaining_chunks_without_submission(self) -> None:
        self.run_scenario("launch_refresh_failure")

    def test_reload_reconciles_durable_middle_launch_with_lost_response(self) -> None:
        self.run_scenario("durable_midlaunch_reload")

    def test_twelve_chunks_drain_with_bounded_parallelism_reload_and_no_transient_warning(
        self,
    ) -> None:
        self.run_scenario("parallel_stress")

    def test_capacity_counts_each_models_run_before_launching_the_next_chunk(self) -> None:
        self.run_scenario("multiple_model_capacity")

    def test_ambiguous_launch_is_not_retried(self) -> None:
        self.run_scenario("ambiguous")


if __name__ == "__main__":
    unittest.main()
