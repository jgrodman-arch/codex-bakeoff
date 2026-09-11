#!/usr/bin/env python3
"""Export bounded numeric replay measurements through Codex's trusted sidecar."""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# Resolve sibling helpers even when the host enables PYTHONSAFEPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_metrics_common as common  # noqa: E402
from replay_metrics_common import (  # noqa: E402
    MAX_MODELS,
    MAX_OUTPUT_BYTES,
    MAX_STATE_BYTES,
    MODEL_SLOTS,
    TERMINAL_STATUSES,
)

SAMPLE_INDEX = Path(__file__).resolve().parents[2] / "assets" / "claude-code-samples" / "index.json"
MAX_OUTPUT_ROWS = 100
REVIEW_DIMENSIONS = (
    "request_fulfillment",
    "code_quality",
    "change_scope",
    "reliability",
    "safe_operations",
    "accurate_reporting",
)
REVIEW_CHECKS: dict[str, tuple[str, ...]] = {
    "request_fulfillment": (
        "required_behavior",
        "stated_constraints",
        "complete_integration",
        "usable_result",
    ),
    "code_quality": (
        "clear_naming",
        "readable_structure",
        "appropriate_complexity",
        "project_conventions",
    ),
    "change_scope": (
        "relevant_files",
        "necessary_changes",
        "preserved_behavior",
        "appropriate_dependencies",
    ),
    "reliability": (
        "invalid_inputs",
        "boundary_conditions",
        "failure_handling",
        "state_consistency",
    ),
    "safe_operations": (
        "preserved_user_work",
        "authorized_actions",
        "protected_sensitive_data",
        "limited_external_changes",
    ),
    "accurate_reporting": (
        "truthful_summary",
        "accurate_outcomes",
        "disclosed_limitations",
        "supported_claims",
    ),
}
RUN_PHASES = (
    "preparing",
    "creating_workspace",
    "implementing",
    "collecting",
    "reviewing",
    "reporting",
)
MAX_RUN_BYTES = 16 * 1024 * 1024
WORKER_CODES = frozenset(
    {
        "invalid_request",
        "invalid_sdk_response",
        "turn_failed",
        "stream_error",
        "incomplete_stream",
        "canceled",
        "codex_unavailable",
        "worker_failed",
        "invalid_json",
    }
)
CONTROLLER_CODES = frozenset({"launch_failed", "coordinator_stopped", "controller_error", "none"})
SYSTEM_CODES = frozenset(
    {
        "econnreset",
        "econnrefused",
        "etimedout",
        "enotfound",
        "eai_again",
        "enoent",
        "eacces",
        "eperm",
        "epipe",
    }
)
WORKER_STAGES = frozenset({"validation", "launch", "turn_start", "stream", "outside_worker"})


