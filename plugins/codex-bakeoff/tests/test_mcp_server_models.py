"""Parallel Codex model selection, execution, and controller presentation tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
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
require(require("node:path").join(require("node:path").dirname(process.argv[1]), "controller-ranges.js"));
const extract = (start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  if (first < 0 || last <= first) throw new Error(`Missing controller code: ${start}`);
  return source.slice(first, last);
};
const controllerCode = extract("      const STEPS =", '      app.addEventListener("click"');
const createController = (setup, values = {}, browser = {}) => new Function(
  "document", "values", "window", [controllerCode, setup].join("\n")
)({getElementById: () => null}, values, browser);
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
const render = createController(`
  Object.assign(state, values);
  return renderRunVariants;
`, JSON.parse(process.argv[2]));
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
    remembered_run_id: str = "",
    recovered_state: dict[str, object] | None = None,
    saved_draft: dict[str, object] | None = None,
) -> dict[str, object]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const events = [];
const context = createController(`
  const {serverState, rememberedRunId, recoveredState, savedDraft, events} = values;
  loadDraft = () => savedDraft ? normalizeDraft({version: DRAFT_VERSION, ...savedDraft}) : null;
  loadActiveRunId = () => rememberedRunId;
  render = () => {};
  startControllerHeartbeat = () => {};
  clearDraft = () => events.push(["clear-draft"]);
  rememberActiveRun = id => events.push(["remember", id]);
  beginPolling = () => events.push(["poll"]);
  callTool = async (name, args) => name === "get_state"
    ? {state: args.run_id ? recoveredState : serverState} : {threads: [], total: 0};
  return {initialize, state, events};
`, {
  serverState: JSON.parse(process.argv[2]), rememberedRunId: process.argv[3],
  recoveredState: JSON.parse(process.argv[4]), savedDraft: JSON.parse(process.argv[5]), events,
}, {setTimeout: () => events.push(["refresh"]), clearTimeout: () => {}});
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
            json.dumps(saved_draft),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def _controller_navigation(state: dict[str, object]) -> dict[str, bool]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const canNavigateTo = createController(`
  Object.assign(state, values);
  return canNavigateTo;
`, JSON.parse(process.argv[2]));
process.stdout.write(JSON.stringify(Object.fromEntries(
  ["thread", "configure", "run", "results"].map(id => [id, canNavigateTo(id)])
)));
"""
    )
    result = subprocess.run(
        ["node", "-e", harness, str(CONTROLLER_PATH), json.dumps(state)],
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
const context = createController(`
  Object.assign(state, {runs: values, runId: values[0].id, step: "run"});
  let attempts = 0, scheduled = 0;
  callTool = async (name, {run_id}) => {
    if (name !== "get_report") throw new Error("Unexpected tool");
    if (run_id === "run-terra" && attempts++ === 0) throw new Error("Temporary report failure");
    return {report: {model: run_id}};
  };
  render = () => {};
  schedulePoll = () => { scheduled += 1; };
  return {refreshRun, state, scheduled: () => scheduled};
`, JSON.parse(process.argv[2]));
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
    def test_accepted_large_request_does_not_block_model_launch(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "runs"
            directory = root / "run-large"

            def engine(command, *args, **kwargs):
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "historical_result_sha256": "a" * 64,
                        "prepared_configuration_sha256": "b" * 64,
                    }
                directory.mkdir(parents=True)
                return {"run_directory": str(directory), "task_request": {}}

            configuration = {
                "thread_id": "thread-1",
                "model": "gpt-5.6-sol",
                "request": "x" * (600 * 1024),
            }
            self.assertLess(len(json.dumps(configuration).encode()), server.MAX_HTTP_BODY_BYTES)
            with (
                mock.patch.object(server, "RUN_ROOT", root),
                mock.patch.object(server, "_engine", side_effect=engine),
                mock.patch.object(server, "_spawn_coordinator") as spawn,
            ):
                prepared = server._prepare_payload(configuration)
                started = server._start_run(
                    {**configuration, "approved": True, "prepare_token": prepared["prepare_token"]}
                )
                self.assertEqual(started["run_id"], directory.name)
                spawn.assert_called_once_with(directory)

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

    def test_parallel_variants_do_not_reuse_historical_checks_across_ranges(self) -> None:
        server = load_server()
        models = ["gpt-5.6-terra", "gpt-5.6-luna"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            range_paths: list[Path] = []
            for index, message_uuid in enumerate(("user-1", "user-2")):
                configuration = {
                    "thread_id": "thread-1",
                    "models": models,
                    "start_message_uuid": message_uuid,
                    "end_message_uuid": message_uuid,
                }
                checks = {"request_fulfillment": {"required_behavior": index}}
                paths: list[Path] = []
                for model in models:
                    run_directory = root / f"range-{index}-{model}"
                    run_directory.mkdir()
                    server._write_json(
                        run_directory / server.STATE_NAME,
                        {
                            "models": models,
                            "prepare_token_hash": "a" * 64,
                            "configuration_fingerprint": server._configuration_fingerprint(
                                configuration
                            ),
                        },
                    )
                    with server._historical_review_guard(run_directory) as path:
                        self.assertIsNotNone(path)
                        if path is None:
                            raise AssertionError("Parallel variants require a shared review path.")
                        if not paths:
                            self.assertFalse(path.exists())
                            server._write_json(path, {"checks": checks})
                        else:
                            self.assertEqual(server._read_json(path)["checks"], checks)
                        paths.append(path)
                self.assertEqual(paths[0], paths[1])
                range_paths.append(paths[0])

            self.assertNotEqual(range_paths[0], range_paths[1])
            self.assertEqual(
                server._read_json(range_paths[0])["checks"],
                {"request_fulfillment": {"required_behavior": 0}},
            )

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
        self.assertNotIn('<select id="review-model">', configure)
        self.assertIn('id="review-model" type="text"', configure)
        self.assertIn('"review-model": "model"', controller)
        self.assertIn('if (reviewKey === "model") setSelectedModels([target.value])', controller)
        self.assertIn("Promise.allSettled", controller)
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

    def test_controller_reload_restores_terminal_runs_without_remembered_run(self) -> None:
        for status, launch_failed in (
            ("completed", False),
            ("failed", False),
            ("cancelled", False),
            ("failed", True),
        ):
            for saved_draft in (None, {"selectedThreadId": "stale-thread"}):
                with self.subTest(
                    status=status, launch_failed=launch_failed, saved_draft=saved_draft
                ):
                    run = {
                        "run_id": "run-terra",
                        "model": "gpt-5.6-terra",
                        "controller_session_id": "controller-a",
                        "prepare_token_hash": "shared-approval",
                        "status": status,
                        "launch_failed": launch_failed,
                    }
                    restored = _restore_controller_runs(
                        {
                            "controller_session_id": "controller-a",
                            "models": [],
                            "recent_runs": [run],
                        },
                        saved_draft=saved_draft,
                    )

                    self.assertEqual(restored["step"], "run")
                    self.assertEqual(restored["run_id"], "run-terra")
                    self.assertEqual(restored["runs"][0]["run"], run)
                    self.assertIn(["clear-draft"], restored["events"])
                    self.assertEqual(["refresh"] in restored["events"], status == "completed")

    def test_controller_navigation_keeps_started_replay_on_run_and_results(self) -> None:
        state = {"step": "configure", "busy": "", "inspection": {}, "run": None, "report": None}
        self.assertEqual(
            _controller_navigation(state),
            {"thread": True, "configure": False, "run": False, "results": False},
        )
        for status in ("running", "completed", "failed", "cancelled"):
            with self.subTest(status=status):
                current = {
                    **state,
                    "step": "run",
                    "run": {"status": status},
                    "report": {} if status == "completed" else None,
                }
                self.assertEqual(
                    _controller_navigation(current),
                    {
                        "thread": False,
                        "configure": False,
                        "run": False,
                        "results": status == "completed",
                    },
                )
                if status == "completed":
                    self.assertEqual(
                        _controller_navigation({**current, "step": "results"}),
                        {"thread": False, "configure": False, "run": True, "results": False},
                    )

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

    def test_prepare_token_binds_config_and_makes_start_idempotent(self) -> None:
        server = load_server()

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"
            run_calls = 0
            coordinator = mock.Mock(pid=4321)

            def fake_engine(command: str, arguments=(), **kwargs):
                nonlocal run_calls
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "blocking_reasons": [],
                        "approval_prompt": "Approve?",
                        "historical_result_sha256": "b" * 64,
                        "prepared_configuration_sha256": "d" * 64,
                    }
                if command == "run":
                    run_calls += 1
                    self.assertEqual(
                        kwargs.get("input_text"),
                        "Build the reviewed thing.",
                    )
                    self.assertIn(
                        [
                            "--expected-historical-result-sha256",
                            "b" * 64,
                        ],
                        [list(arguments[index : index + 2]) for index in range(len(arguments) - 1)],
                    )
                    self.assertIn(
                        [
                            "--expected-prepared-configuration-sha256",
                            "d" * 64,
                        ],
                        [list(arguments[index : index + 2]) for index in range(len(arguments) - 1)],
                    )
                    run_directory.mkdir()
                    return {
                        "status": "native_task_required",
                        "run_directory": str(run_directory),
                        "task_request": {},
                    }
                raise AssertionError(command)

            config = {
                "thread_id": "thread-1",
                "source_path": "/tmp/transcript.jsonl",
                "message_uuid": "message-1",
                "request": "Build the reviewed thing.",
                "beginning_kind": "git",
                "ending_kind": "git",
                "baseline_commit": "a" * 40,
                "ending_commit": "b" * 40,
                "model": "gpt-5.6-terra",
                "timeout_seconds": 1200,
            }
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(
                    server,
                    "_spawn_coordinator",
                    return_value=coordinator,
                ) as spawn_coordinator,
            ):
                prepared = server._prepare_payload(config)
                approved = {
                    **config,
                    "approved": True,
                    "prepare_token": prepared["prepare_token"],
                }
                other_config = {**config, "model": "gpt-5.6-sol"}
                other_prepared = server._prepare_payload(other_config)
                other_approved = {
                    **other_config,
                    "approved": True,
                    "prepare_token": other_prepared["prepare_token"],
                }
                first = server._start_run(approved)
                second = server._start_run(approved)
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "model": "gpt-5.6-sol"})
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "request": "Build something else."})
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "message_uuid": "different-message"})
                with self.assertRaisesRegex(
                    server.ControllerError,
                    "configuration changed",
                ):
                    server._start_run({**approved, "ending_commit": "c" * 40})

                state = server._read_json(server._state_path(run_directory))
                for status in ("running", "completed", "failed", "cancelled"):
                    with self.subTest(status=status):
                        server._write_json(
                            server._state_path(run_directory), {**state, "status": status}
                        )
                        with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                            server._start_run(other_approved)
                        with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                            server._prepare_payload(config)
                        repeated = server._start_run(approved)
                        self.assertTrue(repeated["idempotent"])
                        self.assertEqual(repeated["run_id"], first["run_id"])

                server._prepared_runs.clear()
                recovered = server._start_run(approved)
                self.assertTrue(recovered["idempotent"])
                self.assertEqual(recovered["run_id"], first["run_id"])
                with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                    server._prepare_payload(config)

            self.assertEqual(run_calls, 1)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(first["run"]["coordinator_pid"], os.getpid())
            self.assertEqual(second["run"]["coordinator_pid"], os.getpid())
            spawn_coordinator.assert_called_once_with(run_directory)
            request = run_directory / server.COORDINATOR_REQUEST_NAME
            self.assertTrue(request.is_file())
            self.assertEqual(request.stat().st_mode & 0o777, 0o600)
            self.assertIn("[controller] run approved", first["run"]["run_log"])

    def test_concurrent_approved_replays_only_start_one_group(self) -> None:
        server = load_server()
        starting = threading.Event()
        release = threading.Event()
        config = {"thread_id": "thread-1", "model": "gpt-5.6-terra"}
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            run_directory = run_root / "run-1"

            def fake_engine(
                command: str, _arguments: Sequence[str] = (), **_kwargs: str | None
            ) -> dict[str, str | dict[str, str]]:
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "historical_result_sha256": "b" * 64,
                        "prepared_configuration_sha256": "d" * 64,
                    }
                self.assertEqual(command, "run")
                starting.set()
                if not release.wait(timeout=5):
                    raise AssertionError("The first launch was never released.")
                run_directory.mkdir()
                return {"run_directory": str(run_directory), "task_request": {}}

            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "_engine", side_effect=fake_engine),
                mock.patch.object(
                    server, "_spawn_coordinator", return_value=mock.Mock(pid=4321)
                ) as coordinator,
            ):
                approved = [
                    {
                        **config,
                        "approved": True,
                        "prepare_token": server._prepare_payload(config)["prepare_token"],
                    }
                    for _ in range(2)
                ]
                with ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(server._start_run, approved[0])
                    try:
                        self.assertTrue(starting.wait(timeout=5), "The first launch never began.")
                        self.assertEqual(server._recent_runs(), [])
                        with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                            server._start_run(approved[1])
                        with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                            server._prepare_payload(config)
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=5)["run_id"], "run-1")
                coordinator.assert_called_once_with(run_directory)

    def test_launch_failure_is_durable_and_cannot_restart(self) -> None:
        for failure_stage in ("engine", "coordinator"):
            with (
                self.subTest(failure_stage=failure_stage),
                tempfile.TemporaryDirectory() as temporary,
            ):
                server = load_server()
                run_root = Path(temporary).resolve()
                run_directory = run_root / "run-1"
                config = {"thread_id": "thread-1", "model": "gpt-5.6-terra"}
                run_calls = 0

                def fake_engine(
                    command: str,
                    _arguments: Sequence[str] = (),
                    *,
                    failure_stage: str = failure_stage,
                    server: ModuleType = server,
                    run_directory: Path = run_directory,
                    **_kwargs: str | None,
                ) -> dict[str, str | dict[str, str]]:
                    nonlocal run_calls
                    if command == "prepare":
                        return {
                            "status": "ready_for_approval",
                            "historical_result_sha256": "b" * 64,
                            "prepared_configuration_sha256": "d" * 64,
                        }
                    self.assertEqual(command, "run")
                    run_calls += 1
                    if failure_stage == "engine" and run_calls == 1:
                        raise server.ControllerError("The engine could not start.")
                    run_directory.mkdir()
                    return {"run_directory": str(run_directory), "task_request": {}}

                with (
                    mock.patch.object(server, "RUN_ROOT", run_root),
                    mock.patch.object(server, "_engine", side_effect=fake_engine),
                    mock.patch.object(
                        server,
                        "_spawn_coordinator",
                        return_value=mock.Mock(pid=4321),
                        side_effect=OSError("unavailable")
                        if failure_stage == "coordinator"
                        else None,
                    ) as coordinator,
                ):
                    approved = {
                        **config,
                        "approved": True,
                        "prepare_token": server._prepare_payload(config)["prepare_token"],
                    }
                    with self.assertRaisesRegex(server.ControllerError, "could not start"):
                        server._start_run(approved)
                    attempt = server._read_json(server._attempt_path())
                    self.assertTrue(attempt["start_requested"])
                    self.assertEqual(attempt["models"][0]["launch_status"], "failed")
                    self.assertEqual(attempt["models"][0]["controller_code"], "launch_failed")
                    if failure_stage == "coordinator":
                        state = server._read_json(server._state_path(run_directory))
                        self.assertEqual(
                            state["failure_diagnostic"]["controller_code"], "launch_failed"
                        )
                    for clear_receipt in (False, True):
                        if clear_receipt:
                            server._prepared_runs.clear()
                        with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                            server._start_run(approved)
                        with self.assertRaisesRegex(server.ControllerError, "new Codex task"):
                            server._prepare_payload(config)
                    self.assertEqual(run_calls, 1)
                    self.assertEqual(coordinator.call_count, failure_stage == "coordinator")

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


