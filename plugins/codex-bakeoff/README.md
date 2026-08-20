# Codex Bakeoff

Codex Bakeoff compares an imported historical coding task with a fresh Codex
implementation from the same reviewed baseline. Its local browser controller
handles task selection, approval, isolated execution, blinded review, and
reporting. One Codex reviewer evaluates both the historical Claude
candidate and the Codex replay.

Choose one or more available Codex models. GPT-5.6 Sol, Terra, and Luna are
selected by default when available. Selected models run in parallel from the
same reviewed historical task and baseline, each in its own isolated workspace
with independent progress and results.

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

Each model receives the same original public task prompt and immutable upstream
Git baseline. Pass/fail is determined by executable, task-specific regression
checks rather than by a model-generated verdict or an inferred benchmark label.
Replay results report the blinded LLM judge's winner separately from the lower-
cost provider, so an equal-quality result does not hide a meaningful price gap.

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
`@Codex Bakeoff` in a fresh Codex task. In Codex Desktop, the controller opens in
the task's Codex in-app browser. In Codex CLI, it opens in the system browser.
The browser controller owns the rest of the workflow.

Each invocation starts an independent controller on an automatically available
loopback port, leaving existing listeners untouched. Multiple replay sessions can
run in parallel; each controller resumes only its own runs. Inactive controllers
exit automatically when they have no active runs.