def _exit_code(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and -127 <= value <= 255:
        return value
    return None


@dataclass(frozen=True)
class ReplayRun:
    """A terminal replay state and its finalized, content-bearing local artifacts."""

    model: str
    state: Mapping[str, Any]
    run: Mapping[str, Any]
    report: Mapping[str, Any]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _read_json(path: Path, *, maximum: int = MAX_RUN_BYTES) -> Mapping[str, Any]:
    try:
        if path.stat().st_size > maximum:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


@lru_cache(maxsize=1)
def _bundled_samples() -> Mapping[str, tuple[str, str, str]]:
    index = _read_json(SAMPLE_INDEX, maximum=MAX_RUN_BYTES)
    if index.get("schema_version") != 1:
        return {}
    raw = index.get("samples")
    if not isinstance(raw, list):
        return {}
    samples: dict[str, tuple[str, str, str]] = {}
    for sample in raw:
        if not isinstance(sample, Mapping):
            continue
        sample_id = sample.get("id")
        session_id = sample.get("session_id")
        model = sample.get("model")
        baseline_commit = sample.get("baseline_commit")
        if all(
            isinstance(value, str) and value
            for value in (sample_id, session_id, model, baseline_commit)
        ):
            samples[sample_id] = (session_id, model, baseline_commit)
    return samples


def _source(run: ReplayRun) -> str:
    replay = _mapping(run.run.get("replay"))
    imported_thread_id = replay.get("imported_thread_id")
    if not isinstance(imported_thread_id, str) or not imported_thread_id.startswith(
        "claude-sample:"
    ):
        return "imported"
    sample_id = imported_thread_id[len("claude-sample:") :]
    expected = _bundled_samples().get(sample_id)
    if expected is None:
        return "imported"
    expected_session, expected_model, expected_commit = expected
    if replay.get("session_id") != expected_session or replay.get("claude_model") != expected_model:
        return "imported"
    baseline = _mapping(run.run.get("baseline"))
    if baseline.get("commit") != expected_commit:
        return "imported"
    recorded_result = replay.get("recorded_claude_result")
    if not isinstance(recorded_result, Mapping):
        recorded_result = run.report.get("recorded_claude_result")
    if not isinstance(recorded_result, Mapping):
        return "imported"
    recorded_session = recorded_result.get("session_id")
    if recorded_session is not None and recorded_session != expected_session:
        return "imported"
    model_usage = recorded_result.get("modelUsage")
    if not isinstance(model_usage, Mapping) or expected_model not in model_usage:
        return "imported"
    controller_session_id = run.state.get("controller_session_id")
    source_path = replay.get("source_path")
    if not isinstance(controller_session_id, str) or not isinstance(source_path, str):
        return "imported"
    path = Path(source_path)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return "imported"
    run_directory = run.state.get("run_directory")
    if isinstance(run_directory, str):
        expected_source = (
            Path(run_directory).resolve().parent.parent
            / "controllers"
            / controller_session_id
            / "claude-code-samples"
            / sample_id
            / f"{expected_session}.jsonl"
        )
        if resolved != expected_source.resolve():
            return "imported"
    expected_parts = (
        "controllers",
        controller_session_id,
        "claude-code-samples",
        sample_id,
        f"{expected_session}.jsonl",
    )
    if resolved.parts[-len(expected_parts) :] != expected_parts or not resolved.is_file():
        return "imported"
    materialization = _read_json(resolved.parent / "materialization.json", maximum=MAX_STATE_BYTES)
    recorded_transcript = materialization.get("transcript_path")
    if not isinstance(recorded_transcript, str):
        return "imported"
    try:
        trusted_transcript = Path(recorded_transcript).resolve(strict=True)
    except OSError:
        return "imported"
    if (
        materialization.get("sample_id") != sample_id
        or materialization.get("thread_id") != imported_thread_id
        or materialization.get("baseline_commit") != expected_commit
        or trusted_transcript != resolved
    ):
        return "imported"
    repository_path = materialization.get("repository_path")
    if not isinstance(repository_path, str) or baseline.get("repository") != repository_path:
        return "imported"
    if replay.get("project_dir") not in {None, repository_path}:
        return "imported"
    return "sample"


def _model_from_candidates(report: Mapping[str, Any], provider: str) -> object:
    candidates = _mapping(report.get("candidates"))
    return _mapping(candidates.get(provider)).get("model")


def _failure_phase(phase: object) -> str:
    phases = {
        "preparing": "preparation",
        "creating_workspace": "preparation",
        "implementing": "implementation",
        "collecting": "implementation",
        "reviewing": "evaluation",
        "reporting": "reporting",
    }
    return phases.get(phase, "unknown") if isinstance(phase, str) else "unknown"


def _usage_observed(run: ReplayRun, provider: str) -> bool:
    raw_usage = _mapping(run.report.get("usage")).get(provider)
    if isinstance(raw_usage, list) and any(isinstance(item, Mapping) for item in raw_usage):
        return True
    if provider == "claude":
        replay = _mapping(run.run.get("replay"))
        historical = replay.get("historical_usage")
        if isinstance(historical, Mapping) and historical:
            return True
        recorded = _mapping(replay.get("recorded_claude_result"))
        if not recorded:
            recorded = _mapping(run.report.get("recorded_claude_result"))
        return bool(_mapping(recorded.get("usage")))
    return bool(_mapping(_mapping(run.report.get("codex_execution")).get("usage")))


def _usage_fields(run: ReplayRun, provider: str) -> dict[str, int]:
    if not _usage_observed(run, provider):
        return {}
    usage = _mapping(_mapping(run.report.get("normalized_usage")).get(provider))
    raw = _mapping(run.report.get("usage")).get(provider)
    records = [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    fields = (
        ("input_tokens", usage.get("total_input_tokens")),
        ("output_tokens", usage.get("output_tokens")),
        (
            "cached_input_tokens",
            usage.get("cached_input_tokens"),
        ),
        ("ordinary_input_tokens", usage.get("ordinary_input_tokens")),
        (
            "cache_write_tokens",
            usage.get("cache_write_tokens"),
        ),
    )
    result = {name: number for name, value in fields if (number := _integer(value)) is not None}
    for name, aliases in (
        ("cache_write_1h_tokens", ("cache_write_1h_tokens", "ephemeral_1h_input_tokens")),
        ("cache_write_5m_tokens", ("cache_write_5m_tokens", "ephemeral_5m_input_tokens")),
        ("reasoning_output_tokens", ("reasoning_output_tokens", "reasoning_tokens")),
    ):
        values = []
        for record in records:
            cache_creation = _mapping(record.get("cache_creation"))
            for alias in aliases:
                if (number := _integer(record.get(alias, cache_creation.get(alias)))) is not None:
                    values.append(number)
                    break
        execution_usage = _mapping(_mapping(run.report.get("codex_execution")).get("usage"))
        if not values and provider == "codex":
            for alias in aliases:
                if (number := _integer(execution_usage.get(alias))) is not None:
                    values.append(number)
                    break
        if values:
            result[name] = sum(values)
    if records:
        result["usage_record_count"] = len(records)
    return result


def _phase_durations(run: ReplayRun) -> dict[str, float]:
    raw_events = run.state.get("events")
    if not isinstance(raw_events, list):
        return {}
    transitions = [
        (phase, timestamp)
        for event in raw_events
        if isinstance(event, Mapping)
        and (phase := event.get("phase")) in RUN_PHASES
        and (timestamp := common.timestamp(event.get("at"))) is not None
    ]
    result: dict[str, float] = {}
    for (phase, started), (_, ended) in itertools.pairwise(transitions):
        seconds = (ended - started).total_seconds()
        if seconds >= 0:
            result[phase] = result.get(phase, 0) + seconds
    return result


def _cost(run: ReplayRun, provider: str) -> int | float | None:
    estimate = _mapping(_mapping(run.report.get("estimated_cost")).get(provider))
    if estimate.get("status") != "estimated":
        return None
    return _number(estimate.get("usd"))


def _claude_duration(run: ReplayRun) -> tuple[int | float | None, str]:
    replay = _mapping(run.run.get("replay"))
    recorded = _mapping(replay.get("recorded_claude_result"))
    if not recorded:
        recorded = _mapping(run.report.get("recorded_claude_result"))
    if recorded:
        duration = _number(recorded.get("duration_ms"))
        if duration is None:
            duration = _number(
                run.report.get(
                    "historical_wall_clock_seconds", replay.get("historical_wall_clock_seconds")
                )
            )
        else:
            duration /= 1000
        basis = "recorded_wall_clock"
    else:
        duration = _number(
            run.report.get(
                "historical_model_request_seconds", replay.get("historical_model_request_seconds")
            )
        )
        basis = "model_request"
    return duration, basis


def _candidate_labels(evaluation: Mapping[str, Any]) -> Mapping[str, str]:
    candidates = _mapping(evaluation.get("candidate_mapping"))
    result: dict[str, str] = {}
    for label, provider in candidates.items():
        if isinstance(label, str) and provider in {"codex", "claude"}:
            result[provider] = label
    return result if set(result) == {"codex", "claude"} else {}


def _quality_fields(run: ReplayRun) -> dict[str, object]:
    if run.state.get("status") != "completed":
        return {}
    evaluation = _mapping(run.report.get("evaluation"))
    if evaluation.get("status") != "completed":
        return {}
    labels = _candidate_labels(evaluation)
    reviews = evaluation.get("reviews")
    if not labels or not isinstance(reviews, list):
        return {}
    valid_reviews = [
        review
        for review in reviews
        if isinstance(review, Mapping) and review.get("status") == "completed"
    ]
    if not valid_reviews:
        return {}

    totals = _mapping(evaluation.get("totals"))
    codex_score = _number(totals.get(labels["codex"]))
    claude_score = _number(totals.get(labels["claude"]))
    if codex_score is None or claude_score is None:
        winner = "unavailable"
    else:
        codex_comparable = int(codex_score * 100 + 0.5)
        claude_comparable = int(claude_score * 100 + 0.5)
        if codex_comparable > claude_comparable:
            winner = "codex"
        elif claude_comparable > codex_comparable:
            winner = "claude"
        else:
            winner = "tie"

    fields: dict[str, object] = {"winner": winner}
    if codex_score is not None:
        fields["codex_score"] = codex_score
    if claude_score is not None:
        fields["claude_score"] = claude_score
    comparable = evaluation.get("comparable_dimensions")
    allowed = (
        {dimension for dimension in comparable if isinstance(dimension, str)}
        if isinstance(comparable, list)
        else set(REVIEW_DIMENSIONS)
    )
    dimension_scores: dict[str, dict[str, int | float]] = {}
    dimension_winners: dict[str, str] = {}
    for dimension in REVIEW_DIMENSIONS:
        if dimension not in allowed:
            continue
        provider_scores: dict[str, int | float] = {}
        for provider in ("codex", "claude"):
            scores: list[int | float] = []
            for review in valid_reviews:
                ballot = _mapping(review.get("ballot"))
                decision = _mapping(_mapping(ballot.get("dimensions")).get(dimension))
                if decision.get("winner") == "not_applicable":
                    continue
                candidate = _mapping(_mapping(decision.get("candidates")).get(labels[provider]))
                score = _number(candidate.get("score"))
                if score is not None:
                    scores.append(score)
            if scores:
                provider_scores[provider] = sum(scores) / len(scores)
        if set(provider_scores) == {"codex", "claude"}:
            dimension_scores[dimension] = provider_scores
            codex_score = provider_scores["codex"]
            claude_score = provider_scores["claude"]
            dimension_winners[dimension] = (
                "codex"
                if codex_score > claude_score
                else "claude"
                if claude_score > codex_score
                else "tie"
            )
    if dimension_scores:
        fields["dimension_scores"] = dimension_scores
        fields["dimension_winners"] = dimension_winners
    return fields


def _run_metadata(run: ReplayRun) -> dict[str, object]:
    report = run.report
    evaluation = _mapping(report.get("evaluation"))
    availability = evaluation.get("evaluator_availability")
    reviews = evaluation.get("reviews")
    capabilities = _mapping(report.get("capabilities"))
    result: dict[str, object] = {}
    list_counts = (
        ("changed_file_count", report.get("codex_changed_files")),
        ("capability_count", capabilities.get("items")),
        ("unavailable_capability_count", capabilities.get("unavailable_capabilities")),
        ("limitation_count", report.get("limitations")),
        ("review_count", reviews),
        ("comparable_dimension_count", evaluation.get("comparable_dimensions")),
    )
    for name, values in list_counts:
        if isinstance(values, list):
            result[name] = len(values)
    attempt = _integer(run.state.get("implementation_attempt"))
    if attempt is not None:
        result["implementation_attempt_count"] = attempt
    started = common.timestamp(run.state.get("started_at"))
    completed = common.timestamp(run.state.get("completed_at"))
    if started is not None and completed is not None:
        elapsed = (completed - started).total_seconds()
        if elapsed >= 0:
            result["wall_clock_seconds"] = elapsed
    if durations := _phase_durations(run):
        result["phase_durations"] = durations
    if isinstance(availability, list):
        evaluators = [item for item in availability if isinstance(item, Mapping)]
        result["evaluator_count"] = len(evaluators)
        result["available_evaluator_count"] = sum(
            evaluator.get("available") is True for evaluator in evaluators
        )
    if isinstance(reviews, list):
        selected = []
        for review in reviews:
            if not isinstance(review, Mapping):
                continue
            evaluator_model = review.get("model")
            if not isinstance(evaluator_model, str):
                continue
            family = common.model_family(
                evaluator_model, ("sol", "terra", "luna", "opus", "sonnet", "haiku")
            )
            selected.append(
                {
                    "model": family,
                    "provider": (
                        "codex"
                        if family in {"sol", "terra", "luna"}
                        else "claude"
                        if family in {"opus", "sonnet", "haiku"}
                        else "other"
                    ),
                    "self_judged": "yes" if evaluator_model == run.model else "no",
                    "normalized": (
                        "yes"
                        if _mapping(review.get("normalization")).get("required") is True
                        else "no"
                    ),
                }
            )
        if selected:
            result["evaluators"] = selected
    worker_events = run.state.get("worker_events")
    if isinstance(worker_events, list):
        for event in reversed(worker_events):
            if not isinstance(event, Mapping) or event.get("type") != "completed":
                continue
            counts = _mapping(event.get("itemCounts"))
            for metric, item in (
                ("tool_call_count", "command_execution"),
                ("file_change_event_count", "file_change"),
                ("agent_message_count", "agent_message"),
                ("worker_error_count", "error"),
            ):
                if (count := _integer(counts.get(item))) is not None:
                    result[metric] = count
            break
    return result


def _codex_model(run: ReplayRun) -> dict[str, object]:
    status = run.state.get("status")
    if status not in TERMINAL_STATUSES:
        return {}
    fields: dict[str, object] = {
        "model": common.model_family(run.model, ("sol", "terra", "luna")),
        "status": status,
    }
    if status != "completed":
        # A cancelled retry may still carry the preceding attempt's failure.
        diagnostic = _mapping(run.state.get("failure_diagnostic")) if status == "failed" else {}
        code = diagnostic.get("worker_code")
        controller_code = diagnostic.get("controller_code")
        system = diagnostic.get("system_code")
        system = system.lower() if isinstance(system, str) else "unknown"
        stage = diagnostic.get("worker_stage")
        fields["failure"] = {
            "phase": _failure_phase(run.state.get("phase")),
            "controller_code": controller_code
            if isinstance(controller_code, str) and controller_code in CONTROLLER_CODES
            else "unknown",
            "worker_code": code if isinstance(code, str) and code in WORKER_CODES else "unknown",
            "retryable": (
                "yes"
                if diagnostic.get("retryable") is True
                else "no"
                if diagnostic.get("retryable") is False
                else "unknown"
            ),
        }
        details: dict[str, object] = {
            "system_code": system if system in SYSTEM_CODES else "unknown",
            "worker_stage": stage
            if isinstance(stage, str) and stage in WORKER_STAGES
            else "unknown",
        }
        if (exit_code := _exit_code(diagnostic.get("exit_code"))) is not None:
            details["exit_code"] = exit_code
        retries = _integer(diagnostic.get("retry_count"))
        if retries is not None and retries <= 3:
            details["retry_count"] = retries
        if (elapsed_ms := _number(diagnostic.get("elapsed_ms"))) is not None:
            details["duration_seconds"] = elapsed_ms / 1000
        fields["failure_details"] = details
        return fields
    fields.update(_usage_fields(run, "codex"))
    fields.update(_run_metadata(run))
    if (cost := _cost(run, "codex")) is not None:
        fields["cost_usd"] = cost
    duration = _number(_mapping(run.report.get("codex_execution")).get("elapsed_seconds"))
    if duration is not None:
        fields["duration_seconds"] = duration
    fields.update(_quality_fields(run))
    return fields


def _claude_baseline(runs: Sequence[ReplayRun]) -> dict[str, object]:
    preferred = sorted(runs, key=lambda run: run.state.get("status") != "completed")
    model: object = None
    for run in preferred:
        replay = _mapping(run.run.get("replay"))
        model = replay.get("claude_model")
        if not isinstance(model, str):
            model = _model_from_candidates(run.report, "claude")
        if isinstance(model, str):
            break
    baseline: dict[str, object] = {
        "model": common.model_family(model, ("fable", "opus", "sonnet", "haiku"))
    }
    for run in preferred:
        for name, value in _usage_fields(run, "claude").items():
            baseline.setdefault(name, value)
        if "cost_usd" not in baseline and (cost := _cost(run, "claude")) is not None:
            baseline["cost_usd"] = cost
        if "duration_seconds" not in baseline:
            duration, timing_basis = _claude_duration(run)
            if duration is not None:
                baseline["duration_seconds"] = duration
                baseline["timing_basis"] = timing_basis
        replay = _mapping(run.run.get("replay"))
        changed = replay.get("historical_changed_files")
        if "changed_file_count" not in baseline and isinstance(changed, list):
            baseline["changed_file_count"] = len(changed)
        wall_clock = _number(
            run.report.get(
                "historical_wall_clock_seconds", replay.get("historical_wall_clock_seconds")
            )
        )
        if "wall_clock_seconds" not in baseline and wall_clock is not None:
            baseline["wall_clock_seconds"] = wall_clock
        timing = _mapping(replay.get("historical_model_request_timing"))
        for name in (
            "request_count",
            "missing_request_count",
            "unidentified_assistant_event_count",
        ):
            if name not in baseline and (count := _integer(timing.get(name))) is not None:
                baseline[name] = count
    return baseline


def _replay_event(runs: Sequence[ReplayRun]) -> Mapping[str, object]:
    # Artifact-less launch failures carry no independent provenance. They must
    # not reclassify the verified sibling runs in the same approved comparison.
    evidenced_runs = [run for run in runs if run.run or not run.state.get("launch_failed")]
    source = (
        "sample"
        if evidenced_runs and all(_source(run) == "sample" for run in evidenced_runs)
        else "imported"
    )
    models = [
        _codex_model(run)
        for run in runs[:MAX_MODELS]
        if run.state.get("status") in TERMINAL_STATUSES
    ]
    statuses = {model["status"] for model in models}
    status = next(iter(statuses)) if len(statuses) == 1 else "partial"
    return {
        "name": "replay_completed",
        "payload": {
            "source": source,
            "status": status,
            "claude": _claude_baseline(runs),
            "codex_models": models,
        },
    }


def _measurements(event: Mapping[str, object]) -> list[dict[str, object]]:
    payload = _mapping(event.get("payload"))
    claude = _mapping(payload.get("claude"))
    source = payload.get("source")
    claude_model = claude.get("model")
    raw_models = payload.get("codex_models")
    if not isinstance(source, str) or not isinstance(claude_model, str):
        return []
    if not isinstance(raw_models, list):
        return []

    models = [model for model in raw_models[:MAX_MODELS] if isinstance(model, Mapping)]
    contexts: list[tuple[Mapping[str, Any], dict[str, str]]] = []
    for index, model in enumerate(models):
        codex_model = model.get("model")
        if not isinstance(codex_model, str):
            continue
        contexts.append(
            (
                model,
                {
                    "model_slot": MODEL_SLOTS[index],
                    "codex_model": codex_model,
                    "claude_model": claude_model,
                    "source": source,
                },
            )
        )

    rows: list[dict[str, object]] = []
    for model, dimensions in contexts:
        status = model.get("status")
        if isinstance(status, str):
            rows.append(common.measurement("replay", 1, {**dimensions, "status": status}))

    claude_dimensions = {"claude_model": claude_model, "source": source}
    for field in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_usd",
        "wall_clock_seconds",
    ):
        value = _number(claude.get(field))
        if value is not None:
            rows.append(common.measurement(f"claude_{field}", value, claude_dimensions))
    duration = _number(claude.get("duration_seconds"))
    timing_basis = claude.get("timing_basis")
    if duration is not None and isinstance(timing_basis, str):
        rows.append(
            common.measurement(
                "claude_duration_seconds",
                duration,
                {**claude_dimensions, "timing_basis": timing_basis},
            )
        )

    for model, dimensions in contexts:
        failure = _mapping(model.get("failure"))
        phase = failure.get("phase")
        if isinstance(phase, str):
            rows.append(
                common.measurement(
                    "failure",
                    1,
                    {
                        **dimensions,
                        "phase": phase,
                        "controller_code": str(failure.get("controller_code", "unknown")),
                        "worker_code": str(failure.get("worker_code", "unknown")),
                        "retryable": str(failure.get("retryable", "unknown")),
                    },
                )
            )
        winner = model.get("winner")
        if isinstance(winner, str):
            rows.append(common.measurement("outcome", 1, {**dimensions, "winner": winner}))

    for model, dimensions in contexts:
        details = _mapping(model.get("failure_details"))
        if not details:
            continue
        rows.append(
            common.measurement(
                "failure_detail",
                1,
                {
                    **dimensions,
                    "system_code": str(details["system_code"]),
                    "worker_stage": str(details["worker_stage"]),
                },
            )
        )
        for field in ("exit_code", "retry_count", "duration_seconds"):
            value = (
                _exit_code(details.get(field))
                if field == "exit_code"
                else _number(details.get(field))
            )
            if value is not None:
                rows.append(common.measurement(f"failure_{field}", value, dimensions))

    for field in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_usd",
        "duration_seconds",
        "wall_clock_seconds",
    ):
        for model, dimensions in contexts:
            value = _number(model.get(field))
            if value is not None:
                rows.append(common.measurement(f"codex_{field}", value, dimensions))

    for field in ("codex_score", "claude_score"):
        for model, dimensions in contexts:
            value = _number(model.get(field))
            if value is not None:
                rows.append(common.measurement(field, value, dimensions))

    for model, dimensions in contexts:
        for phase, value in _mapping(model.get("phase_durations")).items():
            seconds = _number(value)
            if phase in {"implementing", "reviewing"} and seconds is not None:
                rows.append(
                    common.measurement(
                        "codex_phase_duration_seconds", seconds, {**dimensions, "phase": phase}
                    )
                )
        evaluator_counts: dict[tuple[str, str, str, str], int] = {}
        evaluators = model.get("evaluators")
        if isinstance(evaluators, list):
            for evaluator in evaluators:
                if not isinstance(evaluator, Mapping):
                    continue
                values = tuple(
                    evaluator.get(key) for key in ("model", "provider", "self_judged", "normalized")
                )
                if all(isinstance(value, str) for value in values):
                    key = (str(values[0]), str(values[1]), str(values[2]), str(values[3]))
                    evaluator_counts[key] = evaluator_counts.get(key, 0) + 1
        for (
            evaluator_model,
            evaluator_provider,
            self_judged,
            normalized,
        ), count in evaluator_counts.items():
            rows.append(
                common.measurement(
                    "evaluation",
                    count,
                    {
                        **dimensions,
                        "evaluator_model": evaluator_model,
                        "evaluator_provider": evaluator_provider,
                        "self_judged": self_judged,
                        "normalized": normalized,
                    },
                )
            )

    for dimension in REVIEW_DIMENSIONS:
        for model, dimensions in contexts:
            scores = _mapping(_mapping(model.get("dimension_scores")).get(dimension))
            for provider in ("codex", "claude"):
                value = _number(scores.get(provider))
                if value is not None:
                    rows.append(
                        common.measurement(
                            f"{provider}_dimension_score",
                            value,
                            {**dimensions, "score_dimension": dimension},
                        )
                    )
            winner = _mapping(model.get("dimension_winners")).get(dimension)
            if winner in {"codex", "claude", "tie"}:
                rows.append(
                    common.measurement(
                        "dimension_outcome",
                        1,
                        {**dimensions, "score_dimension": dimension, "winner": winner},
                    )
                )

    return rows


