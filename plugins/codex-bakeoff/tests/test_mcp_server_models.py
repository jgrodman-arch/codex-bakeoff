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
  extract("      const formatCost =", "      function unwrapToolResult"),
  extract("      function terminalRunStatus", "      function stepIndex"),
  extract("      function outcomeSummary(evaluation,", "      function comparisonClass"),
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
) -> dict[str, object]:
    harness = (
        _CONTROLLER_HARNESS
        + r"""
const context = new Function("serverState", "rememberedRunId", [
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
  "const callTool = async name => name === 'get_state' ? { state: serverState } : { threads: [], total: 0 };",
  "const window = { setTimeout: () => events.push(['refresh']) };",
  extract("      async function initialize()", "      async function selectThread"),
  "return { initialize, state, events };",
].join("\n"))(JSON.parse(process.argv[2]), process.argv[3]);
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
        ["node", "-e", harness, str(CONTROLLER_PATH), json.dumps(server_state), remembered_run_id],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


class McpServerModelTests(unittest.TestCase):
    def test_configuration_schemas_allow_all_available_codex_models(self) -> None:
        server = load_server()

        for approval in (False, True):
            with self.subTest(approval=approval):
                models = server._configuration_schema(approval=approval)["properties"]["models"]
                self.assertEqual(models["type"], "array")
                self.assertEqual(models["minItems"], 1)
                self.assertEqual(models["maxItems"], server.MAX_SELECTION_ITEMS)
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
        self.assertIn(
            "Select one or more models. Selected models run in parallel.",
            configure,
        )
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
                "estimated_cost": {"codex": {"usd": 0.042}},
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
        self.assertIn("Codex replay leads", rendered)
        self.assertIn("Historical Claude: 25%", rendered)
        self.assertIn("Codex replay: 75%", rendered)
        self.assertEqual(rendered.count("Estimated cost"), 3)
        self.assertIn("$0.04", rendered)
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
