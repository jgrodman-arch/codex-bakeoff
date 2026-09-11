"""Approved multi-thread groups and their durable launch membership."""

from __future__ import annotations

import hashlib
import importlib
import os
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Lock = importlib.import_module("final_results_receipt").Lock
_configuration = importlib.import_module("replay_configuration")
ControllerError = _configuration.ControllerError
_normalized_configuration = _configuration._normalized_configuration
_replay_range = _configuration._replay_range


class CoordinatorQueue:
    """Drain approved runs with a bounded number of reusable daemon workers."""

    def __init__(
        self,
        *,
        lock: Lock,
        shutdown: threading.Event,
        max_workers: int,
        run: Callable[[Path, Mapping[str, Any]], None],
    ) -> None:
        self.lock = lock
        self.shutdown = shutdown
        self.max_workers = max_workers
        self.run = run
        self.cancellations: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self._pending: deque[tuple[Path, Mapping[str, Any]]] = deque()
        self._workers: set[threading.Thread] = set()

    def active_count(self) -> int:
        with self.lock:
            return len(self.threads) + sum(
                not self.cancellations[directory.name].is_set() for directory, _ in self._pending
            )

    def submit(self, directory: Path, request: Mapping[str, Any]) -> None:
        with self.lock:
            if self.shutdown.is_set():
                raise ControllerError("The comparison supervisor is shutting down.")
            self.cancellations[directory.name] = threading.Event()
            self._pending.append((directory, request))
            if len(self._workers) >= self.max_workers:
                return
            worker = threading.Thread(target=self._drain, name="replay-supervisor", daemon=True)
            self._workers.add(worker)
            try:
                worker.start()
            except Exception:
                self._workers.remove(worker)
                self._pending.pop()
                self.cancellations.pop(directory.name)
                raise

    def _drain(self) -> None:
        worker = threading.current_thread()
        try:
            while True:
                with self.lock:
                    if not self._pending or self.shutdown.is_set():
                        # Retire under the enqueue lock so a later submission starts a worker.
                        self._workers.remove(worker)
                        return
                    directory, request = self._pending.popleft()
                    if self.cancellations[directory.name].is_set():
                        self.cancellations.pop(directory.name)
                        continue
                    self.threads[directory.name] = worker
                try:
                    self.run(directory, request)
                finally:
                    with self.lock:
                        self.threads.pop(directory.name, None)
                        self.cancellations.pop(directory.name, None)
        finally:
            with self.lock:
                self._workers.discard(worker)

    def stop(self) -> None:
        with self.lock:
            self.shutdown.set()
            for cancellation in self.cancellations.values():
                cancellation.set()
            for directory, _ in self._pending:
                self.cancellations.pop(directory.name)
            self._pending.clear()


