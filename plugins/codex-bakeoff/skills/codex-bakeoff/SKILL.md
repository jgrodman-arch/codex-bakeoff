---
name: codex-bakeoff
description: Open an independent Codex Bakeoff controller in the Codex in-app browser or, in Codex CLI, the system browser. Use whenever this plugin is invoked or the user asks to browse, configure, run, or review a replay. The browser controller owns the entire workflow; do not conduct it in chat.
---

# Codex Bakeoff

Run `command -v codex` only to resolve the invoking task's Codex executable. If it
returns an absolute path, immediately call `mcp__codex_bakeoff.open_controller`
with that path as `codex_cli_path`; otherwise call it with no arguments.

The tool selects an available loopback port and starts a fresh, independent
controller for this invocation. An occupied port is skipped. Never stop an
existing process or ask the user to resolve a port conflict. Multiple replay
sessions can run in parallel.

A successful controller preparation returns `prepared: true`, `opened: false`,
and the local controller `launch_url`. The URL is a plain loopback URL; do not add
session tokens or an authentication step. `opened: false` is expected: the
controller is prepared, but a browser has not opened yet. If native
`open_in_codex` is available, immediately call it directly with
`{ target: { type: "browser", url: launch_url } }`. Do not provide `threadId`;
the browser belongs in the invoking Codex task. If `open_in_codex` is unavailable,
as in Codex CLI, open the system browser with `open "$launch_url"` on macOS or
`xdg-open "$launch_url"` on Linux and verify the command exits successfully.
If the native call exists but fails, report that the controller could not be
opened; do not fall back to an external browser.

After the native call explicitly succeeds, such as by returning a browser tab
ID, or the system-browser command exits successfully, briefly confirm that the
controller opened in the in-app or system browser, then stop. If the chosen
browser cannot be opened, report that the controller could not be opened.

Apart from resolving the Codex executable and opening the system browser when
`open_in_codex` is unavailable, do not use the terminal, run the replay Python CLI, ask workflow
questions, summarize choices, or perform any replay step in chat. The
browser controller owns thread selection, configuration, approval, execution,
progress, and results. Do not add a text walkthrough or duplicate controller
state in chat.

If the plugin tool is unavailable, ask the user to start a new task after
reinstalling or enabling the plugin.
