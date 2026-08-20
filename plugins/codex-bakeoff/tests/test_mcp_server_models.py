"""Parallel Codex model selection, execution, and controller presentation tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PLUGIN_ROOT / "mcp" / "server.py"
CONTROLLER_PATH = PLUGIN_ROOT / "mcp" / "controller.html"


def load_server():
    spec = importlib.util.spec_from_file_location("replay_mcp_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load the MCP server.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONTROLLER_HARNESS = r"""
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const extract = (start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  if (first < 0 || last <= first) throw new Error(`Missing controller code: ${start}`);
  return source.slice(first, last);
};
"""


def _normalize_controller_models(values: list[object]) -> list[dict[str, object]]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const normalize = new Function([
  extract("      const isObject =", "      const escapeHtml ="),
  extract("      function normalizeModels(values)", "      function setSelectedModels"),
  "return normalizeModels;",
].join("\n"))();
process.stdout.write(JSON.stringify(normalize(JSON.parse(process.argv[2]))));
"""
    )
    result = subprocess.run(
        ["node", "-e", harness, str(CONTROLLER_PATH), json.dumps(values)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def _selected_controller_models(
    available: list[dict[str, object]], selected: list[str]
) -> list[str]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const choose = new Function("available", "selected", [
  extract("      const DEFAULT_MODEL_IDS = [", "      const THREAD_PAGE_SIZE ="),
  extract("      const isObject =", "      function normalizeReviewDraft"),
  extract("      function normalizeModels(values)", "      function inspectionParts"),
  extract("      function selectedModelDefault()", "      function recordPromptGeneration"),
  "const state = { models: normalizeModels(available), selectedModels: [], reviewDraft: null };",
  "setSelectedModels(selected); return state.selectedModels;",
].join("\n"));
process.stdout.write(JSON.stringify(choose(
  JSON.parse(process.argv[2]), JSON.parse(process.argv[3])
)));
"""
    )
    result = subprocess.run(
        ["node", "-e", harness, str(CONTROLLER_PATH), json.dumps(available), json.dumps(selected)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def _render_controller_run_variants(
    runs: list[dict[str, object]],
    errors: list[dict[str, object]],
    *,
    results: bool = False,
    active_run_id: str = "",
) -> str:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const render = new Function("state", [
  extract("      const DEFAULT_MODEL_IDS = [", "      const THREAD_PAGE_SIZE ="),
  extract("      const isObject =", "      const safeJson ="),
  extract("      const titleCase =", "      const formatDate ="),
  extract("      const formatDuration =", "      const formatTokens ="),
  extract("      const formatCost =", "      function unwrapToolResult"),
  extract("      function terminalRunStatus", "      function stepIndex"),
  extract("      function outcomeSummary(evaluation,", "      function comparisonClass"),
  extract("      function comparisonClass", "      function usageMetricRows"),
  extract("      function renderRunVariants(results = false)", "      function renderRunStep()"),
  "return renderRunVariants;",
].join("\n"))(JSON.parse(process.argv[2]));
process.stdout.write(render(process.argv[3] === "results"));
"""
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            harness,
            str(CONTROLLER_PATH),
            json.dumps({"runs": runs, "runErrors": errors, "runId": active_run_id}),
            "results" if results else "run",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout


def _restore_controller_runs(
    server_state: dict[str, object],
    *,
    remembered_run_id: str,
    recovered_state: dict[str, object] | None = None,
) -> dict[str, object]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const context = new Function("serverState", "rememberedRunId", "recoveredState", [
  `const state = {
    step: "thread", runs: [], runErrors: [], models: [],
    selectedThreadId: "", reviewDraft: null,
  };`,
  "const events = [];",
  "let draftPersistenceReady = false; let restoredDraft = null;",
  "const THREAD_PAGE_SIZE = 20;",
  "const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);",
  "const asArray = value => Array.isArray(value) ? value : [];",
  "const text = (value, fallback = '') => typeof value === 'string' ? value : fallback;",
  "const normalizeModels = asArray;",
  "const setSelectedModels = values => { state.selectedModels = values; };",
  "const loadDraft = () => null;",
  "const loadActiveRunId = () => rememberedRunId;",
  "const currentRunId = run => text(run.run_id || run.id);",
  "const terminalRunStatus = status => ['completed', 'failed', 'cancelled'].includes(status);",
  "const successfulRunStatus = status => status === 'completed';",
  "const render = () => {}; const startControllerHeartbeat = () => {};",
  "const clearDraft = () => {}; const rememberActiveRun = id => events.push(['remember', id]);",
  "const beginPolling = () => events.push(['poll']);",
  "const activateRun = id => { state.runId = id; state.run = state.runs.find(run => run.id === id).run; };",
  `const callTool = async (name, args) => name === 'get_state'
    ? { state: args.run_id ? recoveredState : serverState }
    : { threads: [], total: 0 };`,
  "const window = { setTimeout: () => events.push(['refresh']) };",
  extract("      async function initialize()", "      async function selectThread"),
  "return { initialize, state, events };",
].join("\n"))(
  JSON.parse(process.argv[2]), process.argv[3], JSON.parse(process.argv[4])
);
context.initialize().then(() => {
  process.stdout.write(JSON.stringify({
    step: context.state.step,
    run_id: context.state.runId,
    models: context.state.selectedModels,
    runs: context.state.runs,
    errors: context.state.runErrors,
    events: context.events,
  }));
}).catch(error => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
"""
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            harness,
            str(CONTROLLER_PATH),
            json.dumps(server_state),
            remembered_run_id,
            json.dumps(recovered_state or server_state),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def _refresh_controller_runs(runs: list[dict[str, object]]) -> dict[str, object]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const context = new Function("runs", [
  "const state = { runs, runId: runs[0].id, step: 'run', busy: '', report: null };",
  "let attempts = 0; let scheduled = 0;",
  "const isObject = value => value !== null && typeof value === 'object';",
  "const text = value => typeof value === 'string' ? value : '';",
  "const terminalRunStatus = status => ['completed', 'failed'].includes(status);",
  "const successfulRunStatus = status => status === 'completed';",
  "const ownedRunRecord = value => value.run || value;",
  `const callTool = async (name, { run_id }) => {
    if (name !== 'get_report') throw new Error('Unexpected tool');
    if (run_id === 'run-terra' && attempts++ === 0) throw new Error('Temporary report failure');
    return { report: { model: run_id } };
  };`,
  "const activateRun = id => { state.runId = id; state.report = state.runs.find(run => run.id === id).report || null; };",
  "const render = () => {}; const schedulePoll = () => { scheduled += 1; };",
  extract("      async function refreshRun(", "      async function cancelRun("),
  "return { refreshRun, state, scheduled: () => scheduled };",
].join("\n"))(JSON.parse(process.argv[2]));
context.refreshRun({ continuePolling: true }).then(async () => {
  const first = { step: context.state.step, scheduled: context.scheduled() };
  await context.refreshRun({ continuePolling: true });
  process.stdout.write(JSON.stringify({
    first,
    scheduled: context.scheduled(),
    reports: context.state.runs.map(run => Boolean(run.report)),
  }));
}).catch(error => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
"""
    )
    result = subprocess.run(
        ["node", "-e", harness, str(CONTROLLER_PATH), json.dumps(runs)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


class McpServerModelTests(unittest.TestCase):
    def test_parallel_variants_share_one_historical_review_artifact(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths: list[Path | None] = []
            for model in ("gpt-5.6-terra", "gpt-5.6-luna"):
                run_directory = root / model
                run_directory.mkdir()
                server._write_json(
                    run_directory / server.STATE_NAME,
                    {
                        "models": ["gpt-5.6-terra", "gpt-5.6-luna"],
                        "prepare_token_hash": "a" * 64,
                        "configuration_fingerprint": "b" * 64,
                    },
                )
                with server._historical_review_guard(run_directory) as path:
                    paths.append(path)

            self.assertEqual(paths[0], paths[1])
            self.assertIsNotNone(paths[0])
            self.assertEqual(paths[0].parent, root / ".shared-reviews")

    def test_reviewer_falls_back_to_discovered_or_successful_implementation_model(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(
                server,
                "_engine",
                return_value={
                    "evaluators": [{"id": "codex", "available": True, "model": "gpt-5.6-terra"}]
                },
            ):
                self.assertEqual(
                    server._review_evaluator(
                        root,
                        historical_evaluation=None,
                        implementation_model="gpt-5.6-luna",
                        timeout=60,
                    ),
                    "gpt-5.6-terra",
                )
            with mock.patch.object(server, "_engine", return_value={"evaluators": []}):
                self.assertEqual(
                    server._review_evaluator(
                        root,
                        historical_evaluation=None,
                        implementation_model="gpt-custom",
                        timeout=60,
                    ),
                    "gpt-custom",
                )

    def test_completed_sibling_state_summaries_track_recomputed_evaluations(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sibling = root / "sibling"
            sibling.mkdir()
            evaluation = {"totals": {"A": 0.75, "B": 0.5}}
            server._write_json(
                sibling / server.STATE_NAME,
                {"status": "completed", "report_summary": {"evaluation": {"totals": {}}}},
            )
            server._write_json(
                sibling / "report.json",
                {"winner": "claude", "evaluation": evaluation},
            )
            shared = root / ".shared-reviews" / "evaluation.json"
            server._write_json(shared, {"run_directories": [str(sibling)]})

            server._sync_historical_review_summaries(shared)

            state = server._read_json(sibling / server.STATE_NAME)
            self.assertEqual(
                state["report_summary"], {"winner": "claude", "evaluation": evaluation}
            )

    def test_existing_historical_baseline_allows_parallel_variant_reviews(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runs: list[Path] = []
            for model in ("gpt-5.6-terra", "gpt-5.6-luna"):
                run_directory = root / model
                run_directory.mkdir()
                server._write_json(
                    run_directory / server.STATE_NAME,
                    {
                        "models": ["gpt-5.6-terra", "gpt-5.6-luna"],
                        "prepare_token_hash": "a" * 64,
                        "configuration_fingerprint": "b" * 64,
                    },
                )
                runs.append(run_directory)
            with server._historical_review_guard(runs[0]) as shared:
                self.assertIsNotNone(shared)
                server._write_json(
                    shared,
                    {"evaluator_model": "gpt-5.6-terra", "run_directories": []},
                )
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def fake_engine(command: str, arguments=(), **kwargs):
                if command == "evaluate":
                    self.assertIn("--evaluator-model", arguments)
                    self.assertIn("gpt-5.6-terra", arguments)
                    return {"task_requests": [{"purpose": "evaluation"}]}
                if command == "collect-native-results":
                    return {"native_results_path": str(kwargs["run_directory"] / "reviews.json")}
                if command == "complete-evaluation":
                    return {"status": "completed"}
                raise AssertionError(command)

            def run_review(run_directory: Path, requests, **kwargs):
                barrier.wait(timeout=3)
                return [run_directory / "review.json"]

            def invoke(run_directory: Path) -> None:
                try:
                    server._review_replay(
                        run_directory,
                        timeout=60,
                        implementation_model=run_directory.name,
                    )
                except BaseException as error:
                    errors.append(error)

            with (
                mock.patch.object(server, "_update_state"),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(server, "_run_review_requests", side_effect=run_review),
            ):
                workers = [threading.Thread(target=invoke, args=(run,)) for run in runs]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertFalse(errors, errors)

    def test_configuration_schemas_allow_all_available_codex_models(self) -> None:
        server = load_server()

        for approval in (False, True):
            with self.subTest(approval=approval):
                models = server._configuration_schema(approval=approval)["properties"]["models"]
                self.assertEqual(models["type"], "array")
                self.assertEqual(models["minItems"], 1)
                self.assertEqual(models["maxItems"], server.MAX_REPLAY_MODELS)
                self.assertEqual(server.MAX_REPLAY_MODELS, 8)
                self.assertEqual(models["items"], {"type": "string", "minLength": 1})

    def test_multi_model_configuration_accepts_any_models_and_rejects_invalid_selection(
        self,
    ) -> None:
        server = load_server()
        selected = ["gpt-5.6-luna", "gpt-4o", "gpt-5.5-sol", "custom-model"]
        configuration = server._normalized_configuration(
            {"thread_id": "thread-1", "model": selected[0], "models": selected}
        )

        self.assertEqual(configuration["model"], selected[0])
        self.assertEqual(configuration["models"], selected)
        self.assertNotIn(
            "models",
            server._normalized_configuration({"thread_id": "thread-1", "model": "gpt-test"}),
        )

        for models in (
            [],
            "gpt-5.6-luna",
            ["gpt-5.6-luna", "gpt-5.6-luna"],
            ["gpt-5.6-luna", "   "],
            ["gpt-5.6-luna", "gpt\x00invalid"],
            ["gpt-5.6-luna", 5],
            [f"gpt-model-{index}" for index in range(server.MAX_REPLAY_MODELS + 1)],
            ["gpt-5.6-luna"] * (server.MAX_SELECTION_ITEMS + 1),
        ):
            with self.subTest(models=models), self.assertRaises(server.ControllerError):
                server._normalized_configuration(
                    {"thread_id": "thread-1", "model": "gpt-5.6-luna", "models": models}
                )

        with self.assertRaises(server.ControllerError):
            server._normalized_configuration(
                {
                    "thread_id": "thread-1",
                    "model": "gpt-5.6-terra",
                    "models": selected,
                }
            )

    def test_controller_offers_all_available_model_multiselect_and_parallel_polling(self) -> None:
        controller = CONTROLLER_PATH.read_text(encoding="utf-8")
        configure = controller.split("function renderReplayConfiguration", 1)[1].split(
            "function normalizedPhases", 1
        )[0]

        self.assertIn("Available Codex models", configure)
        self.assertIn("state.models.length ? state.models.map", configure)
        self.assertIn('data-model-variant="', configure)
        self.assertIn('type="checkbox"', configure)
        self.assertIn("draft.models.length>=8", configure)
        self.assertIn(
            "Select one or more models. Selected models run in parallel.",
            configure,
        )
        self.assertNotIn('<select id="review-model">', configure)
        self.assertIn('id="review-model" type="text"', configure)
        self.assertIn('"review-model": "model"', controller)
        self.assertIn('if (reviewKey === "model") setSelectedModels([target.value])', controller)
        self.assertIn("Promise.allSettled", controller)
        refresh = controller.split("async function refreshRun", 1)[1].split(
            "async function cancelRun", 1
        )[0]
        self.assertIn("const changed=pending.some(e=>successfulRunStatus(e.run?.status))", refresh)
        self.assertIn("successfulRunStatus(e.run?.status)&&(!e.report||changed)", refresh)
        for variant in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            with self.subTest(variant=variant):
                self.assertIn(f'"{variant}"', controller)

    def test_controller_preserves_all_discovered_models_in_discovery_order(self) -> None:
        models = _normalize_controller_models(
            [
                {"id": "gpt-4o", "label": "Unsupported"},
                {"id": "gpt-5.6-sol", "label": "Older Sol entry"},
                {"slug": "gpt-5.6-luna", "name": "Moon"},
                {"model": "gpt-5.6-terra", "display_name": "Terra"},
                {"id": "gpt-5.6-sol", "label": "Available Sol"},
                {"id": "gpt-5.6-nebula", "label": "Unlisted variant"},
                {"id": "   "},
                None,
                7,
            ]
        )

        self.assertEqual(
            [model["id"] for model in models],
            ["gpt-4o", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-nebula"],
        )
        self.assertEqual(models[1]["label"], "Available Sol")
        self.assertEqual(_normalize_controller_models(["gpt-4o"])[0]["id"], "gpt-4o")

    def test_controller_defaults_to_available_sol_terra_and_luna_models(self) -> None:
        available = [
            {"id": "gpt-4o", "recommended": True},
            {"id": "gpt-5.6-luna"},
            {"id": "gpt-5.6-sol"},
            {"id": "gpt-5.6-terra"},
            {"id": "custom-model"},
        ]

        self.assertEqual(
            _selected_controller_models(available, []),
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(_selected_controller_models(available, ["gpt-4o"]), ["gpt-4o"])
        self.assertEqual(
            _selected_controller_models([available[0], available[2]], []),
            ["gpt-5.6-sol"],
        )
        self.assertEqual(
            _selected_controller_models([available[0], available[-1]], []),
            ["gpt-4o"],
        )
        self.assertEqual(_selected_controller_models([], ["gpt-manual"]), ["gpt-manual"])

    def test_controller_renders_independent_variant_status_results_and_failures(self) -> None:
        running = {
            "id": "run-luna",
            "model": "gpt-5.6-luna",
            "run": {"status": "running", "phase": "implementing"},
        }
        completed = {
            "id": "run-terra",
            "model": "gpt-5.6-terra",
            "run": {"status": "completed"},
            "report": {
                "codex_execution": {"elapsed_seconds": 83},
                "historical_model_request_seconds": 125,
                "estimated_cost": {"claude": {"usd": 0.18}, "codex": {"usd": 0.042}},
                "evaluation": {
                    "totals": {"A": 0.25, "B": 0.75},
                    "candidate_mapping": {"A": "claude", "B": "codex"},
                },
            },
        }
        errors = [{"model": "gpt-5.6-sol", "error": "Sol provider unavailable <offline>"}]

        rendered = _render_controller_run_variants([running, completed], errors)

        self.assertIn('aria-label="Parallel replay variants"', rendered)
        for variant in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            with self.subTest(variant=variant):
                self.assertIn(f"<strong>{variant}</strong>", rendered)
        for status in ("Running", "Completed", "Failed"):
            with self.subTest(status=status):
                self.assertIn(f">{status}</span>", rendered)
        self.assertNotIn(" leads", rendered)
        self.assertEqual(rendered.count("<strong>Historical</strong>"), 3)
        self.assertEqual(rendered.count("<strong>Codex</strong>"), 3)
        self.assertIn(
            '<strong class="v-worse">25%</strong><strong class="v-better">75%</strong>',
            rendered,
        )
        self.assertIn(
            '<strong class="v-worse">$0.18</strong><strong class="v-better">$0.04</strong>',
            rendered,
        )
        self.assertIn(
            '<strong class="v-worse">2m 5s</strong><strong class="v-better">1m 23s</strong>',
            rendered,
        )
        self.assertEqual(rendered.count('class="v-better"'), 3)
        self.assertEqual(rendered.count('class="v-worse"'), 3)
        for metric in ("Score", "Cost", "Time"):
            with self.subTest(metric=metric):
                self.assertEqual(rendered.count(f"<span>{metric}</span>"), 3)
        self.assertIn("$0.18", rendered)
        self.assertIn("$0.04", rendered)
        self.assertIn("2m 5s", rendered)
        self.assertIn("1m 23s", rendered)
        self.assertIn("Unavailable", rendered)
        self.assertLess(rendered.index("gpt-5.6-sol"), rendered.index("gpt-5.6-terra"))
        self.assertLess(rendered.index("gpt-5.6-terra"), rendered.index("gpt-5.6-luna"))
        self.assertIn("Sol provider unavailable &lt;offline&gt;", rendered)
        self.assertNotIn("<offline>", rendered)
        self.assertIn('data-action="select-run" data-run-id="run-luna"', rendered)
        self.assertIn('data-action="select-run" data-run-id="run-terra"', rendered)
        self.assertIn('data-action="cancel-run" data-run-id="run-luna"', rendered)
        self.assertNotIn('data-action="cancel-run" data-run-id="run-terra"', rendered)
        self.assertNotIn("View result", rendered)
        self.assertNotIn("View progress", rendered)

        results = _render_controller_run_variants(
            [running, completed], errors, results=True, active_run_id="run-terra"
        )
        self.assertIn(
            'class="v-open" data-action="select-run" data-run-id="run-luna" disabled>', results
        )
        self.assertIn('class="v-open" data-action="select-run" data-run-id="run-terra" >', results)
        self.assertEqual(results.count('aria-current="true"'), 1)
        self.assertIn("vc-a", results)
        self.assertIn("Viewing result", results)
        self.assertIn("$0.04", results)
        self.assertEqual(_render_controller_run_variants([running], []), "")

    def test_controller_reload_restores_only_owned_batch_runs_and_launch_errors(self) -> None:
        session = "controller-a"
        error = {"model": "gpt-5.6-sol", "error": "Sol coordinator could not start."}
        restored = _restore_controller_runs(
            {
                "controller_session_id": session,
                "models": [
                    {"id": "gpt-5.6-luna"},
                    {"id": "gpt-5.6-terra"},
                    {"id": "gpt-5.6-sol"},
                ],
                "recent_runs": [
                    {
                        "run_id": "foreign-run",
                        "model": "gpt-5.6-luna",
                        "controller_session_id": "controller-b",
                        "prepare_token_hash": "shared-approval",
                        "status": "running",
                    },
                    {
                        "run_id": "run-luna",
                        "model": "gpt-5.6-luna",
                        "controller_session_id": session,
                        "prepare_token_hash": "shared-approval",
                        "status": "completed",
                    },
                    {
                        "run_id": "failed-sol",
                        "model": "gpt-5.6-sol",
                        "controller_session_id": session,
                        "prepare_token_hash": "shared-approval",
                        "status": "failed",
                        "launch_failed": True,
                    },
                    {
                        "run_id": "run-terra",
                        "model": "gpt-5.6-terra",
                        "controller_session_id": session,
                        "prepare_token_hash": "shared-approval",
                        "status": "running",
                        "run_group_errors": [error],
                    },
                    {
                        "run_id": "unrelated-run",
                        "model": "gpt-5.6-sol",
                        "controller_session_id": session,
                        "prepare_token_hash": "different-approval",
                        "status": "running",
                    },
                ],
            },
            remembered_run_id="run-terra",
        )

        self.assertEqual(restored["step"], "run")
        self.assertEqual(restored["run_id"], "run-terra")
        self.assertEqual([run["id"] for run in restored["runs"]], ["run-luna", "run-terra"])
        self.assertEqual(restored["models"], ["gpt-5.6-luna", "gpt-5.6-terra"])
        self.assertEqual(restored["errors"], [error])
        self.assertIn(["remember", "run-terra"], restored["events"])
        self.assertIn(["refresh"], restored["events"])

    def test_controller_reload_recovers_batch_older_than_recent_window(self) -> None:
        session = "controller-a"
        recent = [
            {
                "run_id": f"newer-{index}",
                "model": "gpt-5.6-sol",
                "controller_session_id": session,
                "prepare_token_hash": f"newer-{index}",
                "status": "completed",
            }
            for index in range(12)
        ]
        batch = [
            {
                "run_id": f"old-{model}",
                "model": f"gpt-5.6-{model}",
                "controller_session_id": session,
                "prepare_token_hash": "old-approval",
                "status": "completed",
            }
            for model in ("sol", "terra", "luna")
        ]
        state = {"controller_session_id": session, "models": [], "recent_runs": recent}
        recovered = _restore_controller_runs(
            state,
            remembered_run_id="old-terra",
            recovered_state={**state, "recent_runs": [*recent, *batch]},
        )

        self.assertEqual(recovered["run_id"], "old-terra")
        self.assertEqual([run["id"] for run in recovered["runs"]], [run["run_id"] for run in batch])

    def test_controller_retries_reports_after_a_transient_variant_failure(self) -> None:
        refreshed = _refresh_controller_runs(
            [
                {"id": "run-sol", "model": "gpt-5.6-sol", "run": {"status": "completed"}},
                {"id": "run-terra", "model": "gpt-5.6-terra", "run": {"status": "completed"}},
            ]
        )

        self.assertEqual(refreshed["first"], {"step": "results", "scheduled": 1})
        self.assertEqual(refreshed["reports"], [True, True])
        self.assertEqual(refreshed["scheduled"], 1)

    def test_recent_runs_include_all_remembered_batch_siblings_outside_window(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            for index in range(15):
                run_id = f"run-{index:02d}"
                directory = run_root / run_id
                directory.mkdir()
                server._write_json(
                    directory / server.STATE_NAME,
                    {
                        "run_id": run_id,
                        "controller_session_id": server.CONTROLLER_SESSION_ID,
                        "prepare_token_hash": "old-approval" if index < 3 else f"new-{index}",
                    },
                )

            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", return_value={"options": []}),
            ):
                ordinary = server._state_payload()["recent_runs"]
                recovered = server._call_tool(
                    {"name": "get_state", "arguments": {"run_id": "run-01"}}
                )["structuredContent"]["state"]["recent_runs"]

        self.assertEqual(len(ordinary), 12)
        self.assertFalse(any(run["run_id"] == "run-01" for run in ordinary))
        self.assertTrue(
            {"run-00", "run-01", "run-02"}.issubset({run["run_id"] for run in recovered})
        )

    def test_multi_model_approval_launches_isolated_runs_concurrently_and_idempotently(
        self,
    ) -> None:
        server = load_server()
        selected = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        prepared_digests = {
            model: hashlib.sha256(model.encode("utf-8")).hexdigest() for model in selected
        }
        launch_barrier = threading.Barrier(len(selected))

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            prepared_models: list[str] = []
            launched_models: list[str] = []

            def fake_engine(command: str, arguments=(), **kwargs):
                model = arguments[arguments.index("--model") + 1]
                if command == "prepare":
                    prepared_models.append(model)
                    return {
                        "status": "ready_for_approval",
                        "blocking_reasons": [],
                        "approval_prompt": "Approve?",
                        "configuration": {"model": model, "baseline": {"commit": "shared"}},
                        "historical_result_sha256": "b" * 64,
                        "prepared_configuration_sha256": prepared_digests[model],
                    }
                if command != "run":
                    raise AssertionError(command)

                launch_barrier.wait(timeout=5)
                launched_models.append(model)
                self.assertEqual(kwargs.get("input_text"), "Build the reviewed thing.")
                self.assertEqual(
                    arguments[arguments.index("--expected-historical-result-sha256") + 1],
                    "b" * 64,
                )
                self.assertEqual(
                    arguments[arguments.index("--expected-prepared-configuration-sha256") + 1],
                    prepared_digests[model],
                )
                run_directory = run_root / f"run-{model.rsplit('-', 1)[1]}"
                run_directory.mkdir()
                return {
                    "status": "native_task_required",
                    "run_directory": str(run_directory),
                    "task_request": {"model": model},
                }

            config = {
                "thread_id": "thread-1",
                "source_path": "/tmp/transcript.jsonl",
                "message_uuid": "message-1",
                "request": "Build the reviewed thing.",
                "beginning_kind": "git",
                "ending_kind": "git",
                "baseline_commit": "a" * 40,
                "ending_commit": "b" * 40,
                "model": selected[0],
                "models": selected,
                "timeout_seconds": 1200,
            }

            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(
                    server,
                    "_spawn_coordinator",
                    side_effect=lambda directory: mock.Mock(
                        pid=4300 + selected.index(f"gpt-5.6-{directory.name[4:]}")
                    ),
                ) as spawn_coordinator,
            ):
                prepared = server._prepare_payload(config)
                self.assertEqual(prepared["models"], selected)
                self.assertEqual(prepared["run_config"]["model"], selected[0])
                self.assertEqual(prepared["run_config"]["models"], selected)
                self.assertEqual(prepared_models, selected)

                approved = {
                    **config,
                    "approved": True,
                    "prepare_token": prepared["prepare_token"],
                }
                first = server._start_run(approved)
                second = server._start_run(approved)

                with self.assertRaisesRegex(server.ControllerError, "configuration changed"):
                    server._start_run({**approved, "models": selected[:2]})
                with self.assertRaisesRegex(server.ControllerError, "configuration changed"):
                    server._start_run(
                        {
                            **approved,
                            "model": selected[1],
                            "models": [selected[1], selected[0], selected[2]],
                        }
                    )
                server._prepared_runs.clear()
                recovered = server._start_run(approved)

                self.assertCountEqual(launched_models, selected)
                self.assertFalse(first["idempotent"])
                self.assertTrue(second["idempotent"])
                self.assertTrue(recovered["idempotent"])
                self.assertEqual(first["models"], selected)
                self.assertEqual([run["model"] for run in first["runs"]], selected)
                self.assertEqual(first["run_id"], first["runs"][0]["run_id"])
                self.assertEqual(first["run"], first["runs"][0]["run"])
                self.assertEqual(
                    [run["run_id"] for run in second["runs"]],
                    [run["run_id"] for run in first["runs"]],
                )
                self.assertEqual(
                    [run["run_id"] for run in recovered["runs"]],
                    [run["run_id"] for run in first["runs"]],
                )
                self.assertEqual(len({run["run_id"] for run in first["runs"]}), len(selected))
                spawn_coordinator.assert_has_calls(
                    [mock.call(run_root / f"run-{model.rsplit('-', 1)[1]}") for model in selected],
                    any_order=True,
                )
                self.assertEqual(spawn_coordinator.call_count, len(selected))

                for run in first["runs"]:
                    request = run_root / run["run_id"] / server.COORDINATOR_REQUEST_NAME
                    self.assertEqual(
                        json.loads(request.read_text(encoding="utf-8")), {"model": run["model"]}
                    )
                    self.assertEqual(request.stat().st_mode & 0o777, 0o600)

    def test_multi_model_recovery_rejects_an_incomplete_unrecorded_launch(self) -> None:
        server = load_server()
        selected = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

        def prepare(command: str, arguments=(), **kwargs):
            self.assertEqual(command, "prepare")
            model = arguments[arguments.index("--model") + 1]
            return {
                "status": "ready_for_approval",
                "blocking_reasons": [],
                "approval_prompt": "Approve?",
                "configuration": {"model": model, "baseline": {"commit": "shared"}},
                "historical_result_sha256": "a" * 64,
                "prepared_configuration_sha256": hashlib.sha256(model.encode()).hexdigest(),
            }

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            configuration = {"thread_id": "thread-1", "model": selected[0], "models": selected}
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=prepare) as engine,
            ):
                prepared = server._prepare_payload(configuration)
                approved = {
                    **configuration,
                    "approved": True,
                    "prepare_token": prepared["prepare_token"],
                }
                run_directory = run_root / "run-sol"
                run_directory.mkdir()
                state = server._initial_state(
                    run_directory,
                    prepare_token=prepared["prepare_token"],
                    configuration_fingerprint=server._configuration_fingerprint(
                        server._normalized_configuration(approved)
                    ),
                )
                state["model"] = selected[0]
                state["models"] = selected
                server._write_json(server._state_path(run_directory), state)
                server._prepared_runs.clear()

                with self.assertRaisesRegex(server.ControllerError, "every selected model"):
                    server._start_run(approved)

                self.assertEqual(engine.call_count, len(selected))

    def test_multi_model_preparation_rejects_inconsistent_historical_results(self) -> None:
        server = load_server()
        selected = ["gpt-5.6-luna", "gpt-5.6-terra"]

        def fake_engine(command: str, arguments=(), **kwargs):
            self.assertEqual(command, "prepare")
            model = arguments[arguments.index("--model") + 1]
            return {
                "status": "ready_for_approval",
                "blocking_reasons": [],
                "approval_prompt": "Approve?",
                "configuration": {"model": model, "baseline": {"commit": "shared"}},
                "historical_result_sha256": ("a" if model == selected[0] else "b") * 64,
                "prepared_configuration_sha256": hashlib.sha256(model.encode("utf-8")).hexdigest(),
            }

        with (
            mock.patch.object(server, "_engine", side_effect=fake_engine),
            self.assertRaisesRegex(server.ControllerError, "historical Claude result changed"),
        ):
            server._prepare_payload(
                {"thread_id": "thread-1", "model": selected[0], "models": selected}
            )

    def test_multi_model_preparation_rejects_different_prepared_baselines(self) -> None:
        server = load_server()
        selected = ["gpt-5.6-sol", "gpt-5.6-terra"]

        def prepare(command: str, arguments=(), **kwargs):
            self.assertEqual(command, "prepare")
            model = arguments[arguments.index("--model") + 1]
            return {
                "status": "ready_for_approval",
                "configuration": {
                    "model": model,
                    "baseline": {"commit": "first" if model == selected[0] else "second"},
                },
                "historical_result_sha256": "a" * 64,
                "prepared_configuration_sha256": hashlib.sha256(model.encode()).hexdigest(),
            }

        with (
            mock.patch.object(server, "_engine", side_effect=prepare),
            self.assertRaisesRegex(
                server.ControllerError, "baseline changed between selected models"
            ),
        ):
            server._prepare_payload(
                {"thread_id": "thread-1", "model": selected[0], "models": selected}
            )

    def test_multi_model_preparation_cannot_approve_while_any_variant_is_blocked(self) -> None:
        server = load_server()
        selected = ["gpt-5.6-luna", "gpt-5.6-terra"]
        question = {"id": "terra-confirmation", "question": "Confirm Terra's source?"}

        def fake_engine(command: str, arguments=(), **kwargs):
            self.assertEqual(command, "prepare")
            model = arguments[arguments.index("--model") + 1]
            if model == selected[0]:
                return {
                    "status": "ready_for_approval",
                    "can_run": True,
                    "questions": [],
                    "blocking_reasons": [],
                    "approval_prompt": "Approve?",
                    "historical_result_sha256": "a" * 64,
                    "prepared_configuration_sha256": "b" * 64,
                }
            return {
                "status": "needs_user_input",
                "can_run": False,
                "questions": [question],
                "blocking_reasons": ["Terra still needs confirmation."],
            }

        with mock.patch.object(server, "_engine", side_effect=fake_engine):
            prepared = server._prepare_payload(
                {"thread_id": "thread-1", "model": selected[0], "models": selected}
            )

        self.assertFalse(prepared["ready"])
        self.assertFalse(prepared["can_run"])
        self.assertEqual(prepared["status"], "needs_user_input")
        self.assertEqual(prepared["questions"], [question])
        self.assertEqual(prepared["blockers"], ["Terra still needs confirmation."])
        self.assertIsNone(prepared["prepare_token"])
        self.assertIsNone(prepared["approval"]["prepare_token"])

    def test_multi_model_launch_keeps_successful_sibling_when_one_variant_fails(self) -> None:
        server = load_server()
        selected = ["gpt-5.6-luna", "gpt-5.6-terra"]
        launch_barrier = threading.Barrier(len(selected))

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()

            def fake_engine(command: str, arguments=(), **kwargs):
                model = arguments[arguments.index("--model") + 1]
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "blocking_reasons": [],
                        "approval_prompt": "Approve?",
                        "configuration": {"model": model, "baseline": {"commit": "shared"}},
                        "historical_result_sha256": "a" * 64,
                        "prepared_configuration_sha256": hashlib.sha256(
                            model.encode("utf-8")
                        ).hexdigest(),
                    }
                if command != "run":
                    raise AssertionError(command)

                launch_barrier.wait(timeout=5)
                if model == selected[0]:
                    raise server.ControllerError("Luna is temporarily unavailable.")
                run_directory = run_root / "run-terra"
                run_directory.mkdir()
                return {
                    "status": "native_task_required",
                    "run_directory": str(run_directory),
                    "task_request": {"model": model},
                }

            configuration = {
                "thread_id": "thread-1",
                "model": selected[0],
                "models": selected,
            }
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(
                    server,
                    "_spawn_coordinator",
                    return_value=mock.Mock(pid=4321),
                ) as spawn_coordinator,
            ):
                prepared = server._prepare_payload(configuration)
                approved = {
                    **configuration,
                    "approved": True,
                    "prepare_token": prepared["prepare_token"],
                }
                result = server._start_run(approved)
                repeated = server._start_run(approved)
                server._prepared_runs.clear()
                recovered = server._start_run(approved)

                self.assertEqual([run["model"] for run in result["runs"]], [selected[1]])
                self.assertEqual(result["run_id"], "run-terra")
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["errors"][0]["model"], selected[0])
                self.assertIn("Luna is temporarily unavailable", result["errors"][0]["error"])
                self.assertEqual(result["runs"][0]["run"]["status"], "running")
                for retried in (repeated, recovered):
                    with self.subTest(retry=retried):
                        self.assertTrue(retried["idempotent"])
                        self.assertEqual(retried["run_id"], result["run_id"])
                        self.assertEqual(retried["errors"], result["errors"])
                spawn_coordinator.assert_called_once_with(run_root / "run-terra")

    def test_multi_model_recovery_never_restarts_a_failed_coordinator_launch(self) -> None:
        server = load_server()
        selected = ["gpt-5.6-luna", "gpt-5.6-terra"]
        launch_barrier = threading.Barrier(len(selected))

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()

            def fake_engine(command: str, arguments=(), **kwargs):
                model = arguments[arguments.index("--model") + 1]
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "blocking_reasons": [],
                        "approval_prompt": "Approve?",
                        "configuration": {"model": model, "baseline": {"commit": "shared"}},
                        "historical_result_sha256": "a" * 64,
                        "prepared_configuration_sha256": hashlib.sha256(
                            model.encode("utf-8")
                        ).hexdigest(),
                    }
                if command != "run":
                    raise AssertionError(command)
                launch_barrier.wait(timeout=5)
                run_directory = run_root / f"run-{model.rsplit('-', 1)[1]}"
                run_directory.mkdir()
                return {
                    "status": "native_task_required",
                    "run_directory": str(run_directory),
                    "task_request": {"model": model},
                }

            def spawn(directory: Path):
                if directory.name == "run-luna":
                    raise OSError("Luna coordinator is unavailable.")
                return mock.Mock(pid=4321)

            configuration = {
                "thread_id": "thread-1",
                "model": selected[0],
                "models": selected,
            }
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(server, "_spawn_coordinator", side_effect=spawn) as coordinator,
            ):
                prepared = server._prepare_payload(configuration)
                approved = {
                    **configuration,
                    "approved": True,
                    "prepare_token": prepared["prepare_token"],
                }
                started = server._start_run(approved)
                failed_state = json.loads(
                    (run_root / "run-luna" / server.STATE_NAME).read_text(encoding="utf-8")
                )
                server._prepared_runs.clear()
                recovered = server._start_run(approved)

                self.assertEqual(failed_state["status"], "failed")
                self.assertEqual(failed_state["model"], selected[0])
                self.assertEqual([run["model"] for run in started["runs"]], [selected[1]])
                self.assertEqual([run["model"] for run in recovered["runs"]], [selected[1]])
                self.assertEqual(recovered["run_id"], started["run_id"])
                self.assertEqual(recovered["errors"], started["errors"])
                self.assertEqual(recovered["errors"][0]["model"], selected[0])
                self.assertIn("coordinator", recovered["errors"][0]["error"])
                self.assertTrue(recovered["idempotent"])
                self.assertEqual(coordinator.call_count, len(selected))


if __name__ == "__main__":
    unittest.main()
