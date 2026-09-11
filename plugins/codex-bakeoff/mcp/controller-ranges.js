"use strict";

// Range selection shares the controller state and its existing inspect/prepare/start flow.
globalThis.createReplayRangeUI = function (context) {
  const {
    state, isObject, asArray, text,
    stringList, normalizeReviewDraft, escapeHtml, terminalRunStatus,
    render, callTool, initializeClassifications, reviewDraftFromConfiguration,
    inspectionParts, setSelectedModels, synthesizePrompt, inferWorkingDirectory,
    saveDraft, prepareRun, reviewProblems, preparationBlockers, replayDetailsLoading, selectionNeedsRefresh,
    activateRun, rememberActiveRun, preparationReady, startRun, beginPolling
  } = context;
  let inspectedUnitKey = "", inspectedCarry = "", launchingChunks = false;
  let preparedChunks = [];

  function normalizeUnitDraft(value) {
    if (!isObject(value)) return null;
    const reviewDraft = normalizeReviewDraft(value.reviewDraft);
    // Models belong to the session, including when reading older per-chunk drafts.
    if (reviewDraft) { delete reviewDraft.model; delete reviewDraft.models; }
    return {
      reviewDraft,
      classifications: Object.fromEntries(Object.entries(isObject(value.classifications) ? value.classifications : {})
        .filter(([, choice]) => ["claude", "exclude"].includes(choice))),
      attributionEdits: stringList(value.attributionEdits),
      promptGeneration: text(value.promptGeneration),
      detailsOpen: value.detailsOpen === true,
      configured: value.configured === true,
      problems: stringList(value.problems),
      carry: text(value.carry),
    };
  }

  function rememberUnitDraft() {
    if (!state.reviewDraft) return;
    const unit = activeUnit();
    const saved = unit && state.unitDrafts[unit.key];
    const inspected = unit && state.inspection && inspectedUnitKey === unit.key;
    const problems = inspected && !replayDetailsLoading()
      ? [...reviewProblems(), ...preparationBlockers(), ...(state.error ? [state.error] : [])]
      : saved?.problems || [];
    const draft = normalizeUnitDraft({
      reviewDraft: state.reviewDraft,
      classifications: state.classifications,
      attributionEdits: state.attributionEdits,
      promptGeneration: state.promptGeneration,
      detailsOpen: state.configurationDetailsOpen,
      configured: inspected
        ? !replayDetailsLoading() && !selectionNeedsRefresh() && !problems.length
        : saved?.configured,
      problems,
      carry: inspected ? inspectedCarry : saved?.carry,
    });
    if (state.replayMode === "chunks" && state.activeUnitKey) {
      state.unitDrafts[state.activeUnitKey] = draft;
    } else if (state.replayMode === "whole") {
      state.wholeDraft = draft;
    }
  }

  function restoreUnitState(draft) {
    preparedChunks = [];
    if (!draft) return;
    for (const key of ["replayMode", "actionableTurns", "dividers",
      "activeUnitKey", "unitDrafts", "wholeDraft", "pendingUnitKeys", "unitRuns", "submittingUnitKey"]) {
      state[key] = draft[key];
    }
    if (state.replayMode === "chunks") {
      state.selectedThreadId = draft.selectedThreadId;
      state.selectedThreadNumber = draft.selectedThreadNumber;
    }
  }

  function replayUnits() {
    const units = [];
    for (const [index, turn] of state.actionableTurns.entries()) {
      if (!units.length || state.dividers.includes(turn.message_uuid)) {
        units.push({ first: index, last: index, turns: [] });
      }
      const unit = units.at(-1);
      unit.last = index;
      unit.turns.push(turn);
    }
    return units.map((unit, index) => ({
      ...unit,
      number: index + 1,
      start_message_uuid: unit.turns[0].message_uuid,
      end_message_uuid: unit.turns.at(-1).message_uuid,
      key: `${unit.turns[0].message_uuid}/${unit.turns.at(-1).message_uuid}`,
    }));
  }

  function activeUnit() {
    return state.replayMode === "chunks"
      ? replayUnits().find((unit) => unit.key === state.activeUnitKey)
      : null;
  }

  function rangeParameters() {
    const unit = activeUnit();
    return unit ? {
      start_message_uuid: unit.start_message_uuid,
      end_message_uuid: unit.end_message_uuid,
    } : {};
  }

  function earlierFileAttribution(unit = activeUnit()) {
    const included = new Set(), excluded = new Set();
    if (unit) for (const earlier of replayUnits().filter((item) => item.last < unit.first)) {
      const saved = state.unitDrafts[earlier.key];
      if (!saved) continue;
      for (const [path, choice] of Object.entries(saved.classifications)) {
        if (!saved.attributionEdits.includes(path)) continue;
        if (choice === "claude") { included.add(path); excluded.delete(path); }
        else { included.delete(path); excluded.add(path); }
      }
    }
    return { carried_forward_files: [...included], excluded_files: [...excluded] };
  }

  function carryFingerprint(unit) {
    const earlier = earlierFileAttribution(unit);
    return JSON.stringify([earlier.carried_forward_files.sort(), earlier.excluded_files.sort()]);
  }

  function unitConfigured(unit) {
    const saved = state.unitDrafts[unit.key];
    return Boolean(saved?.configured && saved.carry === carryFingerprint(unit));
  }

  function allUnitsConfigured() {
    rememberUnitDraft();
    const units = replayUnits();
    return units.length > 0 && units.every(unitConfigured);
  }

  function markUnitInspected(payload) {
    const unit = activeUnit(), baseline = payload?.baseline, selection = payload?.file_selection;
    if (!unit || !isObject(baseline) || !isObject(selection) ||
      payload.replay?.message_uuid !== unit.start_message_uuid ||
      !( ["git_commit", "empty_directory"].includes(baseline.kind) ||
        (baseline.kind === "unclassified_directory" && baseline.beginning_kind === "non_git" && baseline.proposed_kind === "empty_directory")) ||
      !["git", "non_git"].includes(baseline.beginning_kind) ||
      !["git", "non_git"].includes(baseline.ending_kind) ||
      selection.source_kind !== baseline.ending_kind || !Array.isArray(selection.candidates) ||
      asArray(payload.diagnostics).some((item) => ["thread", "baseline"].includes(item?.step))) return false;
    inspectedUnitKey = unit.key;
    inspectedCarry = carryFingerprint(unit);
    return true;
  }

  function savedStateParameters(saved) {
    const draft = saved?.reviewDraft;
    if (!draft?.repo || !draft.beginning_kind || !draft.ending_kind ||
      (draft.beginning_kind === "git" && (draft.ending_kind !== "git" || !draft.baseline_commit)) ||
      (draft.ending_kind === "git" && !draft.ending_commit)) return {};
    return {
      repo: draft.repo, beginning_kind: draft.beginning_kind, ending_kind: draft.ending_kind,
      ...(draft.beginning_kind === "git" ? { baseline_commit: draft.baseline_commit } : {}),
      ...(draft.ending_kind === "git" ? { ending_commit: draft.ending_commit } : {}),
    };
  }

  function configurationIsLocked() {
    return state.batchPreparing || launchingChunks || state.pendingUnitKeys.length > 0 || Boolean(state.run) && (state.replayMode !== "chunks" ||
      state.runs.some((entry) => !terminalRunStatus(entry.run?.status)));
  }

  async function editReplayUnit(key, mode = "chunks", forLaunch = false) {
    if (state.busy || (!forLaunch && (launchingChunks || configurationIsLocked()))) return false;
    rememberUnitDraft();
    state.replayMode = mode;
    if (mode === "chunks") state.activeUnitKey = key;
    const unit = activeUnit();
    if (mode === "chunks" && !unit) return false;
    const saved = mode === "chunks" ? state.unitDrafts[key] : state.wholeDraft;
    inspectedUnitKey = "";
    const threadIdValue = state.selectedThreadId;
    state.busy = "inspection";
    state.error = "";
    state.notice = "";
    state.reviewDraft = null;
    state.inspection = null;
    state.classifications = { ...saved?.classifications };
    state.promptGeneration = saved?.promptGeneration || "";
    state.preparation = null;
    state.reviewRevision += 1;
    state.promptSynthesisGeneration += 1;
    state.workingDirectoryGeneration += 1;
    state.promptEditRevision = 0;
    state.workingDirectoryEditRevision = 0;
    state.workingDirectoryLoading = false;
    state.attributionEdits = saved?.attributionEdits || [];
    state.configurationDetailsOpen = saved?.detailsOpen ?? mode === "chunks";
    state.step = "configure";
    render();
    try {
      const payload = await callTool("inspect_thread", {
        thread_id: threadIdValue, ...rangeParameters(),
        ...(unit ? { ...earlierFileAttribution(), ...savedStateParameters(saved) } : {}),
      });
      state.inspection = payload;
      const turns = asArray(payload.replay?.actionable_user_turns || payload.thread?.actionable_user_turns);
      if (turns.length) state.actionableTurns = turns;
      initializeClassifications();
      state.reviewDraft = reviewDraftFromConfiguration();
      if (saved?.reviewDraft) {
        state.reviewDraft = normalizeReviewDraft({ ...state.reviewDraft, ...saved.reviewDraft,
          request: mode === "whole" ? saved.reviewDraft.request || state.reviewDraft.request : saved.reviewDraft.request,
          message_uuid: unit ? unit.start_message_uuid : saved.reviewDraft.message_uuid });
        for (const file of inspectionParts().files) {
          if (file.selectable !== false && saved.classifications[file.path] &&
            (!unit || saved.attributionEdits.includes(file.path))) {
            state.classifications[file.path] = saved.classifications[file.path];
          }
        }
      }
      setSelectedModels(state.selectedModels);
      state.promptGeneration = saved?.promptGeneration || text(
        payload.replay?.request_generation?.method || payload.thread?.request_generation?.method,
        "concatenated_fallback"
      );
      await Promise.all([
        state.promptGeneration === "pending"
          ? synthesizePrompt(threadIdValue, state.reviewDraft.request) : Promise.resolve(),
        !saved?.reviewDraft?.repo ? inferWorkingDirectory(threadIdValue) : Promise.resolve(),
      ]);
      markUnitInspected(payload);
      return true;
    } catch (error) {
      state.error = error instanceof Error ? error.message : String(error);
      state.inspection = null;
      state.reviewDraft = normalizeReviewDraft(saved?.reviewDraft);
      setSelectedModels(state.selectedModels);
      state.classifications = { ...saved?.classifications };
      state.promptGeneration = saved?.promptGeneration || "";
      return false;
    } finally {
      state.busy = "";
      render();
    }
  }

  async function changeReplayMode(mode) {
    if (mode === state.replayMode || state.busy || state.run) return;
    if (mode === "chunks" && state.selectedThreadId.startsWith("claude-sample:")) return;
    const units = replayUnits();
    if (mode === "chunks" && !units.length) return;
    await editReplayUnit(units.find((unit) => unit.key === state.activeUnitKey)?.key || units[0]?.key || "", mode);
  }

  async function toggleDivider(messageUuid) {
    if (state.busy || state.run || !state.actionableTurns.slice(1).some((turn) => turn.message_uuid === messageUuid)) return;
    rememberUnitDraft();
    state.dividers = state.dividers.includes(messageUuid)
      ? state.dividers.filter((id) => id !== messageUuid) : [...state.dividers, messageUuid];
    const units = replayUnits();
    state.activeUnitKey = "";
    state.reviewDraft = null;
    await editReplayUnit(units[0].key);
  }

  function remainingUnits() {
    return replayUnits().filter((unit) => !state.unitRuns[unit.key]?.length);
  }

  async function startChunks() {
    if (launchingChunks || state.busy || configurationIsLocked() || state.submittingUnitKey) return;
    rememberUnitDraft();
    preparedChunks = [];
    state.pendingUnitKeys = remainingUnits().map((unit) => unit.key);
    await advanceChunkQueue();
  }

  function chunkModelCount() {
    return state.selectedModels.length;
  }

  function chunkFitsCapacity() {
    return state.runs.filter((entry) => !terminalRunStatus(entry.run?.status)).length +
      chunkModelCount() <= state.maxParallelRuns;
  }

  async function advanceChunkQueue() {
    if (launchingChunks || state.busy || state.submittingUnitKey) return false;
    state.pendingUnitKeys = state.pendingUnitKeys.filter((key) => !state.unitRuns[key]?.length);
    preparedChunks = preparedChunks.filter(({key}) => state.pendingUnitKeys.includes(key));
    saveDraft();
    if (!state.pendingUnitKeys.length) return false;
    if (!chunkModelCount() || chunkModelCount() > state.maxParallelRuns) {
      state.error = `Choose between 1 and ${state.maxParallelRuns} models for this replay.`;
      state.pendingUnitKeys = [];
      preparedChunks = [];
      render();
      return true;
    }
    if (!chunkFitsCapacity()) return false;
    launchingChunks = true;
    try {
      if (!preparedChunks.length) {
        const prepared = [];
        for (const key of state.pendingUnitKeys) {
          if (!await editReplayUnit(key, "chunks", true)) { pauseChunkQueue(); return true; }
          await prepareRun({ autoStart: false });
          if (!preparationReady()) { pauseChunkQueue(); return true; }
          rememberUnitDraft();
          if (!unitConfigured(activeUnit())) { pauseChunkQueue(); return true; }
          prepared.push({key, inspection: state.inspection,
            draft: normalizeUnitDraft(state.unitDrafts[key])});
        }
        preparedChunks = prepared;
      }
      if (!allUnitsConfigured()) {
        state.pendingUnitKeys = [];
        state.notice = "A chunk's historical state changed. Start again to recheck the chunks.";
        return true;
      }
      for (const {key, inspection, draft} of preparedChunks) {
        if (!state.pendingUnitKeys.includes(key)) continue;
        if (!chunkFitsCapacity()) break;
        state.activeUnitKey = key;
        state.inspection = inspection;
        state.preparation = null;
        state.step = "configure";
        state.reviewDraft = normalizeReviewDraft(draft.reviewDraft);
        state.classifications = { ...draft.classifications };
        state.attributionEdits = [...draft.attributionEdits];
        state.promptGeneration = draft.promptGeneration;
        setSelectedModels(state.selectedModels);
        markUnitInspected(inspection);
        await prepareRun({ autoStart: false });
        if (!preparationReady()) { pauseChunkQueue(); break; }
        await startRun();
        if (!state.unitRuns[key]?.length) { state.pendingUnitKeys = []; break; }
        state.pendingUnitKeys = state.pendingUnitKeys.filter((value) => value !== key);
        saveDraft();
      }
    } finally {
      launchingChunks = false;
      if (!state.pendingUnitKeys.length) preparedChunks = [];
      render({ focus: true });
      if (state.runs.some((entry) => !terminalRunStatus(entry.run?.status))) beginPolling();
    }
    return true;
  }

  function chunkLaunchActive() { return launchingChunks; }

  function pauseChunkQueue() {
    state.pendingUnitKeys = [];
    state.configurationDetailsOpen = true;
    state.notice = `Chunk ${activeUnit()?.number} needs attention. Resolve the issue below, then start the remaining chunks.`;
  }

  function renderPendingChunks() {
    const count = state.pendingUnitKeys.length;
    return count ? `<p class="chunk-help">${count} ${count === 1 ? "chunk" : "chunks"} queued. Up to ${state.maxParallelRuns} model runs can run at a time.</p>` : "";
  }

  function renderRangeSelection() {
    rememberUnitDraft();
    const split = state.replayMode === "chunks";
    const disabled = state.busy || configurationIsLocked();
    const units = replayUnits();
    return `<section class="panel range-selection" aria-labelledby="replay-scope-heading">
      <div class="panel__header"><div><h2 id="replay-scope-heading">Replay scope</h2>
        <p>Replay the whole thread or choose ranges of user turns.</p></div></div>
      <div class="panel__body">
        <div class="variant-options" role="group" aria-label="Replay scope">
          <button type="button" class="button button--quiet" data-action="replay-mode" data-mode="whole" aria-pressed="${!split}" ${disabled || state.run ? "disabled" : ""}>Whole thread</button>
          <button type="button" class="button button--quiet" data-action="replay-mode" data-mode="chunks" aria-pressed="${split}" ${disabled || state.run || !units.length || state.selectedThreadId.startsWith("claude-sample:") ? "disabled" : ""}>Split into chunks</button>
        </div>
        ${split ? `<p class="chunk-help">Add dividers between user turns. Start checks every chunk automatically and asks for help only when a setting needs attention. Up to ${state.maxParallelRuns} model runs can run at a time.</p>
          <div class="chunk-timeline">${state.actionableTurns.map((turn, index) => `${index ? `<button type="button" class="chunk-divider" data-action="toggle-divider" data-message-uuid="${escapeHtml(turn.message_uuid)}" aria-pressed="${state.dividers.includes(turn.message_uuid)}" ${disabled || state.run ? "disabled" : ""}><span>${state.dividers.includes(turn.message_uuid) ? "Remove divider" : "Add divider"}</span></button>` : ""}
            <div class="chunk-turn"><span>${index + 1}</span><p>${escapeHtml(text(turn.request, "User turn").slice(0, 320))}</p></div>`).join("")}</div>
          <div class="chunk-choices" role="group" aria-label="Chunks to replay">${units.map((unit) => `<div class="chunk-choice${unit.key === state.activeUnitKey ? " chunk-choice--active" : ""}">
            <div>Chunk ${unit.number}<small> · Turns ${unit.first + 1}–${unit.last + 1} · ${unitConfigured(unit) ? "Ready" : state.unitDrafts[unit.key]?.problems?.length ? "Needs attention" : "Checks on start"}</small></div>
            <button type="button" class="button button--quiet" data-action="edit-unit" data-unit-key="${escapeHtml(unit.key)}" ${disabled ? "disabled" : ""}>${unit.key === state.activeUnitKey ? "Editing" : "Configure"}</button>
          </div>`).join("")}</div>
          <p class="chunk-help">The selected models apply to every chunk.</p>` : ""}
      </div>
    </section>`;
  }

  function recoverRangeRuns(active) {
    if (active.start_message_uuid && active.end_message_uuid) {
      state.replayMode = "chunks";
      state.selectedThreadId = text(active.thread_id);
    }
    return state.replayMode === "chunks";
  }

  function resultUnits(records = state.runs.map((entry) => entry.run)) {
    if (state.replayMode !== "chunks") return [];
    const ranges = records.filter((run) => run.start_message_uuid && run.end_message_uuid &&
      (!state.selectedThreadId || run.thread_id === state.selectedThreadId));
    const configured = replayUnits();
    if (configured.length && ranges.every((run) => configured.some((unit) =>
      unit.start_message_uuid === run.start_message_uuid && unit.end_message_uuid === run.end_message_uuid))) return configured;
    const units = new Map();
    for (const run of ranges.sort((a, b) =>
      text(a.started_at || a.created_at || a.run_id).localeCompare(text(b.started_at || b.created_at || b.run_id)) ||
      text(a.run_id).localeCompare(text(b.run_id)))) {
      const key = `${run.start_message_uuid}/${run.end_message_uuid}`;
      if (!units.has(key)) units.set(key, {key, number: units.size + 1,
        start_message_uuid: run.start_message_uuid, end_message_uuid: run.end_message_uuid});
    }
    return [...units.values()];
  }

  function resultUnit() {
    if (state.replayMode !== "chunks") return null;
    const entry = state.runs.find((item) => item.id === state.runId);
    return resultUnits().find((unit) => unit.number === entry?.unitNumber) || null;
  }

  function resultScope() {
    const unit = resultUnit();
    return unit ? `Chunk ${unit.number}${Number.isInteger(unit.first) ? ` · Turns ${unit.first + 1}–${unit.last + 1}` : ""}` : "";
  }

  function selectChunkResult(number) {
    const entries = state.runs.filter((entry) => entry.unitNumber === number);
    const current = state.runs.find((entry) => entry.id === state.runId);
    const entry = entries.find((item) => item.report && item.model === current?.model)
      || entries.find((item) => item.report) || entries[0];
    if (!entry) return;
    activateRun(entry.id);
    rememberActiveRun(entry.id);
    state.step = "results";
    render({ focus: true });
  }

  function renderChunkResults() {
    if (state.replayMode !== "chunks") return "";
    const selected = resultUnit();
    return `<section class="panel chunk-results" aria-labelledby="chunk-results-heading">
      <div class="panel__header"><div><h2 id="chunk-results-heading">Results by chunk</h2>
        <p>Each chunk has its own comparison report and judge results.</p></div></div>
      <nav class="panel__body chunk-result-options" aria-label="Chunk results">${resultUnits().map((unit) => {
        const entries = state.runs.filter((entry) => entry.unitNumber === unit.number);
        const reports = entries.filter((entry) => entry.report).length;
        const status = reports ? `${reports} ${reports === 1 ? "report" : "reports"}`
          : entries.some((entry) => !terminalRunStatus(entry.run?.status)) ? "Running"
            : entries.length ? "No report" : "Waiting";
        return `<button type="button" class="button button--quiet" data-action="select-chunk-result" data-unit-number="${unit.number}" aria-pressed="${selected?.number === unit.number}" ${entries.length ? "" : "disabled"}>
          <strong>Chunk ${unit.number}</strong>${Number.isInteger(unit.first) ? `<span>Turns ${unit.first + 1}–${unit.last + 1}</span>` : ""}<small>${status}</small></button>`;
      }).join("")}</nav>
    </section>`;
  }

  return {
    normalizeUnitDraft, rememberUnitDraft, restoreUnitState, replayUnits,
    activeUnit, rangeParameters, earlierFileAttribution, configurationIsLocked,
    editReplayUnit, changeReplayMode, toggleDivider, remainingUnits, unitConfigured, allUnitsConfigured, markUnitInspected,
    startChunks, advanceChunkQueue, chunkLaunchActive, renderPendingChunks, renderRangeSelection, resultUnit, selectChunkResult, renderChunkResults,
    recoverRangeRuns, resultUnits, resultScope
  };
};

