# Local real-error E2E

From `oai-maintained-plugins`:

```sh
python3 plugins/codex-bakeoff/manual_tests/runtime_errors.py --run
```

No API key or LLM calls. Uses the supervisor-owned coordinator thread, packaged Node
worker, Codex SDK/CLI, durable state and attempt metadata, and numeric exporter.
It triggers worker validation failure,
a missing repository, and a real TCP reset from a local fault server. Only the final
host metrics destination is intercepted. The fault server uses an isolated temporary
Codex home and a synthetic credential; it does not modify your configuration.

The reporter requires `attempt.json`; state-only controllers are no longer supported.
Assertions check the attempt outcome, failure codes, actual exit code, three implementation retries,
observed duration, and worker stage. Messages and paths must not enter the metric
payload. The command refuses CI/CD environments and is outside normal test discovery.

Failures preserve `worker_code` and explicit `controller_code` values; the reporter
does not infer a broader `reason` from error text. Worker failures have controller
code `none`; missing-repository errors have `controller_error`. Older state without
a controller code exports `unknown`. Dashboard queries must no longer require the
removed `failure.reason` dimension.

The previous LLM sanitization and live-classifier matrix were removed: this patch
only implements information supported by the existing numeric telemetry contract.
