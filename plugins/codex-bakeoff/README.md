# Codex Bakeoff

Codex Bakeoff compares an imported historical coding task with a fresh Codex
implementation from the same reviewed baseline. Its local browser controller
handles task selection, approval, isolated execution, blinded review, and
reporting. One Codex reviewer evaluates both the historical Claude
candidate and the Codex replay.

A small host-managed MCP server launches the detached HTTP controller, which
supervises the model workflows. Four directly invoked metrics scripts report
independent milestones; none launches the controller.

Select one or more threads, then choose the Codex models to use for every
selected thread. GPT-5.6 Sol, Terra, and Luna are selected by default when
available. Each thread retains its own reviewed historical task and baseline;
each thread/model runs in its own isolated workspace with independent progress
and results. The controller queues work with up to eight active model runs.

For multiple threads, Results offers an aggregate for each Codex model and the
existing individual reports. Completed comparisons become available while
other threads continue. Totals sum paired, comparable task costs and show
coverage; missing costs, shared historical usage, and pending or failed runs
are excluded from both sides. Quality outcomes include completed comparisons
regardless of which candidate was preferred. These are API-equivalent task-cost
estimates, not subscription or invoice savings.

Whole-thread Replay is the default. When selecting one imported thread,
**Split into chunks** is also available:
place dividers between user turns, then configure every chunk before starting.
Every defined chunk is prepared before any starts, then their isolated workers
run using the existing configuration and report components, with up to eight
model runs in parallel. Remaining chunks start as running chunks finish.
Results are navigable by chunk, with a separate judge and
report for each. Mode, dividers, and per-chunk edits are saved across reloads.

Chunk boundaries determine the included turns. Git uses beginning/end commits
plus optional dirty files; Non-Git uses an empty beginning plus whole files
attributed to earlier chunks. Earlier files are inferred when inspecting a later
chunk. Dirty and Non-Git files use their current whole contents;
Replay does not reconstruct intermediate file versions.

Results remain available after completion or failure. To replay the same range
again or choose a different thread after running, open a new Codex task and
invoke Codex Bakeoff.

Recorded sample threads can also be generated from real Claude Code runs against
two public `jq` benchmark tasks. The recorder runs Claude Fable 5, Opus 5,
Sonnet 5, and Haiku 4.5 in isolated worktrees, preserves their authentic
transcripts and patches, runs objective behavioral checks, and records the
actual elapsed time, token usage, and Claude Code-reported API-equivalent cost.
After recordings are packaged, they appear in a separate **Sample data** view.
The thread picker defaults to real imported conversations when available and
otherwise opens the sample view. The original upstream Git commit and recorded
patch are reconstructed only when a sample is selected. No competitor
credentials are needed to replay an already-recorded sample.

Bundled samples use versioned, precomputed configurations for the exact prompt,
repository-relative working directory, beginning commit, recorded ending patch,
attributed files, and sample-side capability requirements. Inspection and execution
share the same resolver, including minimal `prepare_run` requests that supply only
the sample ID and model. Sample setup does not reconstruct prompts or infer working
directories with a model, and conflicting client overrides are rejected.
Artifact digests and the materialized Git tree are checked locally. Capability
availability is still checked on the current host, and execution still requires
preparation and approval. Upstream repository fetching is unchanged; no repository
snapshot or machine-specific ending commit is packaged.

The recorder precomputes configuration when packing. To rebuild configuration for
already-packaged recordings after reviewing their provenance:

```bash
python3 scripts/precompute_sample_configurations.py assets/claude-code-samples/index.json
```

Each model receives the same original public task prompt and immutable upstream
Git baseline. Pass/fail is determined by executable, task-specific regression
checks rather than by a model-generated verdict or an inferred benchmark label.
Replay results report the blinded LLM judge's winner separately from the lower-
cost provider, so an equal-quality result does not hide a meaningful price gap.