def _payload(event: Mapping[str, object]) -> bytes:
    return common.encode_measurements(_measurements(event))


@dataclass
class AttemptObservation:
    outcome: str
    runs: list[ReplayRun]
    models: list[tuple[str, str]]
    controller_ready: bool = False
    start_requested: bool = False
    run_observed: bool = False


def _collect_attempt_runs(
    attempt: Mapping[str, Any], run_root: Path, session: str
) -> tuple[list[ReplayRun], list[tuple[str, str]], bool, bool]:
    """Read detailed run artifacts once after a durable terminal receipt."""
    common.approved_models(attempt)
    entries = attempt.get("models", [])
    runs: list[ReplayRun] = []
    models: list[tuple[str, str]] = []
    run_observed = False
    unreadable = False
    for entry in entries:
        model = entry["model"]
        status = "unresolved"
        try:
            if entry.get("launch_status") == "failed":
                status = "launch_failed"
                runs.append(
                    ReplayRun(
                        model,
                        {
                            "status": "failed",
                            "phase": "creating_workspace",
                            "launch_failed": True,
                            "error": entry.get("error"),
                            "failure_diagnostic": {"controller_code": entry.get("controller_code")},
                        },
                        {},
                        {},
                    )
                )
            elif entry.get("run_id") is not None:
                state = common.model_state(entry, run_root, session)
                directory = run_root / entry["run_id"]
                if state is not None:
                    run_observed = True
                    terminal = state.get("status")
                    if terminal in TERMINAL_STATUSES:
                        report = common.observed_json(directory / "report.json", maximum=None)
                        if terminal != "completed" or report is not None:
                            status = terminal
                            run = common.observed_json(
                                directory / "run.json", maximum=MAX_RUN_BYTES
                            )
                            runs.append(ReplayRun(model, state, run or {}, report or {}))
        except (OSError, ValueError, UnicodeError):
            status = "unreadable"
            unreadable = True
        models.append((model, status))
    return runs, models, run_observed, unreadable


