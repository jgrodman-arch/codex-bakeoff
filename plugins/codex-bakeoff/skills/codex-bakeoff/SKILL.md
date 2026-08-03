---
name: codex-bakeoff
description: Open the external-browser Codex Bakeoff controller. Use whenever this plugin is invoked or the user asks to browse, configure, run, or review a bakeoff. The controller owns the entire workflow; do not conduct it in chat.
---

# Codex Bakeoff

Run `command -v codex` only to resolve the invoking task's Codex executable. If it
returns an absolute path, immediately call `mcp__codex_bakeoff.open_controller`
with that path as `codex_cli_path`; otherwise call it with no arguments.

Do not use the terminal for anything else, run the Python CLI, ask workflow questions, summarize choices, or perform any bakeoff step in chat. The controller owns thread selection, configuration, approval, execution, progress, and results.

If `open_controller` reports `requires_confirmation`, tell the user which process and PID occupy the controller port and ask whether to stop that exact process. Pause until the user explicitly confirms. Then call `mcp__codex_bakeoff.stop_port_process_and_open_controller` with `confirmed: true` and the exact `confirmation_token` returned by `open_controller`. Do not stop a process without that confirmation.

After the tool confirms the external browser opened, stop. Do not add a text walkthrough or duplicate the controller state in chat.

If the tool is unavailable, say only that the controller could not be opened and ask the user to start a new task after reinstalling or enabling the plugin.