// Thread batches share the same configuration and individual report surfaces.
globalThis.createReplayBatchUI = function (context) {
  const {
    state, text, isObject, asArray, stringList, normalizeUnitDraft, normalizeReviewDraft, threadTitle, threadId, reviewProblems, preparationBlockers, replayDetailsLoading, selectionNeedsRefresh, render, selectThread, setSelectedModels, refreshAttributionAndContinue, reviewPayload, callTool, ownedRunRecord, currentRunId, clearDraft, rememberActiveRun, activateRun, beginPolling, applyPreparationInspection, formatCost, formatDate, terminalRunStatus, successfulRunStatus, pageHeading, escapeHtml, setStartPending, clearRestoredDraft
  } = context;
  function normalizeSelectedThreads(values) {
    const seen = new Set();
    return asArray(values).flatMap((entry) => {
      const id = text(entry?.id);
      if (!id || seen.has(id)) return [];
      seen.add(id);
      return [{ id, number: Number(entry.number) || seen.size,
        thread: { imported_thread_id: id, title: threadTitle(entry.thread),
          source_path: text(entry.thread?.source_path), project_dir: text(entry.thread?.project_dir) },
        draft: normalizeUnitDraft(entry.draft) }];
    });
  }

  function isThreadBatch() {
    return Boolean(state.batchId) || state.selectedThreads.length > 1;
  }

  function restoreBatch(payload) {
    const batch = payload?.batch;
    if (!isObject(batch) || !text(batch.id)) return false;
    state.batchId = text(batch.id);
    state.batch = batch;
    state.replayMode = "whole";
    state.runErrors = asArray(batch.errors).filter(isObject);
    if (asArray(batch.models).length) setSelectedModels(batch.models);
    if (asArray(batch.threads).length) state.selectedThreads = normalizeSelectedThreads(batch.threads.map((entry, index) => ({
      id: entry.thread_id, number: index + 1, thread: { title: entry.thread_title },
    })));
    return true;
  }

  function rememberBatchThread() {
    if (!isThreadBatch() || state.replayMode !== "whole" || state.run || !state.reviewDraft || state.busy === "inspection") return;
    const entry = state.selectedThreads.find((item) => item.id === state.selectedThreadId);
    if (!entry || state.reviewDraft.thread_id !== entry.id) return;
    entry.draft = normalizeUnitDraft({ reviewDraft: state.reviewDraft,
      classifications: state.classifications, attributionEdits: state.attributionEdits,
      promptGeneration: state.promptGeneration, detailsOpen: state.configurationDetailsOpen,
      problems: [...reviewProblems(), ...preparationBlockers(), ...(state.error ? [state.error] : [])],
      configured: !replayDetailsLoading() && !selectionNeedsRefresh() &&
        !reviewProblems().length && !preparationBlockers().length && !state.error });
  }

  function toggleThreadSelection(id, number) {
    if (state.busy || state.run || state.batchPreparing) return;
    rememberBatchThread();
    const selected = state.selectedThreads.find((item) => item.id === id);
    if (selected) state.selectedThreads = state.selectedThreads.filter((item) => item.id !== id);
    else if (state.selectedThreads.length < 100) {
      const thread = state.threads.find((item) => threadId(item) === id);
      if (thread) state.selectedThreads.push({ id, number, thread, draft: null });
    }
    render();
  }

  async function configureSelectedThreads() {
    if (state.busy || state.run || !state.selectedThreads.length) return;
    const entry = state.selectedThreads.find((item) => item.id === state.selectedThreadId)
      || state.selectedThreads[0];
    if (state.selectedThreads.length === 1 && entry.id === state.selectedThreadId && state.inspection) {
      state.step = "configure";
      render({ focus: true });
      return;
    }
    await selectBatchThread(entry.id);
  }

  async function selectBatchThread(id, { forStart = false } = {}) {
    if (state.busy || state.run || (state.batchPreparing && !forStart)) return;
    rememberBatchThread();
    const entry = state.selectedThreads.find((item) => item.id === id);
    if (!entry) return;
    const saved = id === state.selectedThreadId && state.replayMode === "chunks"
      ? state.wholeDraft : entry.draft;
    await selectThread(id, entry.number, { selected: entry.thread, saved });
    rememberBatchThread();
  }

  function renderBatchConfiguration() {
    rememberBatchThread();
    return `<section class="panel" aria-labelledby="selected-thread-heading">
      <div class="panel__header"><div><h2 id="selected-thread-heading">${state.selectedThreads.length} selected threads</h2>
        <p>Each thread keeps its own configuration. The selected models apply to every thread.</p></div>
        <button type="button" class="button button--quiet" data-action="configuration-back" ${state.busy || state.batchPreparing ? "disabled" : ""}>Choose threads</button></div>
      <div class="panel__body chunk-choices">${state.selectedThreads.map((entry) => `<div class="chunk-choice${entry.id === state.selectedThreadId ? " chunk-choice--active" : ""}">
        <div><strong>${escapeHtml(threadTitle(entry.thread))}</strong><small> · ${entry.draft?.problems.length ? "Needs attention" : entry.draft?.configured ? "Ready" : "Checks on start"}</small></div>
        <button type="button" class="button button--quiet" data-action="configure-thread" data-thread-id="${escapeHtml(entry.id)}" ${state.busy || state.batchPreparing ? "disabled" : ""}>${entry.id === state.selectedThreadId ? "Editing" : "Configure"}</button>
      </div>`).join("")}</div></section>`;
  }

  async function startThreadBatch() {
    if (!isThreadBatch() || state.busy || state.run || state.batchId || state.batchPreparing) return;
    rememberBatchThread();
    state.batchPreparing = true;
    let launchRequested = false;
    state.error = "";
    state.notice = "";
    try {
      const configurations = [];
      for (const entry of state.selectedThreads) {
        await selectBatchThread(entry.id, { forStart: true });
        if (selectionNeedsRefresh()) await refreshAttributionAndContinue();
        if (reviewProblems().length || replayDetailsLoading() || state.error) {
          state.configurationDetailsOpen = true;
          state.notice = `${threadTitle(entry.thread)} needs attention. Review its configuration, then start the threads again.`;
          return;
        }
        configurations.push({ ...reviewPayload(), thread_title: threadTitle(entry.thread) });
      }
      state.busy = "prepare";
      render();
      const preparation = await callTool("prepare_run", { configurations });
      state.busy = "";
      if (preparation.ready !== true && preparation.can_run !== true) {
        const blocked = asArray(preparation.preparations).find((entry) =>
          entry.preparation?.ready !== true && entry.preparation?.can_run !== true);
        if (blocked) {
          await selectBatchThread(blocked.thread_id, { forStart: true });
          state.preparation = blocked.preparation;
          applyPreparationInspection(blocked.preparation);
        } else state.preparation = preparation;
        state.configurationDetailsOpen = true;
        state.notice = "A thread needs attention. Review its configuration, then start the threads again.";
        return;
      }
      state.busy = "start";
      launchRequested = true;
      setStartPending(true);
      render();
      const payload = await callTool("start_run", {
        ...preparation.run_config, approved: true,
        prepare_token: preparation.prepare_token || preparation.approval?.prepare_token,
      });
      state.runs = asArray(payload.runs).flatMap((item) => {
        const run = ownedRunRecord(item), id = currentRunId(item);
        return run && id ? [{ id, run, model: text(item.model || run.model) }] : [];
      });
      state.runErrors = asArray(payload.errors).filter(isObject);
      restoreBatch(payload);
      state.batchId = state.batchId || text(state.runs[0]?.run.batch_id || payload.batch_id);
      if (!state.runs.length && !state.batchId) throw new Error("The controller started no durable run.");
      state.batchView = "aggregate";
      activateRun(state.runs[0]?.id);
      rememberActiveRun(state.runId);
      state.step = "run";
      clearDraft();
      clearRestoredDraft();
      beginPolling();
    } catch (error) {
      state.error = error instanceof Error ? error.message : String(error);
      if (launchRequested) {
        try {
          const payload = await callTool("get_state", {});
          const snapshot = payload.state || payload;
          if (restoreBatch(snapshot)) {
            state.step = "run";
            beginPolling();
          }
        } catch { /* Reopening the controller reconciles the durable launch. */ }
      }
    } finally {
      setStartPending(false);
      state.batchPreparing = false;
      state.busy = "";
      render({ focus: true });
    }
  }

  function batchThreadResults() {
    const threads = new Map();
    for (const entry of asArray(state.batch?.threads)) {
      threads.set(entry.thread_id, { id: entry.thread_id, title: entry.thread_title, entries: [] });
    }
    for (const entry of state.runs) {
      const id = text(entry.run.thread_id);
      if (!threads.has(id)) threads.set(id, { id, title: text(entry.run.thread_title,
        threadTitle(state.selectedThreads.find((item) => item.id === id)?.thread)), entries: [] });
      threads.get(id).entries.push(entry);
    }
    return [...threads.values()];
  }

  function selectThreadResult(id) {
    const entries = state.runs.filter((entry) => entry.run.thread_id === id);
    const current = state.runs.find((entry) => entry.id === state.runId);
    const entry = entries.find((item) => item.report && item.model === current?.model)
      || entries.find((item) => item.report);
    if (!entry) return;
    activateRun(entry.id);
    rememberActiveRun(entry.id);
    state.batchView = "individual";
    state.step = "results";
    render({ focus: true });
  }

  function renderBatchResultsNavigation() {
    if (!isThreadBatch()) return "";
    const threads = batchThreadResults();
    const finished = threads.filter(({ id, entries }) =>
      !state.batch?.starting && entries.every((entry) => terminalRunStatus(entry.run.status)) &&
      entries.length + state.runErrors.filter((error) => error.thread_id === id).length >= state.selectedModels.length).length;
    return `<section class="panel chunk-results" aria-labelledby="thread-results-heading">
      <div class="panel__header"><div><h2 id="thread-results-heading">${finished} of ${threads.length} threads finished</h2>
        <p>Completed comparisons are available while the other threads continue.</p></div></div>
      <nav class="panel__body chunk-result-options" aria-label="Thread results">
        <button type="button" class="button button--quiet" data-action="aggregate-results" aria-pressed="${state.batchView === "aggregate"}"><strong>Aggregate</strong><small>${state.runs.filter((entry) => entry.report).length} comparisons ready</small></button>
        ${threads.map(({ id, title, entries }) => {
          const reports = entries.filter((entry) => entry.report).length;
          const active = state.batchView === "individual" && state.run?.thread_id === id;
          const total = state.batch?.models?.length || state.selectedModels.length;
          const status = reports ? `${reports} of ${total} comparisons ready`
            : entries.some((entry) => !terminalRunStatus(entry.run.status)) ? "Running" : "No report";
          return `<button type="button" class="button button--quiet" data-action="thread-results" data-thread-id="${escapeHtml(id)}" aria-pressed="${active}" ${reports ? "" : "disabled"}><strong>${escapeHtml(title)}</strong><small>${status}</small></button>`;
        }).join("")}
      </nav></section>`;
  }

  function aggregateModelResults() {
    return stringList([...state.runs.map((entry) => entry.model), ...asArray(state.batch?.models)]).map((model) => {
      const entries = state.runs.filter((entry) => entry.model === model);
      const reports = entries.flatMap((entry) => entry.report ? [entry.report] : []);
      const paired = reports.filter((report) => !report.historical_usage_shared &&
        report.estimated_cost?.claude?.status !== "shared" &&
        [report.estimated_cost?.claude?.usd, report.estimated_cost?.codex?.usd]
          .every((value) => Number.isFinite(value) && value >= 0));
      const quality = { wins: 0, ties: 0, losses: 0, unavailable: 0 };
      for (const report of reports) {
        const evaluation = report.evaluation || {};
        const mapping = evaluation.candidate_mapping || { A: "claude", B: "codex" };
        const totals = evaluation.totals || {};
        if (!Number.isFinite(totals.A) || !Number.isFinite(totals.B) ||
            new Set([mapping.A, mapping.B]).size !== 2 ||
            ![mapping.A, mapping.B].every((value) => ["claude", "codex"].includes(value))) {
          quality.unavailable += 1;
          continue;
        }
        const rounded = (value) => report.schema_version === 2 ? value : Math.round(value * 100);
        const claude = rounded(totals[mapping.A === "claude" ? "A" : "B"]);
        const codex = rounded(totals[mapping.A === "codex" ? "A" : "B"]);
        quality[codex === claude ? "ties" : codex > claude ? "wins" : "losses"] += 1;
      }
      const claude = paired.reduce((sum, report) => sum + report.estimated_cost.claude.usd, 0);
      const codex = paired.reduce((sum, report) => sum + report.estimated_cost.codex.usd, 0);
      return { model, total: state.batch?.thread_count || entries.length, ready: reports.length, paired: paired.length,
        failed: entries.filter((entry) => terminalRunStatus(entry.run.status) && !successfulRunStatus(entry.run.status)).length +
          state.runErrors.filter((error) => error.model === model).length,
        claude, codex, quality };
    });
  }

  function renderAggregateResults() {
    return `${pageHeading("Comparison results", "All selected threads", "Totals update as comparisons finish. Costs are API-equivalent estimates for completed comparisons, not subscription bills.")}
      ${renderBatchResultsNavigation()}
      <div class="variant-grid">${aggregateModelResults().map((item) => {
        const model = state.models.find(({ id }) => id === item.model)?.label || item.model;
        const difference = item.paired && item.claude > 0 ? (item.claude - item.codex) / item.claude * 100 : null;
        return `<section class="panel"><div class="panel__header"><div><h2>${escapeHtml(model)}</h2>
          <p>${item.ready} of ${item.total} comparisons ready${item.failed ? ` · ${item.failed} failed or cancelled` : ""}</p></div></div>
          <div class="panel__body"><dl class="metrics">
            <div class="metric"><dt>Historical Claude</dt><dd>${item.paired ? formatCost(item.claude) : "Unavailable"}</dd></div>
            <div class="metric"><dt>Codex replay</dt><dd>${item.paired ? formatCost(item.codex) : "Unavailable"}</dd></div>
            <div class="metric"><dt>Cost difference</dt><dd>${difference === null ? "Unavailable" : `${Math.abs(difference).toFixed(1)}% ${difference >= 0 ? "lower" : "higher"}`}</dd></div>
            <div class="metric"><dt>LLM judge</dt><dd>${item.quality.wins} wins · ${item.quality.ties} ties · ${item.quality.losses} losses</dd></div>
          </dl><p class="chunk-help">Cost coverage: ${item.paired} of ${item.ready} completed comparisons. Missing or shared historical usage is excluded from both cost totals.${item.quality.unavailable ? ` ${item.quality.unavailable} judge results unavailable.` : ""}</p></div>
        </section>`;
      }).join("")}</div>`;
  }

  function renderThreadStep() {
    const threads = state.threads;
    return `
      ${pageHeading("Historical source", "Choose imported Claude threads",
        "Select one or more conversations to replay. Claude is not rerun.")}
      ${state.busy === "inspection"
        ? `<div class="banner" role="status"><span class="spinner" aria-hidden="true"></span><div class="banner__body"><strong>Inspecting ${escapeHtml(threadTitle(state.selectedThread || {}))}</strong><p>Loading the thread, models, baseline, and capabilities…</p></div></div>` : ""}
      ${state.run
        ? `<div class="banner banner--warning" role="status"><span class="banner__mark" aria-hidden="true">i</span><div class="banner__body"><strong>A replay is already ${terminalRunStatus(state.run.status) ? "recorded" : "running"}</strong><p>To run another replay, open a new Codex task and invoke Codex Bakeoff.</p></div></div>` : ""}
      <section class="panel" aria-labelledby="threads-heading">
        <div class="panel__header">
          <div><h2 id="threads-heading">${state.threadSource === "sample" ? "Sample data" : "Imported threads"}</h2>
            <p>${state.loading ? "Loading local imports…" : `${threads.length} of ${state.totalThreads} shown`}</p></div>
          <button type="button" class="button button--quiet" data-action="refresh-threads" ${state.busy ? "disabled" : ""}>Refresh</button>
        </div>
        <div class="panel__body">
          <div class="thread-sources" aria-label="Thread source">
            ${[["imported", "Imported threads", state.importedTotal], ["sample", "Sample data", state.sampleTotal]]
              .map(([source, label, total]) => `<button type="button" class="button button--quiet" data-action="thread-source" data-source="${source}" aria-pressed="${state.threadSource === source}">${label} (${total})</button>`).join("")}
          </div>
          <label class="sr-only" for="thread-search">Search imported threads</label>
          <div class="search"><span class="search__icon" aria-hidden="true"></span><input id="thread-search" type="search" autocomplete="off" placeholder="Search by title, project, model, or ID" value="${escapeHtml(state.query)}"></div>
          ${state.loading
            ? `<div class="thread-list" aria-label="Loading threads"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`
            : threads.length
              ? `<div class="thread-list">${threads.map((thread, index) => {
                const id = threadId(thread);
                const number = index + 1;
                const project = text(thread.project_dir, "No project detected");
                const complexity = Number(thread.complexity_score);
                const calls = Number(thread.tool_call_count);
                const checked = state.selectedThreads.some((entry) => entry.id === id);
                return `<label class="thread thread--select"><input type="checkbox" data-thread-selection="${escapeHtml(id)}" data-thread-number="${number}" aria-label="Select ${escapeHtml(threadTitle(thread))}" ${checked ? "checked" : ""} ${!id || state.busy || state.run || !checked && state.selectedThreads.length >= 100 ? "disabled" : ""}><span><span class="thread__title"><span class="thread__number">#${number}</span>${escapeHtml(threadTitle(thread))}</span><span class="thread__meta"><span>${escapeHtml(formatDate(thread.created_at))}</span><span class="thread__project" title="${escapeHtml(project)}">${escapeHtml(project)}</span>${Number.isFinite(complexity) ? `<span>Complexity ${complexity}</span>` : ""}${Number.isFinite(calls) ? `<span>${calls} tool calls</span>` : ""}${thread.claude_model ? `<span>${escapeHtml(thread.claude_model)}</span>` : ""}</span></span></label>`;
              }).join("")}</div>`
              : `<div class="empty">${state.query
                  ? "No threads match this search."
                  : state.threadSource === "sample"
                    ? "No sample data is available."
                    : "No Claude conversations imported. Import one into Codex, then select Refresh."}</div>`
          }
          ${!state.loading && state.threads.length < state.totalThreads
            ? `<button type="button" class="button button--quiet" data-action="load-more-threads" ${state.busy ? "disabled" : ""}>Load more threads</button>`
            : ""}
        </div></section>
      <div class="footer-actions"><span class="footer-actions__note">${state.selectedThreads.length} selected${state.selectedThreads.length >= 100 ? " · Maximum 100 threads" : ""}</span>
        <button type="button" class="button button--primary" data-action="configure-selected" ${state.busy || state.run || !state.selectedThreads.length ? "disabled" : ""}>Configure ${state.selectedThreads.length === 1 ? "thread" : "threads"}</button></div>
    `;
  }

  return { normalizeSelectedThreads, isThreadBatch, restoreBatch, rememberBatchThread, toggleThreadSelection, configureSelectedThreads, selectBatchThread, renderBatchConfiguration, startThreadBatch, batchThreadResults, selectThreadResult, renderBatchResultsNavigation, aggregateModelResults, renderAggregateResults, renderThreadStep };
};

