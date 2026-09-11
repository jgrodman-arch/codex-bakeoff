---
name: codex-bakeoff
description: Open an independent Codex Bakeoff controller in the Codex in-app browser, or the system browser when no in-app opening path is available. Use whenever this plugin is invoked or the user asks to browse, configure, run, or review a replay. The browser controller owns the entire workflow; do not conduct it in chat.
---

# Codex Bakeoff

Resolve the installed plugin root from this skill's location (two directories
above the directory containing `SKILL.md`). Before the first observer batch,
run this once as its own direct unified-exec call and retain its output as
`controller_session_id`:

```text
python3 -c 'import secrets; print(secrets.token_hex(16))'
```

Validate that the output contains exactly 32 lowercase hexadecimal characters.
Use that same ID for every observer and for MCP; do not let an observer or the
controller allocate a replacement.

Dispatch the following in one parallel batch, each as its own direct unified-exec
call. Use `yield_time_ms: 250` for script 1 and `yield_time_ms: 1000` for
scripts 2 through 4. Also run `command -v codex` in its own call and retain an
absolute result for MCP. If parallel calls are unavailable, dispatch sequentially
without waiting for long-lived observers to exit.

```text
python3 <absolute-plugin-root>/scripts/metrics/1_probe_metrics.py
python3 <absolute-plugin-root>/scripts/metrics/2_start_controller.py --controller-session-id <controller-session-id>
python3 <absolute-plugin-root>/scripts/metrics/3_report_start_metrics.py --controller-session-id <controller-session-id>
python3 <absolute-plugin-root>/scripts/metrics/4_report_final_metrics.py --controller-session-id <controller-session-id>
```

Script 1 exits. Scripts 2 through 4 are observers; do not wait for them to exit.

Do not combine scripts, use shell backgrounding, retry observers, or set
`CODEX_PLUGIN_METRICS_OUTPUT` yourself. Each script needs its own host-created
sidecar. Missing analytics or an observer error never blocks controller launch or
browser use; local writes are not delivery acknowledgments.

After the executable lookup finishes, call
`mcp__codex_bakeoff.open_controller` with the shared `controller_session_id`
and the absolute `codex_cli_path` when one was found. Use the MCP tool, not a
shell launcher. If launch fails, report the limitation without inventing a new
ID, changing permissions, or stopping an existing process.

On success, immediately open the exact plain `launch_url`; do not add tokens.
`opened: false` means the controller is prepared, not that a browser opened.
Choose the opening path by available in-app browser capabilities:

1. If native `open_in_codex` is available, immediately call it directly with
   `{ target: { type: "browser", url: launch_url } }`.
   Do not provide `threadId`; the browser belongs in the invoking Codex task.
   Prefer this host-aware path because it preserves localhost port forwarding
   for controllers running in remote SSH workspaces.
2. Otherwise, if `browser:control-in-app-browser` is listed, read and follow that
   skill to connect to the invoking task's in-app browser. Use its explicit
   in-app selector, never a default, URL-selected, Chrome, or extension browser.
   A listed skill alone is not proof of a usable connection: if its required
   tool or in-app connection is unavailable before navigation, treat this path
   as unavailable and continue to step 3. Once connected, open `launch_url`,
   make the browser visible, and mark the controller tab as a deliverable so
   it stays open after the turn. Verify that the controller page loaded before
   reporting success. A missing `open_in_codex` tool does not mean the in-app
   browser is unavailable.
3. In both Codex Desktop and Codex CLI, when neither in-app path is available and
   the user has not explicitly requested the in-app browser, open the system
   browser with `open "$launch_url"` on macOS or `xdg-open "$launch_url"` on Linux
   and verify the command exits successfully.

If the native call exists but fails, or opening through a connected in-app
Browser fails, report that the controller could not be opened and provide the
clickable link described below; do not fall back to an external browser.
If the user explicitly requested an unavailable in-app browser, report that
limitation and provide the link without opening a system browser.

A native response with `status: "queued"` is a successful handoff, not proof that
the browser tab appeared. Do not retry or switch browsers after a queued response.

After the browser handoff and observer startup checks, give a brief status and
always include a clickable Markdown link labeled `Open Codex Bakeoff`, with the
exact returned `launch_url` as its target, then stop. Include the link even when
opening reports success, so the user can recover if the browser does not appear.
For a queued handoff, say that the controller is ready, not that the browser
opened. If the chosen browser cannot be opened, report that the controller could
not be opened automatically, provide the same link, and leave the observers
running.
If controller preparation failed without returning a `launch_url`, report the
failure without inventing a link.

Do not perform Replay workflow steps in chat; the browser controller owns them.

If the installed MCP tool or required metrics scripts are unavailable, ask the
user to start a new task after reinstalling or enabling the plugin.