def _resolved_outcome(models: Sequence[tuple[str, str]]) -> str | None:
    statuses = {status for _, status in models}
    if not models or statuses.intersection({"unresolved", "unreadable"}):
        return None
    return (
        "succeeded"
        if statuses == {"completed"}
        else "launch_failure"
        if statuses == {"launch_failed"}
        else "failed"
        if statuses <= {"failed", "launch_failed"}
        else "cancelled"
        if statuses == {"cancelled"}
        else "mixed"
    )


def _observe_attempt(
    arguments: argparse.Namespace,
    run_root: Path,
) -> AttemptObservation:
    session = arguments.controller_session_id
    root = run_root.parent / "controllers" / session
    pre_start_deadline = time.monotonic() + arguments.pre_start_timeout_seconds
    deadline: float | None = None
    started_at: dt.datetime | None = None
    controller_pid: int | None = None
    observed = AttemptObservation("no_run_observed", [], [])
    while True:
        try:
            attempt = common.read_attempt_if_present(root / "attempt.json", session)
            if attempt is None:
                if time.monotonic() >= pre_start_deadline:
                    observed.outcome = "prestart_timeout"
                    return observed
                common.pause(arguments.poll_interval_seconds, pre_start_deadline)
                continue
            observed.controller_ready |= attempt.get("controller_ready") is True
            observed.start_requested |= attempt.get("start_requested") is True
            if attempt.get("startup_failed") is True:
                observed.outcome = "startup_failure"
                return observed
            selected = common.approved_models(attempt)
            observed.models = [(model, "unresolved") for model in selected]
            started_at = common.start_time(attempt, selected)
            if started_at is not None and deadline is None:
                deadline = common.observation_deadline(started_at, arguments.run_timeout_seconds)
            controller_pid = common.persisted_pid(attempt, controller_pid)
            if attempt.get("final_results_ready") is True:
                (
                    observed.runs,
                    observed.models,
                    observed.run_observed,
                    unreadable,
                ) = _collect_attempt_runs(attempt, run_root, session)
                outcome = _resolved_outcome(observed.models)
                observed.outcome = (
                    outcome if outcome is not None and not unreadable else "unreadable_state"
                )
                return observed
        except (OSError, ValueError, UnicodeError):
            observed.outcome = "unreadable_state"
            return observed

        expired = deadline is not None and time.monotonic() >= deadline
        pre_start_expired = deadline is None and time.monotonic() >= pre_start_deadline
        dead = False
        try:
            dead, controller_pid = common.controller_dead(
                root, session, controller_pid, stopped=attempt.get("controller_stopped") is True
            )
        except (OSError, ValueError, UnicodeError):
            observed.outcome = "unreadable_state"
            return observed
        if expired or pre_start_expired or dead:
            try:
                (
                    observed.runs,
                    observed.models,
                    observed.run_observed,
                    unreadable,
                ) = _collect_attempt_runs(attempt, run_root, session)
            except (OSError, ValueError, UnicodeError):
                unreadable = True
            outcome = _resolved_outcome(observed.models)
            if outcome is not None and not unreadable:
                observed.outcome = outcome
                return observed
            statuses = {status for _, status in observed.models}
            observed.outcome = (
                "unreadable_state"
                if unreadable or "unreadable" in statuses
                else "prestart_timeout"
                if pre_start_expired
                else "interrupted"
                if dead and observed.start_requested
                else "observation_timeout"
                if observed.start_requested
                else "no_run_observed"
            )
            return observed
        common.pause(
            arguments.poll_interval_seconds,
            deadline if deadline is not None else pre_start_deadline,
        )