// Result summaries use the same validation and formatting as the controller.
globalThis.createReplayOutcomeSummary = function ({ isObject, formatCost }) {
  return function outcomeSummary(evaluation, schemaVersion = 3, costs = {}) {
    const mapping = isObject(evaluation.candidate_mapping) ? evaluation.candidate_mapping : { A: "claude", B: "codex" };
    const { A: a, B: b } = isObject(evaluation.totals) ? evaluation.totals : {};
    const [historical, replay] = [costs.claude?.usd, costs.codex?.usd];
    const shared = costs.claude?.status === "shared";
    const validCost = (value) => Number.isFinite(value) && value >= 0;
    const priceWinner = shared || ![historical, replay].every(validCost) ? null
      : historical === replay ? "Tie" : historical < replay ? "Historical Claude" : "Codex replay";
    const savings = priceWinner && priceWinner !== "Tie"
      ? (1 - Math.min(historical, replay) / Math.max(historical, replay)) * 100 : 0;
    const roundedSavings = Math.round(savings);
    const price = {
      winner: priceWinner,
      values: [
        { name: "Historical Claude", value: shared ? "Shared usage" : validCost(historical) ? formatCost(historical) : "Unavailable" },
        { name: "Codex replay", value: validCost(replay) ? formatCost(replay) : "Unavailable" },
      ],
      detail: shared ? "No standalone usage — shared across chunks"
        : !priceWinner ? "Two valid cost estimates are required."
        : priceWinner === "Tie" ? "Same estimated cost"
        : `${roundedSavings === 0 ? "<1" : roundedSavings === 100 && savings < 100 ? ">99" : roundedSavings}% lower estimated cost`,
    };
    const legacy = schemaVersion === 2;
    const score = (value) => legacy ? `${value}` : `${Math.round(value * 100)}%`;
    const [roundedA, roundedB] = legacy ? [a, b] : [a, b].map((value) => Math.round(value * 100));
    const validScores = Number.isFinite(a) && Number.isFinite(b);
    const detail = legacy
      ? "Each dimension win awards one point; ties and N/A award zero."
      : "Scores average applicable dimension percentages; N/A excluded.";
    const name = (label) => ({ codex: "Codex replay", claude: "Historical Claude" })[mapping[label]] || `Candidate ${label}`;
    const difference = Math.abs(roundedA - roundedB);
    const quality = {
      winner: !validScores ? null : roundedA === roundedB ? "Tie" : name(roundedA > roundedB ? "A" : "B"),
      values: [a, b].map((value, index) => ({ name: name(index === 0 ? "A" : "B"), value: Number.isFinite(value) ? score(value) : "Unavailable" })),
      detail: !validScores ? "No validated score" : difference === 0 ? "Same quality score"
        : `${difference} ${legacy ? "point" : "percentage point"}${difference === 1 ? "" : "s"} higher`,
    };
    const summary = !price.winner || !quality.winner ? "Partial comparison"
      : price.winner === "Tie" && quality.winner === "Tie" ? "Price and quality tied"
      : price.winner === "Tie" || quality.winner === "Tie" ? "One metric tied"
      : price.winner === quality.winner ? `${price.winner} wins both` : "Split winners";
    const labeledScores = validScores ? quality.values.map(({ name, value }) => `${name}: ${value}`).join("\n") : "No validated score";
    return { price, quality, summary, score: labeledScores, detail };
  };
};