API-equivalent token costs use the standard pricing tables published directly by
[OpenAI](https://developers.openai.com/api/docs/pricing.md) and
[Anthropic](https://platform.claude.com/docs/en/about-claude/pricing.md), including
cache rates and OpenAI's long-context tier. Lookups are cached hourly within each
process. Bundled rates provide a fallback when a provider page is unavailable,
its table cannot be parsed, or a model is absent, including historical Claude 3
rates from Anthropic's published rate cards. Saved reports retain the costs
calculated when they were generated; opening a report does not reprice it.

Run the recorder from a terminal that can reach Anthropic, with an explicitly
chosen per-run spending cap:

```bash
python3 scripts/record_claude_code_samples.py --max-budget-usd 10 --pack
```

All eight recordings run concurrently by default; add `--jobs 4` to reduce the
number of simultaneous Claude sessions. Completed recordings are reused
automatically. The cap applies independently to each new run, so eight sessions
with `--max-budget-usd 10` can consume up to `$80` of API-equivalent usage.
Claude Code's reported cost is an estimate, not an observed subscription charge;
its model breakdown also preserves any genuine background helper-model usage.
Fable runs can consume additional usage credits; review the selected cap before
starting. Keep raw transcripts outside the repository and review packaged
artifacts for sensitive material before sharing them.


## Use in Codex

Install Codex Bakeoff when it is available to your account, then invoke
`@Codex Bakeoff` in a fresh Codex task. In both Codex Desktop and Codex CLI,
Replay prefers the task's Codex in-app browser: first the native host-aware
opener, which preserves SSH localhost port forwarding, then the in-app Browser
skill. Both surfaces fall back to the system browser when neither supported
in-app opening path is available, including a listed Browser skill whose
required tool or in-app connection is unavailable. The packaged default prompt
leaves this browser choice to the skill. An explicit user request for in-app
opening is never replaced with a system browser, and a failed in-app page-opening
attempt does not trigger fallback.
The browser controller owns the rest of the workflow.
The reply always includes a clickable controller link so you can open it manually
if the browser does not appear.

The controller starts through the host-managed MCP server, not a sandboxed shell
command. No shell escalation is requested by the controller launch flow. MCP
availability and tool policy still apply; there is no shell-launch fallback.
Model implementations still use `workspace-write`, and reviewers still use
`read-only`, with their existing approval policy.

Each invocation starts an independent controller on an automatically available
loopback port, leaving existing listeners untouched. Multiple replay sessions can
run in parallel; each controller resumes only its own runs. Inactive controllers
exit automatically when they have no active runs.

## Plugin metrics

The invocation's metrics observers report one terminal replay group. For a
multi-thread batch this is the first selected thread's model group; split-thread
reporting keeps its existing single-group scope. They do not aggregate multiple
threads or chunks. Every result remains in its individual
run and comparison report; the browser's all-thread summary has broader coverage
than this existing telemetry contract.

Failures include allowlisted worker codes and retryability on the existing `failure`
counter. `failure_detail` adds worker stage and observed native system code; unknown
codes become `unknown`. Numeric `failure_exit_code`, `failure_retry_count`, and
`failure_duration_seconds` are emitted only when observed. Exit codes may be negative
for signal termination. Retry count is the number of preceding implementation retries;
duration covers the last failed worker call, not the entire run or all retries.
SDK/CLI exceptions retain bounded, credential-redacted details in the local run
log, and the controller displays the final cause line. Error messages remain
local: no LLM classification or text submission is performed.
See [local real-error E2E checks](manual_tests/README.md).

`analytics.yaml` declares four operations using the supported `version: 1`
numeric-measurement contract; it does not schedule them.

The numbered script names describe expected return milestones, not a strict
process exit order. The probe and observers run independently. Analytics
operation and measurement names are unchanged by the filename numbering.

| Operation | Directly executed script | Measurements |
|---|---|---|
| `metrics_probe` | `scripts/metrics/1_probe_metrics.py` | `metrics_probe: 1`, without dimensions; `plugin_version` with `component` of `major`, `minor`, or `patch` |
| `controller_launch` | `scripts/metrics/2_start_controller.py` | `controller_launch: 1`, with `outcome` of `ready`, `startup_failure`, or `launch_unobserved` |
| `replay_start` | `scripts/metrics/3_report_start_metrics.py` | `run_start: 1`, `selected_model_count`, or `run_start_timeout: 1` |
| `replay_metrics` | `scripts/metrics/4_report_final_metrics.py` | Final attempt outcome, `final_prestart_timeout: 1` when no start arrives in 10 minutes, detailed model measurements, and the shared Claude baseline once |

The skill generates one shared `controller_session_id`, then dispatches scripts
1 through 4 and the Codex executable lookup concurrently in separate
execution-tool calls. It calls MCP with that same ID after the executable lookup,
without waiting for observer milestones. Hosts without parallel tool calls
dispatch sequentially without waiting for running sessions to exit.
The probe writes its reporting-path check and the packaged `plugin.json` version
as three numeric `plugin_version` rows (for example, `1.0.99` becomes major `1`,
minor `0`, and patch `99`); prerelease and build suffixes are not exported.
It starts no controller, reads no Replay state, and creates no controller or run
artifacts. Without a host sidecar it reports
`analytics_unavailable` and exits without writing. A probe error never blocks
Replay launch. A `written` acknowledgment is not ingestion proof; verify the
`metrics_probe` operation/measurement downstream. Keep probe counts separate
from controller launches and comparison outcomes.

Each script owns its stage-specific execution and reporting logic. Only common
state-reading, argument validation, and trusted-sidecar primitives live in
`scripts/metrics/replay_metrics_common.py`; early reporters do not load the final reporter.

The startup observer (`2_start_controller.py`, retaining its declared path)
receives the shared session ID and waits for MCP-owned controller state. It
never launches processes or writes lifecycle state.

The MCP `open_controller` tool accepts that ID, persists the launch attempt,
starts the detached controller, records readiness or failure, and returns its
URL. Repeating an ID never resets state or starts a second controller. The
startup observer writes one launch metric and exits. If startup cannot be
observed within 60 seconds, or its state is unreadable, it reports
`launch_unobserved`, not an invented success or confirmed startup failure.

The start and final observers begin before MCP creates `attempt.json`.
Each waits for that durable state, uses a separate execution-tool call and
host-created sidecar, and does not block the browser handoff. Codex checks only
initial observer startup and browser handoff before ending its turn, without
waiting for configuration, Go, or results. Missing analytics makes reporters
exit without blocking Replay.

Observers read durable controller state, write once at their milestone, and exit
independently. They are not spawned by the server. Model workflows still run as
background threads in the server; Codex workers and engine subprocesses remain
independent and do not inherit a metrics sidecar. The start observer treats the
durable `start_requested: true` transition as the entire run-start milestone;
model metadata can add `selected_model_count` but cannot suppress `run_start`.
After every selected model has durable terminal artifacts, the controller writes one
`final_results_ready: true` receipt to `attempt.json`. The final observer waits
for that receipt, then reads the detailed run artifacts once and exits. The
start and final observers report a pre-start timeout after 10 minutes without
Go. After Go, the final observation deadline begins at persisted
`start_requested_at`, not controller launch; expiration leaves models unresolved,
not failed.

Graceful controller shutdown records a durable receipt in `attempt.json` after
stopping its jobs. Start/final observers use that receipt to finish even while
the controller PID still appears alive; without it, existing PID checks apply.
Already-observed starts and completed results retain their normal reporting.

Final reporting retains cancellations and all observed detailed measurements.
For example, the final numeric envelope includes:

```json
{
  "version": 1,
  "measurements": [
    {
      "name": "replay",
      "value": 1,
      "dimensions": {
        "model_slot": "one",
        "codex_model": "sol",
        "claude_model": "sonnet",
        "source": "sample",
        "status": "completed"
      }
    },
    {
      "name": "codex_input_tokens",
      "value": 12480,
      "dimensions": {
        "model_slot": "one",
        "codex_model": "sol",
        "claude_model": "sonnet",
        "source": "sample"
      }
    },
    {
      "name": "outcome",
      "value": 1,
      "dimensions": {
        "model_slot": "one",
        "codex_model": "sol",
        "claude_model": "sonnet",
        "source": "sample",
        "winner": "codex"
      }
    },
    {
      "name": "codex_dimension_score",
      "value": 0.85,
      "dimensions": {
        "model_slot": "one",
        "codex_model": "sol",
        "claude_model": "sonnet",
        "source": "sample",
        "score_dimension": "code_quality"
      }
    }
  ]
}
```

`replay`, `outcome`, `dimension_outcome`, and `failure` are counters with
`value: 1`. Other measurements contain each provider's observed input, output,
and cached-input token counts; estimated costs; and elapsed or wall-clock
durations. Codex measurements also report implementation and review phase
durations. Shared Claude usage and timing are emitted once per replay group,
while Codex values are attributed to each selected model.

Approved judge measurements include overall scores and all six
evaluation-dimension scores and outcomes for both imported and sample runs by default.
Individual rubric-check scores are not exported.
Evaluator metadata uses bounded model-family and provider dimensions and records
whether a model judged itself or required normalization. `model_slot`
distinguishes parallel runs of the same model family because the host
deduplicates rows by measurement name and dimensions.

The host accepts at most 100 measurement rows and 64 KiB per execution. The full
local numeric envelope is retained in `metrics-full.json`. When it exceeds the
host budget, the bounded host payload reserves summary rows, records the omitted
row count in `measurements_omitted`, and marks `attempt.reporting_complete: no`.
An incomplete observation, such as timeout or unreadable state, also sets this
dimension to `no`; the attempt outcome still describes the observed replay result.
Complete details for eight-model comparisons do not fit the current host limit.
Full-data rollout remains blocked on a coordinated host/transport limit increase;
the local full copy does not make omitted rows available downstream.
The final observation deadline limits waiting: a delayed observer still includes
already-durable results.

Every numeric field is finite; observed failure exit codes may be negative.
Claude request time and recorded wall-clock time have distinct timing-basis
dimensions. Missing usage, costs,
timing, or comparison evidence remains absent instead of becoming zero. Only
allowlisted numeric judge scores and bounded outcome dimensions
are exported. No prompts, responses, patches, repository paths, customer
identifiers, or other sensitive or free-text content are exported.

The host collects each directly invoked reporter's sidecar after it exits
successfully. Reporters also preserve local copies under
`~/.cache/codex-bakeoff/controllers/<controller-session-id>/` as
`metrics-launch.json`, `metrics-start.json`, and `metrics.json` (the final
bounded host payload). A reporter ending without its
milestone can preserve an empty `measurements` array. `metrics-full.json` preserves
the final unbounded numeric envelope. Local preservation is best-effort: a
filesystem error is reported on stderr and may leave a copy unavailable. No
metrics copies are written when the reporter has no host sidecar. Local files and
successful reporter exits are not delivery acknowledgments; installed-plugin host
collection and downstream ingestion require separate verification.

The final `attempt` counter reports one handled observation outcome, including
all-model launch failure, mixed results, cancellation, unreadable state, and
observation timeout. Existing `replay` rows account for terminal models, including
artifact-less launch failures as failed models; `attempt_model` adds unresolved
or unreadable models without counting terminal models twice. A 10-minute
pre-start timeout is reported separately. The local
`attempt.json` stores the approved start request and exact run locations; it is
diagnostic state, not a restart/resume instruction. Final reporting requires this
file; missing or invalid attempt metadata produces `unreadable_state` without
falling back to older state-only controllers.

Each stage has a different host `execution_id`. Correlate stages by the host's
`thread_id`, original launch `turn_id`, and plugin identity; the operation
identifies the stage. Do not join by `execution_id` or a custom controller-ID
dimension. The skill requires
one controller per launch turn; this is not enforced against manual invocations.
Count launches, starts, final attempts, and terminal models separately.
Downstream analytics must adopt this staged contract before rollout; the presence
of correlation fields on legacy metrics does not prove staged collection.

A reporter exiting does not stop the server or its workers. A server crash
interrupts supervision; unfinished models remain interrupted or unresolved and
are never automatically rerun. Host shutdown, reporter failure, disabled
analytics, and delivery loss remain outside plugin-only guarantees. No automatic
retry follows an ambiguous submission. This is checkpointed reporting, not
streaming or crash-proof delivery.