def _attempt_measurements(observed: AttemptObservation) -> list[dict[str, object]]:
    if observed.outcome == "prestart_timeout":
        return [common.measurement("final_prestart_timeout", 1, {})]
    rows = [
        common.measurement(
            "attempt",
            1,
            {
                "outcome": observed.outcome,
                "controller_ready": "yes" if observed.controller_ready else "no",
                "start_requested": "yes" if observed.start_requested else "no",
                "run_observed": "yes" if observed.run_observed else "no",
                "reporting_complete": "yes"
                if observed.outcome
                in {"succeeded", "failed", "mixed", "cancelled", "launch_failure"}
                else "no",
            },
        )
    ]
    for index, (model, status) in enumerate(observed.models):
        if status in TERMINAL_STATUSES or status == "launch_failed":
            continue  # The existing replay row already accounts for this model.
        rows.append(
            common.measurement(
                "attempt_model",
                1,
                {
                    "model_slot": MODEL_SLOTS[index],
                    "codex_model": common.model_family(model, ("sol", "terra", "luna")),
                    "status": status,
                },
            )
        )
    if observed.runs:
        model_rows = _measurements(_replay_event(observed.runs))
        # Preserve approved slots even when an earlier model is still unresolved.
        slots = {model: MODEL_SLOTS[i] for i, (model, _) in enumerate(observed.models)}
        for row in model_rows:
            dimensions = row.get("dimensions")
            if isinstance(dimensions, dict) and "model_slot" in dimensions:
                index = MODEL_SLOTS.index(dimensions["model_slot"])
                dimensions["model_slot"] = slots[observed.runs[index].model]
        rows.extend(model_rows)
    return rows


