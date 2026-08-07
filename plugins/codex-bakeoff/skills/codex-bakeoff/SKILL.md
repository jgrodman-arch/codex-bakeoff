---
name: codex-bakeoff
description: Open an independent Codex Bakeoff controller in the Codex in-app browser, falling back to an external browser when unavailable. Use whenever this plugin is invoked or the user asks to browse, configure, run, or review a replay. The browser controller owns the entire workflow; do not conduct it in chat.
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
controller is prepared, but the Codex browser has not opened yet. Immediately call native
`open_in_codex` directly with
`{ target: { type: "browser", url: launch_url } }`. Do not provide `threadId`;
the browser belongs in the invoking Codex task.

Say the controller opened in the in-app browser only after the native call
explicitly succeeds, such as by returning a browser tab ID, then stop. If
`open_in_codex` is unavailable or the native call fails, open the same
`launch_url` in the system's external browser: run `open "$launch_url"` on
macOS, `xdg-open "$launch_url"` on Linux, or `Start-Process "$launch_url"` on
Windows. Say the controller opened in an external browser only after that
command succeeds, then stop. If controller preparation or both browser-opening
attempts fail, report that the controller could not be opened.

Apart from resolving the Codex executable and the external-browser fallback,
do not use the terminal, run the Python CLI, ask workflow questions, summarize
choices, or perform any replay step in chat. The browser
controller owns thread selection, configuration, approval, execution, progress,
and results. Do not add a text walkthrough or duplicate controller state in chat.

If the plugin tool is unavailable, ask the user to start a new task after
reinstalling or enabling the plugin.