@dataclass
class BatchController:
    lock: Lock
    prepared_runs: dict[str, dict[str, Any]]
    attempt_path: Callable[[], Path]
    session_id: Callable[[], str]
    read_json: Callable[..., dict[str, Any]]
    write_json: Callable[[Path, Mapping[str, Any]], None]
    recent_runs: Callable[..., list[dict[str, Any]]]
    pid_is_alive: Callable[[int], bool]
    record_model_launch: Callable[..., None]
    prepare_payload: Callable[[Mapping[str, Any]], dict[str, Any]]
    ensure_can_start: Callable[[], None]
    fingerprint: Callable[[Mapping[str, Any]], str]
    started_runs_response: Callable[..., dict[str, Any]]
    update_attempt: Callable[..., None]
    now: Callable[[], str]
    start_model: Callable[..., dict[str, Any]]
    max_record_bytes: int
    max_threads: int
    max_models: int
    max_prepare_tokens: int
    max_parallel_runs: int

    def _batch_path(self) -> Path:
        return self.attempt_path().with_name("batch.json")

    def _batch_summary(self) -> dict[str, Any] | None:
        if not self._batch_path().is_file():
            return None
        with self.lock:
            batch = self.read_json(self._batch_path(), maximum=self.max_record_bytes)
            self._recover_interrupted_batch(batch)
        return {
            key: batch[key]
            for key in (
                "id",
                "thread_count",
                "models",
                "thread_ids",
                "threads",
                "errors",
                "starting",
            )
        }

    def _recover_interrupted_batch(self, batch: dict[str, Any]) -> None:
        if not batch["starting"] or self.pid_is_alive(batch["controller_pid"]):
            return
        states = {
            (state.get("thread_id"), state.get("model"))
            for state in self.recent_runs(limit=self.max_threads * self.max_models)
            if state.get("batch_id") == batch["id"] and state.get("launch_failed") is not True
        }
        errors = {(error["thread_id"], error["model"]) for error in batch["errors"]}
        for thread in batch["threads"]:
            for model in batch["models"]:
                if (thread["thread_id"], model) not in states | errors:
                    batch["errors"].append(
                        {
                            **thread,
                            "model": model,
                            "error": "Controller stopped before launch.",
                            "controller_code": "launch_failed",
                        }
                    )
                    if thread["thread_id"] == batch["thread_ids"][0]:
                        self.record_model_launch(
                            model, launch_status="failed", controller_code="launch_failed"
                        )
        batch["starting"] = False
        self.write_json(self._batch_path(), batch)

    def _batch_configurations(self, arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw = arguments.get("configurations")
        if not isinstance(raw, list) or not 1 <= len(raw) <= self.max_threads:
            raise ControllerError(f"Choose between 1 and {self.max_threads} threads.")
        configurations: list[dict[str, Any]] = []
        identities: set[str] = set()
        selected_models: list[str] | None = None
        for item in raw:
            if not isinstance(item, Mapping):
                raise ControllerError("Each thread needs a replay configuration.")
            configuration = _normalized_configuration(item)
            if _replay_range(configuration):
                raise ControllerError("Multiple-thread replays require whole threads.")
            thread_id = configuration["thread_id"]
            if thread_id in identities:
                raise ControllerError("Choose each thread only once.")
            identities.add(thread_id)
            models = list(configuration.get("models") or [configuration["model"]])
            if selected_models is not None and models != selected_models:
                raise ControllerError("Choose the same Codex models for every thread.")
            selected_models = models
            title = item.get("thread_title", thread_id)
            if not isinstance(title, str) or not title.strip() or len(title) > 2_000:
                raise ControllerError("Each thread needs a valid title.")
            configuration["thread_title"] = title.strip()
            configurations.append(configuration)
        return configurations

    def _prepare_batch(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        configurations = self._batch_configurations(arguments)
        with self.lock:
            self.ensure_can_start()
        preparations: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        blockers: list[str] = []
        for configuration in configurations:
            try:
                prepared = self.prepare_payload(configuration)
            except ControllerError as error:
                prepared = {"ready": False, "can_run": False, "blocking_reasons": [str(error)]}
            preparations.append(
                {
                    "thread_id": configuration["thread_id"],
                    "thread_title": configuration["thread_title"],
                    "preparation": prepared,
                }
            )
            token = prepared.get("prepare_token")
            with self.lock:
                receipt = self.prepared_runs.get(token) if isinstance(token, str) else None
                if receipt is not None:
                    receipts.append(dict(receipt))
            if prepared.get("ready") is not True:
                blockers.extend(
                    f"{configuration['thread_title']}: {reason}"
                    for reason in prepared.get("blocking_reasons")
                    or ["Review this thread's setup."]
                )
        ready = len(receipts) == len(configurations) and not blockers
        run_config = {"configurations": configurations}
        prepare_token: str | None = None
        if ready:
            prepare_token = secrets.token_urlsafe(32)
            with self.lock:
                self.ensure_can_start()
                while len(self.prepared_runs) >= self.max_prepare_tokens:
                    self.prepared_runs.pop(next(iter(self.prepared_runs)))
                self.prepared_runs[prepare_token] = {
                    "controller_session_id": self.session_id(),
                    "fingerprint": self.fingerprint(run_config),
                    "preparations": receipts,
                    "starting": False,
                }
        models = list(configurations[0].get("models") or [configurations[0]["model"]])
        prompt = f"Approve {len(configurations)} threads using {len(models)} Codex model(s)?"
        return {
            "controller_session_id": self.session_id(),
            "status": "ready_for_approval" if ready else "blocked",
            "ready": ready,
            "can_run": ready,
            "models": models,
            "model": models[0],
            "preparations": preparations,
            "blocking_reasons": blockers,
            "blockers": blockers,
            "prepare_token": prepare_token,
            "approval_prompt": prompt if ready else None,
            "approval": {"required": True, "prepare_token": prepare_token, "prompt": prompt},
            "run_config": run_config,
        }

    def _batch_response(self, batch: Mapping[str, Any], *, idempotent: bool) -> dict[str, Any]:
        states = {
            (state.get("thread_id"), state.get("model")): state
            for state in self.recent_runs(limit=self.max_threads * self.max_models)
            if state.get("batch_id") == batch["id"] and state.get("launch_failed") is not True
        }
        ordered = []
        for thread_id in batch["thread_ids"]:
            for model in batch["models"]:
                state = states.get((thread_id, model))
                if state is not None:
                    ordered.append(state)
        response = (
            self.started_runs_response(
                ordered, batch["models"], errors=batch["errors"], idempotent=idempotent
            )
            if ordered
            else {
                "runs": [],
                "errors": batch["errors"],
                "models": batch["models"],
                "idempotent": idempotent,
            }
        )
        response["batch"] = self._batch_summary()
        response["batch_id"] = batch["id"]
        return response

    def _start_batch(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("approved") is not True:
            raise ControllerError("Explicit approval is required before starting a replay.")
        token = arguments.get("prepare_token")
        if not isinstance(token, str) or len(token) < 32:
            raise ControllerError("Prepare and approve this exact configuration before starting.")
        configurations = self._batch_configurations(arguments)
        fingerprint = self.fingerprint({"configurations": configurations})
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        models = list(configurations[0].get("models") or [configurations[0]["model"]])
        with self.lock:
            receipt = self.prepared_runs.get(token)
            if self._batch_path().is_file():
                batch = self.read_json(self._batch_path(), maximum=self.max_record_bytes)
                if batch["prepare_token_hash"] != token_hash or batch["fingerprint"] != fingerprint:
                    raise ControllerError(
                        "The approved configuration changed. Open a new controller."
                    )
                if batch["starting"]:
                    if self.pid_is_alive(batch["controller_pid"]):
                        raise ControllerError(
                            "This approved batch is already being started. Retry shortly."
                        )
                    # An interrupted start may leave durable child runs. Account for the rest,
                    # but never launch new work after a controller restart.
                    self._recover_interrupted_batch(batch)
                return self._batch_response(batch, idempotent=True)
            if receipt is None or receipt.get("controller_session_id") != self.session_id():
                raise ControllerError(
                    "The prepare token is missing or expired. Prepare the batch again."
                )
            if receipt["fingerprint"] != fingerprint:
                raise ControllerError(
                    "The approved configuration changed. Prepare and approve it again."
                )
            for configuration, prepared in zip(configurations, receipt["preparations"]):
                if prepared["fingerprint"] != self.fingerprint(
                    _normalized_configuration(configuration)
                ):
                    raise ControllerError(
                        "The prepared thread configuration changed. Prepare the batch again."
                    )
            self.ensure_can_start()
            primary = configurations[0]
            self.update_attempt(
                start_requested=True,
                start_requested_at=self.now(),
                start_request=primary,
                models=[{"model": model, "launch_status": "pending"} for model in models],
                final_results_ready=False,
                final_results_ready_at=None,
            )
            batch = {
                "id": secrets.token_hex(16),
                "controller_pid": os.getpid(),
                "controller_session_id": self.session_id(),
                "prepare_token_hash": token_hash,
                "fingerprint": fingerprint,
                "thread_count": len(configurations),
                "thread_ids": [configuration["thread_id"] for configuration in configurations],
                "threads": [
                    {key: configuration[key] for key in ("thread_id", "thread_title")}
                    for configuration in configurations
                ],
                "models": models,
                "errors": [],
                "starting": True,
            }
            self.write_json(self._batch_path(), batch)
            receipt["starting"] = True

        def launch(index: int, model: str) -> dict[str, Any]:
            configuration = configurations[index]
            prepared = receipt["preparations"][index]
            return self.start_model(
                configuration,
                model,
                prepare_token=token,
                fingerprint=self.fingerprint(configuration),
                historical_result_sha256=prepared["historical_result_sha256"],
                prepared_configuration_sha256=prepared["prepared_configuration_sha256_by_model"][
                    model
                ],
                selected_models=models,
                batch_id=batch["id"],
                record_attempt=index == 0,
            )

        try:
            with ThreadPoolExecutor(max_workers=self.max_parallel_runs) as executor:
                futures = [
                    (index, model, executor.submit(launch, index, model))
                    for index in range(len(configurations))
                    for model in models
                ]
                for index, model, future in futures:
                    try:
                        future.result()
                    except Exception as error:  # noqa: BLE001 - account for every approved comparison.
                        failure = {
                            **batch["threads"][index],
                            "model": model,
                            "error": str(error)[:2_000],
                            "controller_code": "launch_failed",
                        }
                        if index == 0:
                            self.record_model_launch(
                                model,
                                launch_status="failed",
                                error=failure["error"],
                                controller_code="launch_failed",
                            )
                        with self.lock:
                            batch["errors"].append(failure)
                            self.write_json(self._batch_path(), batch)
        finally:
            with self.lock:
                batch["starting"] = False
                receipt["starting"] = False
                self.write_json(self._batch_path(), batch)
        return self._batch_response(batch, idempotent=False)