def _bounded_payload(complete_rows: Sequence[dict[str, object]]) -> bytes:
    rows = list(complete_rows)
    omitted = 0
    while True:
        submitted = rows + (
            [common.measurement("measurements_omitted", omitted, {})] if omitted else []
        )
        encoded = common.encode_measurements(submitted)
        if len(submitted) <= MAX_OUTPUT_ROWS and len(encoded) <= MAX_OUTPUT_BYTES:
            return encoded
        if len(rows) <= 1:
            raise ValueError("Replay attempt summary exceeds the sidecar limit")
        if not omitted and rows[0]["name"] == "attempt":
            rows[0] = {
                **rows[0],
                "dimensions": {**_mapping(rows[0]["dimensions"]), "reporting_complete": "no"},
            }
        rows.pop()
        omitted += 1


def main(argv: Sequence[str] | None = None) -> int:
    if not common.analytics_available():
        return 0
    arguments = common.arguments(argv)
    try:
        common.require_session(arguments.controller_session_id)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    run_root = common.run_root(arguments)
    root = run_root.parent / "controllers" / arguments.controller_session_id
    try:
        sidecar = common.open_sidecar()
        if sidecar is None:
            return 0
        with sidecar:
            common.observer_started("final_results", arguments.controller_session_id)
            observed = _observe_attempt(arguments, run_root)
            rows = _attempt_measurements(observed)
            complete_payload = common.encode_measurements(rows)
            payload = _bounded_payload(rows)
            if payload != complete_payload:
                print(
                    "Replay final metrics exceed the host limit; reporting_complete=no. "
                    "Full local measurements are not a delivery acknowledgment.",
                    file=sys.stderr,
                )
            common.write_sidecar(sidecar, payload)
    except (OSError, ValueError) as error:
        print(f"Unable to write the trusted Replay sidecar: {error}.", file=sys.stderr)
        return 1
    common.preserve_payload(root / "metrics.json", payload)
    common.preserve_payload(root / "metrics-full.json", complete_payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
