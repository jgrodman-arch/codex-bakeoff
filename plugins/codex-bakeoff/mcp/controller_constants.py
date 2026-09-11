"""Static controller contracts kept separate from the large server module."""

from __future__ import annotations

import re

REQUEST_SYNTHESIS_MODEL = "gpt-5.6-terra"
DEFAULT_IMPLEMENTATION_MODEL = "gpt-5.6-sol"
REQUEST_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
    "additionalProperties": False,
}
WORKING_DIRECTORY_SCHEMA = {
    "type": "object",
    "properties": {"working_directory": {"type": "string"}},
    "required": ["working_directory"],
    "additionalProperties": False,
}
RUN_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
COMMIT_PATTERN = re.compile(r"\A[0-9a-fA-F]{7,64}\Z")
HTTP_TOOL_NAMES = frozenset(
    {
        "get_state",
        "list_threads",
        "inspect_thread",
        "infer_working_directory",
        "synthesize_request",
        "prepare_run",
        "start_run",
        "cancel_run",
        "get_run",
        "get_report",
    }
)
PHASES = (
    ("preparing", "Preparing configuration"),
    ("creating_workspace", "Creating isolated workspace"),
    ("implementing", "Implementing with Codex"),
    ("collecting", "Capturing result"),
    ("reviewing", "Running blind review"),
    ("reporting", "Finalizing report"),
)