class McpServerBatchTests(unittest.TestCase):
    def test_supervisor_bounds_workers_for_800_comparisons(self) -> None:
        server = load_server()
        count = server.MAX_REPLAY_THREADS * server.MAX_REPLAY_MODELS
        release = threading.Event()
        condition = threading.Condition()
        entered: list[str] = []
        workers: list[threading.Thread] = []
        thread_type = threading.Thread

        def create_worker(*args, **kwargs):
            worker = thread_type(*args, **kwargs)
            workers.append(worker)
            return worker

        def workflow(directory, request):
            with condition:
                entered.append(directory.name)
                condition.notify_all()
            release.wait(timeout=10)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                mock.patch.object(server, "RUN_ROOT", root),
                mock.patch.object(server, "_coordinator", side_effect=workflow),
                mock.patch.object(server.threading, "Thread", side_effect=create_worker),
            ):
                try:
                    for index in range(count):
                        directory = root / f"run-{index}"
                        directory.mkdir()
                        server._write_json(directory / server.COORDINATOR_REQUEST_NAME, {})
                        server._spawn_coordinator(directory)
                    with condition:
                        self.assertTrue(
                            condition.wait_for(
                                lambda: len(entered) == server.MAX_PARALLEL_RUNS, timeout=5
                            )
                        )
                    self.assertEqual(len(workers), server.MAX_PARALLEL_RUNS)
                    self.assertTrue(all(worker.daemon for worker in workers))
                    self.assertEqual(server._active_controller_runs(), count)
                    cancelled_directory = root / f"run-{count - 1}"
                    server._write_json(
                        server._state_path(cancelled_directory),
                        server._initial_state(cancelled_directory),
                    )
                    server._cancel_run({"run_id": cancelled_directory.name})
                    self.assertEqual(server._active_controller_runs(), count - 1)
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(timeout=10)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(len(workers), server.MAX_PARALLEL_RUNS)
                self.assertEqual(len(entered), count - 1)
                self.assertEqual(len(set(entered)), count - 1)
                self.assertNotIn(cancelled_directory.name, entered)
                self.assertEqual(server._active_controller_runs(), 0)
                self.assertFalse(server._run_cancellations)

    def test_supervisor_recovers_from_worker_start_failure_and_idle_retirement(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "run-1"
            directory.mkdir()
            server._write_json(directory / server.COORDINATOR_REQUEST_NAME, {})
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("limit")):
                with self.assertRaisesRegex(RuntimeError, "limit"):
                    server._spawn_coordinator(directory)
            self.assertEqual(server._coordinators.active_count(), 0)
            self.assertFalse(server._run_cancellations)
            with mock.patch.object(server, "_coordinator") as workflow:
                for _ in range(2):
                    with server._active_processes_lock:
                        server._spawn_coordinator(directory)
                        workers = list(server._coordinators._workers)
                    for worker in workers:
                        worker.join(timeout=5)
                        self.assertFalse(worker.is_alive())
                    self.assertEqual(server._coordinators.active_count(), 0)
                self.assertEqual(workflow.call_count, 2)

    def test_all_failed_batch_and_interrupted_launch_recover_without_new_workers(self) -> None:
        server = load_server()
        configurations = [
            {"thread_id": thread, "model": "model", "request": thread}
            for thread in ("first", "second")
        ]

        def engine(command, *args, **kwargs):
            if command == "models":
                return {"options": []}
            if command == "prepare":
                return {
                    "status": "ready_for_approval",
                    "historical_result_sha256": "a" * 64,
                    "prepared_configuration_sha256": "b" * 64,
                }
            raise server.ControllerError("Launch unavailable")

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(server, "RUN_ROOT", Path(temporary) / "runs"),
            mock.patch.object(server, "_engine", side_effect=engine) as calls,
        ):
            prepared = server._prepare_payload({"configurations": configurations})
            approval = {
                **prepared["run_config"],
                "prepare_token": prepared["prepare_token"],
                "approved": True,
            }
            started = server._start_run(approval)
            self.assertEqual(started["runs"], [])
            self.assertEqual(len(started["errors"]), 2)
            server._prepared_runs.clear()
            recovered = server._start_run(approval)
            self.assertTrue(recovered["idempotent"])
            self.assertEqual(recovered["errors"], started["errors"])
            self.assertEqual(calls.call_count, 4)
            payload = server._state_payload()
            self.assertEqual(payload["recent_runs"], [])
            self.assertEqual(len(payload["batch"]["errors"]), 2)
            # Simulate a process dying after the durable intent but before any launch.
            batch = server._read_json(server._batch_path())
            batch.update(starting=True, errors=[], controller_pid=999999999)
            server._write_json(server._batch_path(), batch)
            with mock.patch.object(server, "_pid_is_alive", return_value=False):
                restored = server._state_payload()["batch"]
            self.assertFalse(restored["starting"])
            self.assertEqual(len(restored["errors"]), 2)
            self.assertTrue(all("stopped" in item["error"] for item in restored["errors"]))
            self.assertEqual(calls.call_count, 6)  # Only model discovery ran during reload.

    def test_batch_validates_membership_shared_models_and_exact_approval(self) -> None:
        server = load_server()
        configurations = [
            {"thread_id": thread, "thread_title": thread, "model": "model", "request": thread}
            for thread in ("first", "second")
        ]
        for invalid in (
            [],
            [configurations[0], configurations[0]],
            [configurations[0], {**configurations[1], "model": "different"}],
            [{**configurations[0], "start_message_uuid": "a", "end_message_uuid": "b"}],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(server.ControllerError):
                server._prepare_payload({"configurations": invalid})
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(server, "RUN_ROOT", Path(temporary) / "runs"),
            mock.patch.object(
                server,
                "_engine",
                return_value={
                    "status": "ready_for_approval",
                    "historical_result_sha256": "a" * 64,
                    "prepared_configuration_sha256": "b" * 64,
                },
            ),
        ):
            prepared = server._prepare_payload({"configurations": configurations})
            approval = {**prepared["run_config"], "prepare_token": prepared["prepare_token"]}
            with self.assertRaisesRegex(server.ControllerError, "approval"):
                server._start_run(approval)
            approval["approved"] = True
            approval["configurations"][1]["request"] = "changed"
            with self.assertRaisesRegex(server.ControllerError, "configuration changed"):
                server._start_run(approval)
            self.assertFalse(server._batch_path().exists())

    def test_batch_preserves_per_thread_digests_failures_metrics_and_recovery(self) -> None:
        server = load_server()
        models = ["model-a", "model-b"]
        configurations = [
            {
                "thread_id": thread,
                "thread_title": thread.title(),
                "model": models[0],
                "models": models,
                "request": thread,
            }
            for thread in ("first", "second")
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "runs"
            launched: list[tuple[str, str]] = []

            def engine(command, arguments=(), **kwargs):
                thread = arguments[arguments.index("--imported-thread-id") + 1]
                model = arguments[arguments.index("--model") + 1]
                digest = hashlib.sha256(thread.encode()).hexdigest()
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "historical_result_sha256": digest,
                        "prepared_configuration_sha256": "b" * 64,
                        "configuration": {"thread_id": thread, "model": model},
                    }
                self.assertEqual(
                    arguments[arguments.index("--expected-historical-result-sha256") + 1], digest
                )
                launched.append((thread, model))
                if (thread, model) == ("second", models[0]):
                    raise server.ControllerError("Launch unavailable")
                directory = root / f"run-{thread}-{model}"
                directory.mkdir(parents=True)
                return {"run_directory": str(directory), "task_request": {}}

            with (
                mock.patch.object(server, "RUN_ROOT", root),
                mock.patch.object(server, "_engine", side_effect=engine),
                mock.patch.object(server, "_spawn_coordinator"),
            ):
                prepared = server._prepare_payload({"configurations": configurations})
                approval = {
                    **prepared["run_config"],
                    "prepare_token": prepared["prepare_token"],
                    "approved": True,
                }
                started = server._start_run(approval)
                self.assertEqual(len(started["runs"]), 3)
                self.assertEqual(started["errors"][0]["thread_id"], "second")
                self.assertEqual(started["errors"][0]["model"], models[0])
                self.assertEqual(started["batch"]["thread_count"], 2)
                self.assertEqual(
                    [item["run"]["thread_id"] for item in started["runs"]],
                    ["first", "first", "second"],
                )
                attempt = server._read_json(server._attempt_path())
                self.assertEqual(attempt["start_request"]["thread_id"], "first")
                self.assertTrue(all("first" in entry["run_id"] for entry in attempt["models"]))
                for entry in attempt["models"]:
                    directory = root / entry["run_id"]
                    server._write_json(directory / "report.json", {"ready": True})
                    server._update_state(directory, status="completed")
                self.assertTrue(server._read_json(server._attempt_path())["final_results_ready"])
                server._prepared_runs.clear()
                recovered = server._start_run(approval)
                self.assertTrue(recovered["idempotent"])
                self.assertEqual(recovered["errors"], started["errors"])
                self.assertEqual(len(launched), 4)

    def test_batch_queues_at_capacity_cancels_waiting_and_exposes_partial_reports(self) -> None:
        server = load_server()
        count = server.MAX_PARALLEL_RUNS + 2
        condition = threading.Condition()
        entered: set[str] = set()
        releases = {f"thread-{index}": threading.Event() for index in range(count)}
        finished = {thread: threading.Event() for thread in releases}
        configurations = [
            {"thread_id": thread, "model": "model", "request": thread} for thread in releases
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "runs"

            def engine(command, arguments=(), **kwargs):
                if command == "models":
                    return {"options": []}
                if command == "prepare":
                    return {
                        "status": "ready_for_approval",
                        "historical_result_sha256": "a" * 64,
                        "prepared_configuration_sha256": "b" * 64,
                    }
                thread = arguments[arguments.index("--imported-thread-id") + 1]
                directory = root / thread
                directory.mkdir(parents=True)
                return {"run_directory": str(directory), "task_request": {"thread": thread}}

            def coordinator(directory, request):
                thread = request["thread"]
                server._update_state(directory, phase="implementing")
                with condition:
                    entered.add(thread)
                    condition.notify_all()
                releases[thread].wait(timeout=10)
                server._write_json(directory / "report.json", {"thread": thread})
                server._update_state(directory, status="completed")
                finished[thread].set()

            with (
                mock.patch.object(server, "RUN_ROOT", root),
                mock.patch.object(server, "_engine", side_effect=engine),
                mock.patch.object(server, "_coordinator", side_effect=coordinator),
            ):
                try:
                    prepared = server._prepare_payload({"configurations": configurations})
                    started = server._start_run(
                        {
                            **prepared["run_config"],
                            "prepare_token": prepared["prepare_token"],
                            "approved": True,
                        }
                    )
                    self.assertEqual(len(started["runs"]), count)
                    with condition:
                        self.assertTrue(
                            condition.wait_for(
                                lambda: len(entered) == server.MAX_PARALLEL_RUNS, timeout=5
                            )
                        )
                        self.assertEqual(len(entered), server.MAX_PARALLEL_RUNS)
                        queued = set(releases) - entered
                        completed_thread = next(iter(entered))
                    cancelled_thread = next(iter(queued))
                    self.assertEqual(
                        server._read_json(server._state_path(root / cancelled_thread))["phase"],
                        "queued",
                    )
                    cancelled = server._cancel_run({"run_id": cancelled_thread})
                    self.assertEqual(cancelled["run"]["status"], "cancelled")
                    with server._active_processes_lock:
                        workers = set(server._run_threads.values())
                    self.assertEqual(len(workers), server.MAX_PARALLEL_RUNS)
                    releases[completed_thread].set()
                    self.assertTrue(finished[completed_thread].wait(timeout=5))
                    pending_thread = next(iter(queued - {cancelled_thread}))
                    with condition:
                        self.assertTrue(
                            condition.wait_for(lambda: pending_thread in entered, timeout=5)
                        )
                    with server._active_processes_lock:
                        self.assertLessEqual(set(server._run_threads.values()), workers)
                    payload = server._state_payload()
                    self.assertEqual(len(payload["recent_runs"]), count)
                    self.assertTrue(
                        any(run["status"] == "running" for run in payload["recent_runs"])
                    )
                    report = server._call_tool(
                        {"name": "get_report", "arguments": {"run_id": completed_thread}}
                    )
                    self.assertEqual(
                        report["structuredContent"]["report"]["thread"], completed_thread
                    )
                    self.assertNotIn(cancelled_thread, entered)
                finally:
                    for release in releases.values():
                        release.set()
                    with server._active_processes_lock:
                        workers = list(server._run_threads.values())
                    for worker in workers:
                        worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
