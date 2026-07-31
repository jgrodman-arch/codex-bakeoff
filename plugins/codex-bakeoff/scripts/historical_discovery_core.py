#!/usr/bin/env python3
# ruff: noqa: F401, F821
"""Read-only discovery and provenance for imported historical Claude tasks.

The external-agent import ledger is the exclusive authority for which Claude
sessions have actually been imported. Transcript contents are streamed; hidden
thinking and post-task Claude answers never enter a Codex replay specification.
"""

from __future__ import annotations

import ctypes
import difflib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
from collections import Counter, deque
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_IMPORT_LEDGER = Path.home() / ".codex" / "external_agent_session_imports.json"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
CACHE_CREATION_FIELDS = (
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
)
NATIVE_TOOLS = {
    "Read": "Codex file reading",
    "Write": "Codex workspace file editing",
    "Edit": "Codex workspace file editing",
    "MultiEdit": "Codex workspace file editing",
    "Glob": "Codex file discovery",
    "Grep": "Codex content search",
    "Bash": "Codex sandboxed shell",
    "Skill": "Codex skill invocation",
    "AskUserQuestion": "Codex user-input requests",
}
NATIVE_SKILL_EQUIVALENTS = {
    "docx": "documents",
    "xlsx": "spreadsheets",
    "pptx": "presentations",
    "pdf": "pdf",
    "skill-creator": "skill-creator",
    "skillcreator": "skill-creator",
}
NATIVE_CONNECTOR_EQUIVALENTS = {
    "claudebrowser": "chrome-internal:control-chrome",
    "claude_browser": "chrome-internal:control-chrome",
    "claudeinchrome": "chrome-internal:control-chrome",
}
OFFICIAL_CODEX_MARKETPLACES = frozenset(
    {"openai-bundled", "openai-curated", "openai-primary-runtime"}
)
REQUIRED_OFFICIAL_CODEX_MARKETPLACES = frozenset({"openai-curated"})
OFFICIAL_INSTALL_MARKETPLACES = {
    "openai-bundled": frozenset({"openai-bundled"}),
    "openai-curated": frozenset({"openai-curated", "openai-curated-remote"}),
    "openai-primary-runtime": frozenset({"openai-primary-runtime"}),
}
ANTHROPIC_FIRST_PARTY_SKILLS = frozenset(
    {
        "algorithmic-art",
        "artifact-design",
        "brand-guidelines",
        "canvas-design",
        "doc-coauthoring",
        "docx",
        "frontend-design",
        "internal-comms",
        "mcp-builder",
        "pdf",
        "pptx",
        "skill-creator",
        "slack-gif-creator",
        "theme-factory",
        "web-artifacts-builder",
        "webapp-testing",
        "xlsx",
    }
)
ANTHROPIC_FIRST_PARTY_NAMESPACES = frozenset(
    {
        "anthropic",
        "anthropic-skills",
        "claude",
        "claude-code",
        "claude-plugins-official",
    }
)
SKILL_RESOLUTION_POLICY = "resolve_all_observed_skills"
CAPABILITY_SNAPSHOT_SCHEMA_VERSION = 2
MAX_CAPABILITY_SNAPSHOT_ENTRIES = 256
MAX_CAPABILITY_SNAPSHOT_FILES = 20_000
MAX_CAPABILITY_SNAPSHOT_NODES = 40_000
MAX_CAPABILITY_SNAPSHOT_TOTAL_NODES = 80_000
MAX_CAPABILITY_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_CAPABILITY_SNAPSHOT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_CAPABILITY_SNAPSHOT_XATTRS = 20_000
MAX_CAPABILITY_SNAPSHOT_TOTAL_XATTRS = 40_000
MAX_CAPABILITY_SNAPSHOT_XATTR_BYTES = 16 * 1024 * 1024
MAX_CAPABILITY_SNAPSHOT_TOTAL_XATTR_BYTES = 32 * 1024 * 1024
CAPABILITY_FINGERPRINT_FIELDS = (
    "tree_sha256",
    "node_count",
    "directory_count",
    "file_count",
    "byte_count",
    "xattr_count",
    "xattr_bytes",
)
CAPABILITY_COMPONENT_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
ATTRIBUTION_SKILL_PATTERN = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,127}"
    r"(?:[:/][A-Za-z0-9][A-Za-z0-9._+-]{0,127})?\Z"
)
ATTRIBUTION_PLUGIN_PATTERN = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,127}" r"(?:@[A-Za-z0-9][A-Za-z0-9._+-]{0,127})?\Z"
)
ATTRIBUTION_MCP_SERVER_PATTERN = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,127}" r"(?: [A-Za-z0-9][A-Za-z0-9._+-]{0,127})*\Z"
)
ATTRIBUTION_MCP_TOOL_PATTERN = re.compile(
    r"\Amcp__(?P<server>[A-Za-z0-9](?:[A-Za-z0-9.+-]|_(?!_)){0,127})"
    r"__(?P<tool>[A-Za-z0-9](?:[A-Za-z0-9.+-]|_(?!_)){0,127})\Z"
)
MAX_ATTRIBUTION_IDENTITY_LENGTH = 256
CAPABILITY_SECRET_DIRECTORIES = frozenset(
    {
        ".aws",
        ".azure",
        ".gnupg",
        ".kube",
        ".ssh",
        "credentials",
        "secrets",
        "tokens",
    }
)
CAPABILITY_SECRET_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".envrc",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "token.json",
        "tokens.json",
    }
)
CAPABILITY_SECRET_FILE_PATTERN = re.compile(
    r"\A(?:access[-_]?token|api[-_]?key|credentials?|private[-_]?key|"
    r"refresh[-_]?token|secrets?|tokens?)(?:\..+)?\Z",
    re.I,
)
SECRET_PATTERNS = (
    (
        re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})"),
        "[REDACTED]",
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"password|secret|authorization)\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
)
SYSTEM_PROMPT_PREFIXES = (
    "<task-notification>",
    "<command-name>",
    "<local-command-",
    "<system-reminder>",
    "base directory for this skill:",
)
INTERRUPTION_PLACEHOLDER = re.compile(
    r"\A\s*\[\s*request\s+interrupted"
    r"(?:\s+by\s+(?:the\s+)?user)?"
    r"(?:\s+(?:for|during|by)\s+(?:a\s+)?tool(?:[\s-]+(?:use|call))?)?"
    r"\s*\.?\s*\]\s*\Z",
    re.IGNORECASE,
)
CLAUDE_PROJECT_SKILL_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])\.claude[\\/]skills[\\/]"
    r"(?P<name>[^\\/\s'\"`]+)(?:[\\/]SKILL\.md)?",
    re.IGNORECASE,
)


class HistoricalDiscoveryError(ValueError):
    """A historical session cannot be safely discovered or interpreted."""


class LedgerError(HistoricalDiscoveryError):
    """The authoritative external-agent import ledger is unavailable or invalid."""


class TranscriptError(HistoricalDiscoveryError):
    """A ledger-referenced Claude transcript is unavailable or unreadable."""


class TaskNotFoundError(HistoricalDiscoveryError):
    """The selected original Claude user-message UUID is not in its session."""


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1000 if abs(value) > 100_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact(value: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _compact(value: str, *, limit: int = 140) -> str:
    collapsed = " ".join(_redact(value).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError as error:
        raise TranscriptError(
            f"Cannot read the imported transcript {path}: {error.strerror or type(error).__name__}"
        ) from error


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _visible_message_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content if content.strip() else ""
    return "\n".join(
        block["text"]
        for block in _content_blocks(event)
        if block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    )


def _is_actionable_user_event(event: dict[str, Any]) -> bool:
    if event.get("type") != "user":
        return False
    if event.get("isMeta") or event.get("isSidechain") or event.get("isCompactSummary"):
        return False
    if event.get("sourceToolAssistantUUID") or event.get("sourceToolUseID"):
        return False
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") not in (None, "user"):
        return False
    uuid = event.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        return False
    text = _visible_message_text(event)
    if not text.strip():
        return False
    if INTERRUPTION_PLACEHOLDER.fullmatch(text):
        return False
    return not text.lstrip().casefold().startswith(SYSTEM_PROMPT_PREFIXES)


def _tool_blocks(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if event.get("type") != "assistant":
        return
    for block in _content_blocks(event):
        if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
            yield block


def _complexity_score(evidence: dict[str, int]) -> int:
    points = 0
    points += min(3, max(0, evidence["task_count"] - 1))
    points += min(3, evidence["tool_call_count"] // 5)
    points += min(2, evidence["changed_file_count"] // 2)
    points += min(2, max(0, evidence["project_root_count"] - 1))
    points += min(2, evidence["connector_count"])
    points += min(2, evidence["verification_count"])
    points += min(2, evidence["subagent_call_count"])
    points += 1 if evidence["elapsed_seconds"] >= 1800 else 0
    if points >= 7:
        return 4
    if points >= 4:
        return 3
    if points >= 2:
        return 2
    return 1


def _summarize_session(record: dict[str, Any], source_path: Path) -> dict[str, Any]:
    connector_names = _string_list(record.get("connector_names"))
    project_dirs: set[str] = set()
    changed_files: set[str] = set()
    observed_tools: Counter[str] = Counter()
    task_count = 0
    verification_count = 0
    subagent_count = 0
    created_at: datetime | None = None
    first_activity: datetime | None = None
    last_activity: datetime | None = None
    first_request = ""
    title = ""
    session_id = source_path.stem
    claude_model: str | None = None
    try:
        for event in _iter_events(source_path):
            event_session = event.get("sessionId", event.get("session_id"))
            if isinstance(event_session, str) and event_session.strip():
                session_id = event_session
            cwd = event.get("cwd")
            if isinstance(cwd, str) and cwd.strip() and not event.get("isSidechain"):
                project_dirs.add(cwd)
            if event.get("type") in ("custom-title", "ai-title", "summary"):
                for field in ("customTitle", "title", "aiTitle", "summary"):
                    candidate = event.get(field)
                    if isinstance(candidate, str) and candidate.strip():
                        title = _compact(candidate)
                        break
            is_task = _is_actionable_user_event(event)
            if is_task:
                task_count += 1
                if not first_request:
                    first_request = _visible_message_text(event)
                created = _parse_timestamp(event.get("timestamp"))
                if created is not None and (created_at is None or created < created_at):
                    created_at = created
            if event.get("type") == "assistant" or is_task:
                activity = _parse_timestamp(event.get("timestamp"))
                if activity is not None:
                    if first_activity is None or activity < first_activity:
                        first_activity = activity
                    if last_activity is None or activity > last_activity:
                        last_activity = activity
            if event.get("type") == "assistant":
                message = event.get("message")
                if isinstance(message, dict):
                    model = message.get("model")
                    if isinstance(model, str) and model.strip():
                        claude_model = model
            for block in _tool_blocks(event):
                name = block["name"]
                observed_tools[name] += 1
                arguments = block.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    for field in ("file_path", "path", "notebook_path"):
                        value = arguments.get(field)
                        if isinstance(value, str) and value.strip():
                            changed_files.add(value)
                if name in ("Task", "Agent", "dispatch_agent", "spawn_agent"):
                    subagent_count += 1
                if name in ("Bash", "shell", "exec_command"):
                    command = arguments.get("command", arguments.get("cmd", ""))
                    if isinstance(command, str) and re.search(
                        r"\b(?:pytest|unittest|vitest|jest|playwright|ruff|mypy|"
                        r"(?:npm|pnpm|yarn|cargo|go)\s+(?:test|check)|git\s+diff\s+--check)\b",
                        command,
                        re.IGNORECASE,
                    ):
                        verification_count += 1
    except TranscriptError:
        pass

    if last_activity is not None:
        activity_at = last_activity
        activity_source = "transcript_timestamp"
    else:
        activity_at = _parse_timestamp(record.get("source_modified_at"))
        if activity_at is not None:
            activity_source = "source_modified_at"
        else:
            try:
                activity_at = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                activity_at = None
            activity_source = "source_mtime" if activity_at is not None else "unavailable"

    elapsed = 0
    if first_activity is not None and last_activity is not None:
        elapsed = max(0, int((last_activity - first_activity).total_seconds()))
    evidence = {
        "task_count": task_count,
        "tool_call_count": sum(observed_tools.values()),
        "changed_file_count": len(changed_files),
        "project_root_count": len(project_dirs),
        "connector_count": len(connector_names),
        "verification_count": verification_count,
        "subagent_call_count": subagent_count,
        "elapsed_seconds": elapsed,
    }
    score = _complexity_score(evidence)
    project_dir = min(project_dirs) if project_dirs else None
    return {
        "session_id": session_id,
        "imported_thread_id": record["imported_thread_id"],
        "source_path": str(source_path),
        "title": title or _compact(first_request) or "Imported Claude session",
        "project_dir": project_dir,
        "project_dirs": sorted(project_dirs),
        "created_at": _format_timestamp(created_at),
        "creation_source": ("transcript_timestamp" if created_at is not None else "unavailable"),
        "activity_at": _format_timestamp(activity_at),
        "activity_source": activity_source,
        "complexity_score": score,
        "complexity_evidence": evidence,
        "task_count": task_count,
        "tool_call_count": evidence["tool_call_count"],
        "observed_tools": sorted(observed_tools),
        "connector_names": connector_names,
        "imported_at": record.get("imported_at"),
        "source_modified_at": record.get("source_modified_at"),
        "claude_model": claude_model,
    }


def list_imported_sessions(
    ledger_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return ledger-imported sessions, newest Claude creation time first."""

    path = Path(ledger_path).expanduser() if ledger_path is not None else DEFAULT_IMPORT_LEDGER
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LedgerError(
            f"Cannot read the external-agent import ledger {path}: {type(error).__name__}"
        ) from error

    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise LedgerError("The external-agent import ledger does not contain a records array.")

    sessions_by_source: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for ledger_index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        imported_thread = record.get("imported_thread_id")
        raw_source = record.get("source_path")
        if not isinstance(imported_thread, str) or not imported_thread.strip():
            continue
        if not isinstance(raw_source, str) or not raw_source.strip():
            continue
        source_path = Path(raw_source).expanduser()
        if not source_path.is_absolute():
            source_path = (path.parent / source_path).resolve()
        try:
            source_key = str(source_path.resolve())
        except (OSError, RuntimeError):
            source_key = str(source_path.absolute())
        sessions_by_source.setdefault(source_key, []).append(
            (ledger_index, _summarize_session(record, source_path))
        )

    sessions: list[dict[str, Any]] = []
    for candidates in sessions_by_source.values():
        _, selected = max(
            candidates,
            key=lambda candidate: (
                _sortable_number(candidate[1].get("imported_at")),
                _sortable_number(candidate[1].get("source_modified_at")),
                candidate[0],
            ),
        )
        sessions.append(selected)

    sessions.sort(
        key=lambda session: (
            _parse_timestamp(session.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            _parse_timestamp(session.get("activity_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            str(session.get("session_id", "")),
        ),
        reverse=True,
    )
    return sessions


def _sortable_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("-inf")
    if isinstance(value, float) and not math.isfinite(value):
        return float("-inf")
    return value


def list_session_tasks(session: dict[str, Any]) -> list[dict[str, Any]]:
    """List original actionable user requests, excluding tool results and metadata."""

    raw_path = session.get("source_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise TranscriptError("The imported session does not identify a source transcript.")
    source_path = Path(raw_path).expanduser()
    tasks: list[dict[str, Any]] = []
    for event in _iter_events(source_path):
        if not _is_actionable_user_event(event):
            continue
        tasks.append(
            {
                "message_uuid": event["uuid"],
                "request": _visible_message_text(event),
                "timestamp": _format_timestamp(_parse_timestamp(event.get("timestamp"))),
                "project_dir": (
                    event.get("cwd")
                    if isinstance(event.get("cwd"), str)
                    else session.get("project_dir")
                ),
                "session_id": session.get("session_id"),
                "imported_thread_id": session.get("imported_thread_id"),
                "task_index": len(tasks),
            }
        )
    return tasks


def build_thread_task(session: dict[str, Any]) -> dict[str, Any]:
    """Represent all actionable original user turns as one chronological task."""

    tasks = list_session_tasks(session)
    if not tasks:
        raise TaskNotFoundError(
            "The selected imported conversation contains no actionable user messages."
        )
    return {
        **tasks[0],
        "request": "\n\n".join(task["request"] for task in tasks),
        "task_scope": "whole_thread",
        "user_message_count": len(tasks),
        "message_uuids": [task["message_uuid"] for task in tasks],
    }


def _task_events(
    source_path: Path,
    message_uuid: str,
    *,
    whole_thread: bool = False,
) -> Iterator[dict[str, Any]]:
    started = False
    for event in _iter_events(source_path):
        if not started:
            if _is_actionable_user_event(event) and event.get("uuid") == message_uuid:
                started = True
                yield event
            continue
        if _is_actionable_user_event(event) and not whole_thread:
            break
        yield event
    if not started:
        raise TaskNotFoundError(
            "The selected original user-message UUID is not in the imported transcript."
        )


def _linked_subagent_paths(
    source_path: Path,
    message_uuid: str,
    *,
    whole_thread: bool = False,
) -> list[Path]:
    """Find the recursive closure of subagents launched by the selected task."""

    return [
        Path(source["source_path"])
        for source in _linked_subagent_sources(
            source_path,
            message_uuid,
            whole_thread=whole_thread,
        )
    ]


def _launched_subagents(events: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    pending_calls: dict[str, str] = {}
    launched: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        for block in _tool_blocks(event):
            if block["name"] not in ("Agent", "Task", "dispatch_agent", "spawn_agent"):
                continue
            block_id = block.get("id")
            if isinstance(block_id, str) and block_id:
                pending_calls[block_id] = block["name"]
        if event.get("type") != "user":
            continue
        result = event.get("toolUseResult")
        if not isinstance(result, dict):
            continue
        agent_id = result.get("agentId", result.get("agent_id"))
        if not isinstance(agent_id, str) or not agent_id:
            continue
        for block in _content_blocks(event):
            tool_use_id = block.get("tool_use_id")
            if (
                block.get("type") != "tool_result"
                or not isinstance(tool_use_id, str)
                or tool_use_id not in pending_calls
            ):
                continue
            launch_key = (agent_id, tool_use_id)
            if launch_key in seen:
                continue
            seen.add(launch_key)
            launched.append(
                {
                    "agent_id": agent_id,
                    "launch_tool_use_id": tool_use_id,
                    "launch_tool": pending_calls[tool_use_id],
                }
            )
    return launched


def _subagent_source_agent_id(source_path: Path) -> str | None:
    for event in _iter_events(source_path):
        for field in ("agentId", "agent_id"):
            agent_id = event.get(field)
            if isinstance(agent_id, str) and agent_id:
                return agent_id
    if source_path.stem.startswith("agent-") and len(source_path.stem) > len("agent-"):
        return source_path.stem[len("agent-") :]
    return None


def _linked_subagent_sources(
    source_path: Path,
    message_uuid: str,
    *,
    whole_thread: bool = False,
) -> list[dict[str, Any]]:
    """Return recursively linked subagent JSONL sources with launch provenance."""

    candidates_by_agent: dict[str, list[Path]] = {}

    def register_candidates(parent_path: Path) -> None:
        for candidate in _subagent_paths(parent_path):
            try:
                agent_id = _subagent_source_agent_id(candidate)
            except TranscriptError:
                continue
            if agent_id is None:
                continue
            candidates = candidates_by_agent.setdefault(agent_id, [])
            if candidate not in candidates:
                candidates.append(candidate)

    register_candidates(source_path)

    linked: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    sources: deque[tuple[Path, str | None, int, bool]] = deque([(source_path, None, 0, True)])
    while sources:
        parent_path, parent_agent_id, parent_depth, is_main = sources.popleft()
        register_candidates(parent_path)
        events = (
            _task_events(
                parent_path,
                message_uuid,
                whole_thread=whole_thread,
            )
            if is_main
            else _iter_events(parent_path)
        )
        for launch in _launched_subagents(events):
            candidates = candidates_by_agent.get(launch["agent_id"], [])
            if len(candidates) > 1:
                raise TranscriptError(
                    "Multiple Claude subagent transcripts claim the same linked agent ID."
                )
            if not candidates:
                raise TranscriptError(
                    "The selected Claude task launched subagent "
                    f"{launch['agent_id']!r} with {launch['launch_tool']} tool use "
                    f"{launch['launch_tool_use_id']!r} from {parent_path}, but its "
                    "JSONL transcript is unavailable. Capability inventory cannot "
                    "safely omit that subagent."
                )
            child_path = candidates[0]
            if child_path in seen_paths:
                continue
            seen_paths.add(child_path)
            linked.append(
                {
                    "source_path": str(child_path),
                    "agent_id": launch["agent_id"],
                    "parent_source_path": str(parent_path),
                    "parent_agent_id": parent_agent_id,
                    "launch_tool_use_id": launch["launch_tool_use_id"],
                    "launch_tool": launch["launch_tool"],
                    "depth": parent_depth + 1,
                }
            )
            sources.append((child_path, launch["agent_id"], parent_depth + 1, False))
    return linked


def _usage_key(event: dict[str, Any]) -> tuple[str, str] | None:
    message = event.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    for field, origin in (("id", "provider_message_id"),):
        value = message.get(field)
        if isinstance(value, str) and value:
            return origin, value
    for field, origin in (("requestId", "request_id"), ("uuid", "event_uuid")):
        value = event.get(field)
        if isinstance(value, str) and value:
            return origin, value
    return None


def _project_skill_dependencies(event: dict[str, Any]) -> set[str]:
    """Find project skills actually loaded through structured tool invocations."""

    dependencies: set[str] = set()
    for block in _tool_blocks(event):
        if block["name"] not in {"Read", "read_file"}:
            continue
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            continue
        for field in ("file_path", "path"):
            path = arguments.get(field)
            if not isinstance(path, str):
                continue
            match = CLAUDE_PROJECT_SKILL_PATH.search(path)
            if match is not None:
                dependencies.add(match.group("name"))
    return dependencies


def _validated_attribution_identity(
    value: object,
    pattern: re.Pattern[str],
) -> str | None:
    """Return an exact structured attribution identity, never a fuzzy rewrite."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ATTRIBUTION_IDENTITY_LENGTH
        or pattern.fullmatch(value) is None
    ):
        return None
    return value


def _event_capability_attribution(
    event: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract independently validated skill, plugin, and MCP attribution."""

    if event.get("type") != "assistant":
        return None, None, None, None

    skill = _validated_attribution_identity(
        event.get("attributionSkill"),
        ATTRIBUTION_SKILL_PATTERN,
    )
    plugin = _validated_attribution_identity(
        event.get("attributionPlugin"),
        ATTRIBUTION_PLUGIN_PATTERN,
    )
    server = _validated_attribution_identity(
        event.get("attributionMcpServer"),
        ATTRIBUTION_MCP_SERVER_PATTERN,
    )

    raw_tool = event.get("attributionMcpTool")
    tool_server: str | None = None
    tool_name: str | None = None
    tool_is_valid = raw_tool is None
    if isinstance(raw_tool, str):
        full_tool = (
            ATTRIBUTION_MCP_TOOL_PATTERN.fullmatch(raw_tool)
            if len(raw_tool) <= MAX_ATTRIBUTION_IDENTITY_LENGTH
            else None
        )
        if full_tool is not None:
            tool_server = full_tool.group("server")
            tool_name = full_tool.group("tool")
            tool_is_valid = True
        elif not raw_tool.startswith("mcp__") and (
            _validated_attribution_identity(
                raw_tool,
                CAPABILITY_COMPONENT_PATTERN,
            )
            is not None
        ):
            # A bare tool name validates the explicit server, but cannot invent one.
            tool_name = raw_tool
            tool_is_valid = True

    if not tool_is_valid:
        server = None
    elif tool_server is not None:
        if server is not None and server.casefold() != tool_server.casefold():
            server = None
        else:
            server = tool_server

    return skill, plugin, server, tool_name


def _mcp_server_from_tool_name(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_ATTRIBUTION_IDENTITY_LENGTH:
        return None
    match = ATTRIBUTION_MCP_TOOL_PATTERN.fullmatch(value)
    if match is None:
        return None
    return match.group("server")


def _mcp_server_alias_key(value: str) -> str:
    """Match only Claude's observed display-space versus tool underscore form."""

    return value.casefold().replace(" ", "_")


def _project_skill_names(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {match.group("name") for match in CLAUDE_PROJECT_SKILL_PATH.finditer(value)}


def _mutated_project_skills(
    tool_name: str,
    arguments: dict[str, Any],
) -> set[str]:
    """Conservatively identify skills whose post-task files are unsafe to import."""

    names: set[str] = set()
    if tool_name in {
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "write_file",
    }:
        for field in ("file_path", "path", "notebook_path"):
            names.update(_project_skill_names(arguments.get(field)))
    if tool_name == "apply_patch":
        for field in ("patch", "input", "diff"):
            names.update(_project_skill_names(arguments.get(field)))
    if tool_name in {"Bash", "shell", "exec_command"}:
        for field in ("command", "cmd"):
            names.update(_project_skill_names(arguments.get(field)))
    return names


def _historical_model_request_timing(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure client-observed Claude request latency without idle or tool gaps."""

    events_by_uuid = {
        event["uuid"]: event
        for event in events
        if isinstance(event.get("uuid"), str) and event["uuid"].strip()
    }
    requests: dict[str, dict[str, Any]] = {}
    unidentified_assistant_event_count = 0
    for event in events:
        if event.get("type") != "assistant":
            continue
        timestamp = _parse_timestamp(event.get("timestamp"))
        if timestamp is None:
            continue
        request_id = event.get("requestId")
        message = event.get("message")
        provider_message_id = message.get("id") if isinstance(message, dict) else None
        if isinstance(request_id, str) and request_id.strip():
            key = f"request_id:{request_id}"
            identity_basis = "request_id"
        elif isinstance(provider_message_id, str) and provider_message_id.strip():
            key = f"provider_message_id:{provider_message_id}"
            identity_basis = "provider_message_id"
        else:
            unidentified_assistant_event_count += 1
            continue
        current = requests.get(key)
        if current is None:
            requests[key] = {
                "first_at": timestamp,
                "last_at": timestamp,
                "first_event": event,
                "identity_basis": identity_basis,
            }
            continue
        if timestamp < current["first_at"]:
            current["first_at"] = timestamp
            current["first_event"] = event
        if timestamp > current["last_at"]:
            current["last_at"] = timestamp

    observed_seconds = 0.0
    observed_request_count = 0
    missing_request_count = 0
    identity_basis_counts: dict[str, int] = {}
    for request in requests.values():
        first_event = request["first_event"]
        parent_uuid = first_event.get("parentUuid")
        parent = events_by_uuid.get(parent_uuid) if isinstance(parent_uuid, str) else None
        started_at = _parse_timestamp(parent.get("timestamp")) if parent is not None else None
        if started_at is None:
            missing_request_count += 1
            continue
        duration = (request["last_at"] - started_at).total_seconds()
        if not math.isfinite(duration) or duration < 0:
            missing_request_count += 1
            continue
        observed_seconds += duration
        observed_request_count += 1
        identity_basis = request["identity_basis"]
        identity_basis_counts[identity_basis] = identity_basis_counts.get(identity_basis, 0) + 1

    if observed_request_count == 0:
        status = "unavailable"
        seconds: float | None = None
    else:
        status = (
            "observed"
            if missing_request_count == 0 and unidentified_assistant_event_count == 0
            else "partial"
        )
        seconds = round(observed_seconds, 3)
    return {
        "status": status,
        "seconds": seconds,
        "request_count": observed_request_count,
        "missing_request_count": missing_request_count,
        "unidentified_assistant_event_count": unidentified_assistant_event_count,
        "identity_basis_counts": identity_basis_counts,
        "basis": "client_observed_request_latency",
        "includes": "request dispatch through final assistant transcript event",
        "excludes": "inter-request idle time, user waiting, and tool execution gaps",
        "limitation": (
            "Client-observed request latency includes transport and provider queueing; "
            "the transcript does not expose server-side inference duration."
        ),
    }


def _task_observations(
    source_path: Path,
    message_uuid: str,
    *,
    whole_thread: bool = False,
    linked_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tools: set[str] = set()
    skills: set[str] = set()
    skill_provenance: list[dict[str, str]] = []
    seen_skill_provenance: set[tuple[tuple[str, str], ...]] = set()
    plugins: set[str] = set()
    connectors: set[str] = set()
    attributed_mcp_observations: set[tuple[str, str | None]] = set()
    direct_mcp_observations: set[tuple[str, str]] = set()
    mutated_project_skills: set[str] = set()
    instruction_paths: set[str] = set()
    project_dirs: set[str] = set()
    changed_files: set[str] = set()
    usage_by_message: dict[str, dict[str, int]] = {}
    provider_message_ids: set[str] = set()
    fallback_message_count = 0
    claude_model: str | None = None
    first_at: datetime | None = None
    last_at: datetime | None = None

    if linked_sources is None:
        linked_sources = _linked_subagent_sources(
            source_path,
            message_uuid,
            whole_thread=whole_thread,
        )
    event_streams: list[list[dict[str, Any]]] = [
        list(_task_events(source_path, message_uuid, whole_thread=whole_thread)),
        *(
            list(_iter_events(Path(linked_source["source_path"])))
            for linked_source in linked_sources
        ),
    ]
    for event_stream in event_streams:
        for event in event_stream:
            (
                attributed_skill,
                attributed_plugin,
                attributed_server,
                attributed_mcp_tool,
            ) = _event_capability_attribution(event)
            if attributed_skill is not None:
                skills.add(attributed_skill)
                provenance = {
                    "name": attributed_skill,
                    "source": "claude_event_attribution",
                }
                if attributed_plugin is not None:
                    provenance["plugin"] = attributed_plugin
                elif ":" in attributed_skill:
                    provenance["plugin"] = attributed_skill.split(":", 1)[0]
                elif "/" in attributed_skill:
                    provenance["plugin"] = attributed_skill.split("/", 1)[0]
                provenance_key = tuple(sorted(provenance.items()))
                if provenance_key not in seen_skill_provenance:
                    seen_skill_provenance.add(provenance_key)
                    skill_provenance.append(provenance)
                if ":" in attributed_skill:
                    plugins.add(attributed_skill.split(":", 1)[0])
                elif "/" in attributed_skill:
                    plugins.add(attributed_skill.split("/", 1)[0])
            if attributed_plugin is not None:
                plugins.add(attributed_plugin)
            if attributed_server is not None:
                attributed_mcp_observations.add((attributed_server, attributed_mcp_tool))
            for skill in _project_skill_dependencies(event):
                skills.add(skill)
                provenance = {
                    "name": skill,
                    "source": "task_observed_claude_project_skill_path",
                }
                provenance_key = tuple(sorted(provenance.items()))
                if provenance_key not in seen_skill_provenance:
                    seen_skill_provenance.add(provenance_key)
                    skill_provenance.append(provenance)
            timestamp = _parse_timestamp(event.get("timestamp"))
            if timestamp is not None:
                first_at = timestamp if first_at is None else min(first_at, timestamp)
                last_at = timestamp if last_at is None else max(last_at, timestamp)
            cwd = event.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                project_dirs.add(cwd)
            message = event.get("message")
            if event.get("type") == "assistant" and isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model.strip():
                    claude_model = model
                key = _usage_key(event)
                if key is not None:
                    origin, message_id = key
                    unique_key = f"{origin}:{message_id}"
                    if origin == "provider_message_id":
                        provider_message_ids.add(message_id)
                    elif unique_key not in usage_by_message:
                        fallback_message_count += 1
                    current = usage_by_message.setdefault(
                        unique_key,
                        {field: 0 for field in (*TOKEN_FIELDS, *CACHE_CREATION_FIELDS)},
                    )
                    raw_usage = message["usage"]
                    for field in TOKEN_FIELDS:
                        count = raw_usage.get(field)
                        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                            current[field] = max(current[field], count)
                    cache_creation = raw_usage.get("cache_creation")
                    if isinstance(cache_creation, dict):
                        for field in CACHE_CREATION_FIELDS:
                            count = cache_creation.get(field)
                            if (
                                isinstance(count, int)
                                and not isinstance(count, bool)
                                and count >= 0
                            ):
                                current[field] = max(current[field], count)
            for block in _tool_blocks(event):
                name = block["name"]
                tools.add(name)
                arguments = block.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                if name == "Skill":
                    skill = arguments.get("skill", arguments.get("name"))
                    if isinstance(skill, str) and skill.strip():
                        skills.add(skill)
                        metadata = arguments.get("metadata")
                        if not isinstance(metadata, dict):
                            metadata = {}
                        provenance: dict[str, str] = {"name": skill}
                        for field in ("author", "publisher", "vendor", "source"):
                            value = arguments.get(field, metadata.get(field))
                            if isinstance(value, dict):
                                value = value.get("name")
                            if isinstance(value, str) and value.strip():
                                provenance[field] = _compact(value, limit=160)
                        plugin = arguments.get(
                            "plugin",
                            arguments.get(
                                "plugin_name",
                                arguments.get("pluginName"),
                            ),
                        )
                        if isinstance(plugin, str) and plugin.strip():
                            provenance["plugin"] = _compact(plugin, limit=160)
                            plugins.add(plugin)
                        elif ":" in skill:
                            provenance["plugin"] = skill.split(":", 1)[0]
                        elif "/" in skill:
                            provenance["plugin"] = skill.split("/", 1)[0]
                        provenance_key = tuple(sorted(provenance.items()))
                        if provenance_key not in seen_skill_provenance:
                            seen_skill_provenance.add(provenance_key)
                            skill_provenance.append(provenance)
                        if ":" in skill:
                            plugins.add(skill.split(":", 1)[0])
                        elif "/" in skill:
                            plugins.add(skill.split("/", 1)[0])
                connector = _mcp_server_from_tool_name(name)
                if connector is not None:
                    connectors.add(connector)
                    direct_mcp_observations.add((connector, name.split("__", 2)[2]))
                mutated_project_skills.update(_mutated_project_skills(name, arguments))
                for field in ("file_path", "path", "notebook_path"):
                    file_path = arguments.get(field)
                    if not isinstance(file_path, str) or not file_path.strip():
                        continue
                    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                        changed_files.add(file_path)
                    if Path(file_path).name in ("CLAUDE.md", "AGENTS.md"):
                        instruction_paths.add(file_path)

    for attributed_server, attributed_tool in attributed_mcp_observations:
        if any(
            _mcp_server_alias_key(attributed_server) == _mcp_server_alias_key(direct_server)
            and (attributed_tool is None or attributed_tool.casefold() == direct_tool.casefold())
            for direct_server, direct_tool in direct_mcp_observations
        ):
            continue
        connectors.add(attributed_server)

    totals = {field: 0 for field in TOKEN_FIELDS}
    for usage in usage_by_message.values():
        for field in TOKEN_FIELDS:
            totals[field] += usage[field]
    totals["cache_creation"] = {
        field: sum(usage[field] for usage in usage_by_message.values())
        for field in CACHE_CREATION_FIELDS
    }
    totals["cache_write_5m_tokens"] = totals["cache_creation"]["ephemeral_5m_input_tokens"]
    totals["cache_write_1h_tokens"] = totals["cache_creation"]["ephemeral_1h_input_tokens"]
    totals["cached_input_tokens"] = totals["cache_read_input_tokens"]
    totals["cache_write_input_tokens"] = totals["cache_creation_input_tokens"]
    totals["provider_message_count"] = len(provider_message_ids)
    totals["fallback_message_count"] = fallback_message_count
    totals["provider_message_ids"] = sorted(provider_message_ids)
    totals["deduplication"] = (
        "provider_message_id; request_id or event_uuid only when provider ID is unavailable"
    )

    wall_clock_seconds: int | None = None
    if first_at is not None and last_at is not None:
        wall_clock_seconds = max(0, int((last_at - first_at).total_seconds()))
    stream_timings = [_historical_model_request_timing(events) for events in event_streams]
    observed_stream_timings = [timing for timing in stream_timings if timing["seconds"] is not None]
    model_request_seconds = (
        round(sum(timing["seconds"] for timing in observed_stream_timings), 3)
        if observed_stream_timings
        else None
    )
    model_request_timing = {
        "status": (
            "unavailable"
            if not observed_stream_timings
            else (
                "observed"
                if all(timing["status"] == "observed" for timing in stream_timings)
                else "partial"
            )
        ),
        "seconds": model_request_seconds,
        "request_count": sum(timing["request_count"] for timing in stream_timings),
        "missing_request_count": sum(timing["missing_request_count"] for timing in stream_timings),
        "unidentified_assistant_event_count": sum(
            timing["unidentified_assistant_event_count"] for timing in stream_timings
        ),
        "identity_basis_counts": {
            basis: sum(timing["identity_basis_counts"].get(basis, 0) for timing in stream_timings)
            for basis in ("request_id", "provider_message_id")
            if any(basis in timing["identity_basis_counts"] for timing in stream_timings)
        },
        "source_count": len(event_streams),
        "basis": "client_observed_request_latency",
        "includes": "request dispatch through final assistant transcript event",
        "excludes": "inter-request idle time, user waiting, and tool execution gaps",
        "limitation": (
            "Client-observed request latency includes transport and provider queueing; "
            "the transcript does not expose server-side inference duration."
        ),
    }
    return {
        "observed_tools": sorted(tools),
        "observed_skills": sorted(skills),
        "observed_skill_provenance": sorted(
            skill_provenance,
            key=lambda item: (
                item.get("name", ""),
                item.get("author", ""),
                item.get("publisher", ""),
            ),
        ),
        "observed_plugins": _canonical_plugin_observations(plugins),
        "observed_connector_names": sorted(connectors),
        "task_mutated_claude_skills": sorted(mutated_project_skills),
        "observed_instruction_paths": sorted(instruction_paths),
        "project_dirs": sorted(project_dirs),
        "historical_changed_files": sorted(changed_files),
        "claude_model": claude_model,
        "historical_usage": totals,
        "historical_elapsed_seconds": wall_clock_seconds,
        "historical_wall_clock_seconds": wall_clock_seconds,
        "historical_model_request_seconds": model_request_seconds,
        "historical_model_request_timing": model_request_timing,
    }


def validate_replay_sources(replay_spec: dict[str, Any]) -> dict[str, Any]:
    """Return the currently linked transcript sources without binding later phases."""

    raw_source = replay_spec.get("source_path")
    message_uuid = replay_spec.get("message_uuid")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise TranscriptError("The replay specification has no main transcript source.")
    if not isinstance(message_uuid, str) or not message_uuid.strip():
        raise TranscriptError("The replay specification has no selected user-message UUID.")
    source_path = Path(raw_source).expanduser()
    linked = _linked_subagent_sources(
        source_path,
        message_uuid,
        whole_thread=replay_spec.get("task_scope") == "whole_thread",
    )
    return {
        "source_path": str(source_path),
        "linked_sources": linked,
    }


def build_replay_spec(session: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """Replay original user requests without leaking historical Claude output."""

    raw_path = session.get("source_path")
    message_uuid = task.get("message_uuid")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise TranscriptError("The imported session does not identify a source transcript.")
    if not isinstance(message_uuid, str) or not message_uuid.strip():
        raise TaskNotFoundError(
            "Select an original user-message UUID before constructing a replay."
        )
    source_path = Path(raw_path).expanduser()
    whole_thread = task.get("task_scope") == "whole_thread"

    preceding_context: deque[dict[str, Any]] = deque(maxlen=12)
    selected_event: dict[str, Any] | None = None
    thread_requests: list[str] = []
    thread_message_uuids: list[str] = []
    for event in _iter_events(source_path):
        if _is_actionable_user_event(event):
            if event.get("uuid") == message_uuid and selected_event is None:
                selected_event = event
                if not whole_thread:
                    break
            if selected_event is not None and whole_thread:
                thread_requests.append(_visible_message_text(event))
                thread_message_uuids.append(event["uuid"])
                continue
        if selected_event is not None and whole_thread:
            continue
        if event.get("type") not in ("user", "assistant"):
            continue
        if event.get("type") == "user" and not _is_actionable_user_event(event):
            continue
        text = _visible_message_text(event)
        if not text.strip():
            continue
        preceding_context.append(
            {
                "role": event["type"],
                "text": text[:12_000],
                "timestamp": _format_timestamp(_parse_timestamp(event.get("timestamp"))),
                "message_uuid": event.get("uuid"),
            }
        )
    if selected_event is None:
        raise TaskNotFoundError(
            "The selected original user-message UUID is not in the imported transcript."
        )

    linked_sources = _linked_subagent_sources(
        source_path,
        message_uuid,
        whole_thread=whole_thread,
    )
    observations = _task_observations(
        source_path,
        message_uuid,
        whole_thread=whole_thread,
        linked_sources=linked_sources,
    )
    timestamp = _format_timestamp(_parse_timestamp(selected_event.get("timestamp")))
    project_dir = selected_event.get("cwd")
    if not isinstance(project_dir, str) or not project_dir.strip():
        project_dir = task.get("project_dir", session.get("project_dir"))
    project_dirs = sorted(
        set(_string_list(session.get("project_dirs")))
        | set(observations["project_dirs"])
        | ({project_dir} if isinstance(project_dir, str) and project_dir.strip() else set())
    )
    connector_names = observations["observed_connector_names"]
    configured_connector_names = _string_list(session.get("connector_names"))
    replay = {
        "session_id": session.get("session_id", selected_event.get("sessionId", source_path.stem)),
        "imported_thread_id": session.get("imported_thread_id"),
        "source_path": str(source_path),
        "message_uuid": message_uuid,
        "request": (
            "\n\n".join(thread_requests) if whole_thread else _visible_message_text(selected_event)
        ),
        "task_timestamp": timestamp,
        "project_dir": project_dir,
        "project_dirs": project_dirs,
        "preceding_context": [] if whole_thread else list(preceding_context),
        "claude_model": observations["claude_model"] or session.get("claude_model"),
        "observed_tools": observations["observed_tools"],
        "observed_skills": observations["observed_skills"],
        "observed_skill_provenance": observations["observed_skill_provenance"],
        "observed_plugins": observations["observed_plugins"],
        "task_mutated_claude_skills": observations["task_mutated_claude_skills"],
        "observed_instruction_paths": observations["observed_instruction_paths"],
        "connector_names": connector_names,
        "configured_connector_names": configured_connector_names,
        "historical_usage": observations["historical_usage"],
        "historical_changed_files": observations["historical_changed_files"],
        "historical_elapsed_seconds": observations["historical_elapsed_seconds"],
        "historical_wall_clock_seconds": observations["historical_wall_clock_seconds"],
        "historical_model_request_seconds": observations["historical_model_request_seconds"],
        "historical_model_request_timing": observations["historical_model_request_timing"],
        "linked_sources": linked_sources,
        "imported_at": session.get("imported_at"),
    }
    if whole_thread:
        replay.update(
            {
                "task_scope": "whole_thread",
                "user_message_count": len(thread_requests),
                "message_uuids": thread_message_uuids,
            }
        )
    return replay


def _tool_result_text(event: dict[str, Any], block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            entry["text"]
            for entry in content
            if isinstance(entry, dict)
            and entry.get("type") == "text"
            and isinstance(entry.get("text"), str)
        )
    result = event.get("toolUseResult")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return "\n".join(
            result[field]
            for field in ("stdout", "output", "content")
            if isinstance(result.get(field), str)
        )
    return ""


def recover_historical_final_response(
    source_path: str | Path,
    message_uuid: str,
    *,
    whole_thread: bool = False,
) -> str | None:
    """Recover the final visible assistant response only for post-run reporting."""
    final_response: str | None = None
    for event in _task_events(
        Path(source_path).expanduser(),
        message_uuid,
        whole_thread=whole_thread,
    ):
        if event.get("type") != "assistant":
            continue
        visible_text = _visible_message_text(event)
        if visible_text.strip():
            final_response = _redact(visible_text)[:12_000]
    return final_response


def _is_mutating_tool(name: str, arguments: dict[str, Any]) -> bool:
    if name in (
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "apply_patch",
        "write_file",
    ):
        return True
    if name not in ("Bash", "shell", "exec_command"):
        return False
    command = arguments.get("command", arguments.get("cmd", ""))
    if not isinstance(command, str):
        return False
    if re.search(
        r"\bgit\b[^\n;&|]*?\s(?:add|am|apply|bisect|checkout|cherry-pick|clean|commit|merge|mv|rebase|reset|restore|revert|rm|stash|switch|worktree)\b",
        command,
    ):
        return True
    if re.search(r"(?:^|[\s;&|])(?:rm|mv|cp|touch|mkdir|rmdir|tee|truncate)\s", command):
        return True
    if re.search(r"\b(?:sed|perl)\s+[^\n]*\s-i(?:\s|$)", command):
        return True
    return re.search(r"(?<![0-9])>{1,2}(?!&)", command) is not None


def _simple_git_command(
    command: object,
    event: dict[str, Any],
) -> tuple[Path, str] | None:
    """Resolve one unambiguous Git command and its effective directory."""

    if not isinstance(command, str):
        return None
    if any(character in command for character in "\n\r;|<>`") or "$(" in command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    directory = Path(cwd).expanduser()
    index = 0
    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        if any("&" in token for offset, token in enumerate(tokens) if offset != 2):
            return None
        selected = Path(tokens[1]).expanduser()
        directory = selected if selected.is_absolute() else directory / selected
        index = 3
    elif "&&" in tokens or any("&" in token for token in tokens):
        return None
    if index >= len(tokens):
        return None
    if tokens[index] != "git":
        return None
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-C":
            if index + 1 >= len(tokens):
                return None
            selected = Path(tokens[index + 1]).expanduser()
            directory = selected if selected.is_absolute() else directory / selected
            index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            selected = Path(token[2:]).expanduser()
            directory = selected if selected.is_absolute() else directory / selected
            index += 1
            continue
        break
    if index >= len(tokens) or tokens[index].startswith("-"):
        return None
    subcommand = tokens[index]
    if any(token in ("&&", "&") or "&" in token for token in tokens[index + 1 :]):
        return None
    try:
        return directory.resolve(), subcommand
    except OSError:
        return None


def _git_status_directory(command: object, event: dict[str, Any]) -> Path | None:
    parsed = _simple_git_command(command, event)
    if parsed is None or parsed[1] != "status":
        return None
    return parsed[0]


def _git_repository_root(directory: Path) -> Path | None:
    ok, output = _git_capture(directory, "rev-parse", "--show-toplevel")
    if not ok or not output.strip():
        return None
    try:
        return Path(output.strip()).expanduser().resolve()
    except OSError:
        return None


def _same_repository_or_path(left: Path, right: Path) -> bool:
    try:
        resolved_left = left.expanduser().resolve()
        resolved_right = right.expanduser().resolve()
    except OSError:
        return False
    if resolved_left == resolved_right:
        return True
    left_root = _git_repository_root(resolved_left)
    right_root = _git_repository_root(resolved_right)
    return left_root is not None and right_root is not None and left_root == right_root


def _mutation_targets_repository(
    name: str,
    arguments: dict[str, Any],
    event: dict[str, Any],
    repository: Path | None,
) -> bool:
    if repository is None:
        return True
    expected = repository.expanduser().resolve()
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "write_file"):
        path = arguments.get(
            "file_path",
            arguments.get("path", arguments.get("notebook_path")),
        )
        if not isinstance(path, str) or not path.strip():
            return True
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            cwd = event.get("cwd")
            if not isinstance(cwd, str) or not cwd.strip():
                return True
            candidate = Path(cwd).expanduser() / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to(expected)
            return True
        except OSError:
            return True
        except ValueError:
            return _same_repository_or_path(candidate.parent, expected)
    if name in ("Bash", "shell", "exec_command"):
        command = arguments.get("command", arguments.get("cmd"))
        parsed = _simple_git_command(command, event)
        if parsed is None or parsed[1] == "worktree":
            return True
        try:
            parsed[0].relative_to(expected)
            return True
        except ValueError:
            return _same_repository_or_path(parsed[0], expected)
    return True


def _classify_status(command: str, output: str, *, is_error: bool) -> tuple[str, list[str]]:
    if is_error:
        return "unknown", []
    if "--porcelain" in command or re.search(r"(?:^|\s)-s(?:\s|$)", command):
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            return "verified_clean", []
        paths: list[str] = []
        for line in lines:
            if len(line) >= 3 and re.match(r"^(?:[ MARCUD!?]{2})\s", line):
                paths.append(line[3:].strip())
        return "known_dirty", paths
    lowered = output.casefold()
    if (
        "nothing to commit, working tree clean" in lowered
        or "nothing to commit, working directory clean" in lowered
    ):
        return "verified_clean", []
    dirty_phrases = (
        "changes not staged for commit",
        "changes to be committed",
        "untracked files:",
        "nothing added to commit but untracked files present",
        "unmerged paths:",
    )
    if any(phrase in lowered for phrase in dirty_phrases):
        paths = [
            match.group(1).strip()
            for line in output.splitlines()
            if (
                match := re.match(
                    r"\s*(?:modified:|new file:|deleted:|renamed:|both modified:)\s+(.+)",
                    line,
                )
            )
        ]
        return "known_dirty", paths
    return "unknown", []


def _status_evidence(
    events: Iterable[dict[str, Any]],
    *,
    source_path: Path,
    mutation_cutoff: datetime | None = None,
    repository: Path | None = None,
) -> dict[str, Any]:
    pending: dict[str, str] = {}
    latest: dict[str, Any] | None = None
    for event in events:
        event_timestamp = _parse_timestamp(event.get("timestamp"))
        if mutation_cutoff is not None:
            if event_timestamp is None:
                continue
            if event_timestamp >= mutation_cutoff:
                break
        for block in _tool_blocks(event):
            name = block["name"]
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {}
            if _is_mutating_tool(name, arguments) and _mutation_targets_repository(
                name,
                arguments,
                event,
                repository,
            ):
                if latest is not None:
                    return latest
                return {
                    "state": "unknown",
                    "evidence": None,
                    "affected_files": [],
                    "reason": "No historical git-status result was observed before the first repository mutation.",
                }
            command = arguments.get("command", arguments.get("cmd"))
            block_id = block.get("id")
            status_directory = _git_status_directory(command, event)
            repository_matches = status_directory is not None and (
                repository is None
                or _same_repository_or_path(
                    status_directory,
                    repository,
                )
            )
            if (
                repository_matches
                and isinstance(block_id, str)
            ):
                pending[block_id] = command
        for block in _content_blocks(event):
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or tool_use_id not in pending:
                continue
            command = pending.pop(tool_use_id)
            output = _tool_result_text(event, block)
            state, affected = _classify_status(
                command, output, is_error=bool(block.get("is_error"))
            )
            latest = {
                "state": state,
                "evidence": {
                    "source": "historical_transcript",
                    "source_path": str(source_path),
                    "timestamp": _format_timestamp(_parse_timestamp(event.get("timestamp"))),
                    "command": _compact(command, limit=500),
                    "output": _redact(output)[:4000],
                    "original_contents_reconstructable": (
                        False if state == "known_dirty" else None
                    ),
                },
                "affected_files": affected,
                "reason": (
                    None
                    if state != "unknown"
                    else "The historical git-status output does not establish cleanliness."
                ),
            }
    if latest is not None:
        return latest
    return {
        "state": "unknown",
        "evidence": None,
        "affected_files": [],
        "reason": "No historical git-status result was observed before a repository mutation.",
    }


def _subagent_paths(source_path: Path) -> list[Path]:
    candidates = (
        source_path.parent / source_path.stem / "subagents",
        source_path.parent / f"{source_path.stem}.subagents",
    )
    paths: set[Path] = set()
    for directory in candidates:
        try:
            if directory.is_dir():
                paths.update(path for path in directory.glob("*.jsonl") if path.is_file())
        except OSError:
            continue
    return sorted(paths)


def _first_mutation_timestamp(
    events: Iterable[dict[str, Any]],
    *,
    repository: Path | None = None,
) -> datetime | None:
    for event in events:
        for block in _tool_blocks(event):
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {}
            if _is_mutating_tool(
                block["name"],
                arguments,
            ) and _mutation_targets_repository(
                block["name"],
                arguments,
                event,
                repository,
            ):
                return _parse_timestamp(event.get("timestamp")) or datetime.min.replace(
                    tzinfo=timezone.utc
                )
    return None


def _historical_working_tree(
    source_path: Path,
    message_uuid: str,
    *,
    whole_thread: bool = False,
    linked_paths: list[Path] | None = None,
    repository: Path | None = None,
) -> dict[str, Any]:
    try:
        if linked_paths is None:
            linked_paths = _linked_subagent_paths(
                source_path,
                message_uuid,
                whole_thread=whole_thread,
            )
        mutation_times = [
            value
            for value in (
                _first_mutation_timestamp(
                    _task_events(source_path, message_uuid, whole_thread=whole_thread),
                    repository=repository,
                ),
                *(
                    _first_mutation_timestamp(
                        _iter_events(path),
                        repository=repository,
                    )
                    for path in linked_paths
                ),
            )
            if value is not None
        ]
        cutoff = min(mutation_times) if mutation_times else None
        main_evidence = _status_evidence(
            _task_events(source_path, message_uuid, whole_thread=whole_thread),
            source_path=source_path,
            mutation_cutoff=cutoff,
            repository=repository,
        )
    except TranscriptError as error:
        return {
            "state": "unknown",
            "evidence": None,
            "affected_files": [],
            "reason": str(error),
        }
    if not linked_paths:
        return main_evidence
    candidates = [main_evidence]
    for subagent_path in linked_paths:
        try:
            candidate = _status_evidence(
                _iter_events(subagent_path),
                source_path=subagent_path,
                mutation_cutoff=cutoff,
                repository=repository,
            )
        except TranscriptError:
            continue
        if candidate["state"] != "unknown":
            candidates.append(candidate)
    established = [candidate for candidate in candidates if candidate["state"] != "unknown"]
    dirty = [candidate for candidate in established if candidate["state"] == "known_dirty"]
    if dirty:
        return max(
            dirty,
            key=lambda candidate: (
                _parse_timestamp((candidate.get("evidence") or {}).get("timestamp"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
    if established:
        return max(
            established,
            key=lambda candidate: (
                _parse_timestamp((candidate.get("evidence") or {}).get("timestamp"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
    return main_evidence


def _git_capture(repo: Path, *arguments: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, _redact(str(error))[:1000]
    if completed.returncode != 0:
        return (
            False,
            _redact(completed.stderr.strip() or completed.stdout.strip())[:1000],
        )
    return True, completed.stdout


def inspect_baseline(replay_spec: dict[str, Any]) -> dict[str, Any]:
    """Infer a historical Git baseline using exclusively read-only Git commands."""

    raw_repo = replay_spec.get("project_dir")
    if not isinstance(raw_repo, str) or not raw_repo.strip():
        project_dirs = _string_list(replay_spec.get("project_dirs"))
        raw_repo = project_dirs[0] if project_dirs else None
    timestamp = _parse_timestamp(replay_spec.get("task_timestamp"))
    raw_source = replay_spec.get("source_path")
    message_uuid = replay_spec.get("message_uuid")
    linked_sources = replay_spec.get("linked_sources")
    working_tree_repository: Path | None = None
    if isinstance(raw_repo, str) and raw_repo.strip():
        selected_repository = Path(raw_repo).expanduser()
        working_tree_repository = (
            _git_repository_root(selected_repository) or selected_repository
        )
    working_tree = {
        "state": "unknown",
        "evidence": None,
        "affected_files": [],
        "reason": "No selected historical transcript and user-message UUID were supplied.",
    }
    if isinstance(raw_source, str) and isinstance(message_uuid, str):
        try:
            working_tree = _historical_working_tree(
                Path(raw_source).expanduser(),
                message_uuid,
                whole_thread=replay_spec.get("task_scope") == "whole_thread",
                repository=working_tree_repository,
                linked_paths=(
                    [
                        Path(source["source_path"])
                        for source in linked_sources
                        if isinstance(source, Mapping)
                        and isinstance(source.get("source_path"), str)
                    ]
                    if isinstance(linked_sources, list)
                    else None
                ),
            )
        except (HistoricalDiscoveryError, OSError):
            working_tree = {
                "state": "unknown",
                "evidence": None,
                "affected_files": [],
                "reason": "Historical pre-mutation git-status evidence could not be verified.",
            }
    result: dict[str, Any] = {
        "repository": raw_repo,
        "task_timestamp": _format_timestamp(timestamp),
        "commit": None,
        "kind": "unavailable",
        "confidence": "unavailable",
        "evidence": [],
        "working_tree": working_tree,
        "working_tree_state": working_tree["state"],
    }
    if not isinstance(raw_repo, str) or not raw_repo.strip():
        result["evidence"].append(
            {
                "source": "repository",
                "reason": "The selected historical task has no verified project directory.",
            }
        )
        return result
    if timestamp is None:
        result["evidence"].append(
            {
                "source": "transcript",
                "reason": "The selected historical task has no parseable original timestamp.",
            }
        )
        return result

    repo = Path(raw_repo).expanduser()
    ok, top_level = _git_capture(repo, "rev-parse", "--show-toplevel")
    if not ok:
        changed_files = _string_list(replay_spec.get("historical_changed_files"))
        contained_files: list[str] = []
        for raw_path in changed_files:
            try:
                contained_files.append(
                    Path(raw_path).expanduser().resolve().relative_to(repo.resolve()).as_posix()
                )
            except (OSError, ValueError):
                contained_files = []
                break
        if repo.is_dir() and contained_files:
            result["empty_directory_candidate"] = {
                "project_directory": str(repo.resolve()),
                "historical_changed_files": contained_files,
                "reason": (
                    "The selected task used a non-Git project directory and all observed "
                    "historical changed files are contained within it, indicating a "
                    "greenfield starting state."
                ),
            }
            result["evidence"].append(
                {
                    "source": "historical_transcript",
                    "reason": "The non-Git task has enough greenfield evidence for an empty projectless workspace.",
                    "historical_changed_files": contained_files,
                }
            )
        else:
            result["evidence"].append(
                {
                    "source": "git",
                    "reason": "The selected historical project is not an accessible Git repository.",
                }
            )
        return result
    repository_path = top_level.strip()
    result["repository"] = repository_path

    ok, reflog = _git_capture(
        repo,
        "reflog",
        "show",
        "--date=iso-strict",
        "--format=%H%x1f%gD%x1f%gs",
        "HEAD",
    )
    if ok:
        eligible: list[tuple[datetime, str, str, str]] = []
        for line in reflog.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) != 3:
                continue
            commit, selector, summary = parts
            matched = re.search(r"@\{([^}]+)\}", selector)
            reflog_time = _parse_timestamp(matched.group(1)) if matched else None
            if (
                reflog_time is not None
                and reflog_time <= timestamp
                and re.fullmatch(r"[a-fA-F0-9]{40,64}", commit)
            ):
                eligible.append((reflog_time, commit, selector, summary))
        if eligible:
            reflog_time, commit, selector, summary = max(eligible, key=lambda entry: entry[0])
            result.update({"commit": commit, "kind": "git_commit", "confidence": "direct"})
            result["evidence"].append(
                {
                    "source": "head_reflog",
                    "timestamp": _format_timestamp(reflog_time),
                    "selector": selector,
                    "commit": commit,
                    "summary": _compact(summary, limit=300),
                }
            )
            return result

    before = _format_timestamp(timestamp)
    ok, history = _git_capture(
        repo,
        "log",
        "--all",
        f"--before={before}",
        "-1",
        "--format=%H%x1f%cI%x1f%s",
    )
    if ok:
        parts = history.strip().split("\x1f", 2)
        if len(parts) == 3 and re.fullmatch(r"[a-fA-F0-9]{40,64}", parts[0]):
            commit, commit_time, summary = parts
            result.update({"commit": commit, "kind": "git_commit", "confidence": "inferred"})
            result["evidence"].append(
                {
                    "source": "git_history",
                    "timestamp": _format_timestamp(_parse_timestamp(commit_time)),
                    "commit": commit,
                    "summary": _compact(summary, limit=300),
                    "limitation": "Git history does not independently establish which commit was checked out.",
                }
            )
            return result

    result["evidence"].append(
        {
            "source": "git",
            "reason": "Neither the HEAD reflog nor reachable commit history establishes a commit at the task timestamp.",
        }
    )
    ok, current_history = _git_capture(
        repo,
        "log",
        "--all",
        "--format=%H%x1f%cI%x1f%s",
    )
    if ok:
        commits: list[tuple[datetime, str, str]] = []
        complete_history = True
        for line in current_history.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) != 3:
                complete_history = False
                break
            commit, commit_time, summary = parts
            parsed_time = _parse_timestamp(commit_time)
            if parsed_time is None or re.fullmatch(r"[a-fA-F0-9]{40,64}", commit) is None:
                complete_history = False
                break
            commits.append((parsed_time, commit, summary))
        if (
            complete_history
            and commits
            and all(commit_time > timestamp for commit_time, _, _ in commits)
        ):
            first_time, first_commit, first_summary = min(
                commits,
                key=lambda entry: entry[0],
            )
            result["post_task_git_history"] = {
                "first_reachable_commit": first_commit,
                "timestamp": _format_timestamp(first_time),
                "summary": _compact(first_summary, limit=300),
                "reason": (
                    "All reachable Git commits were created after the selected Claude task began."
                ),
            }
            result["evidence"].append(
                {
                    "source": "post_task_git_history",
                    **result["post_task_git_history"],
                }
            )
    return result


def _relative_historical_path(value: object, repo: Path, event: dict[str, Any]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        cwd = event.get("cwd")
        base = Path(cwd).expanduser() if isinstance(cwd, str) and cwd.strip() else repo
        candidate = base / candidate
    try:
        return candidate.resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _reconstruct_file_operations(
    operations: list[tuple[dict[str, Any], dict[str, Any]]],
    repo: Path,
    baseline_commit: str | None,
    *,
    empty_baseline: bool = False,
) -> tuple[str | None, list[str], list[str]]:
    originals: dict[str, str] = {}
    current: dict[str, str] = {}
    existed: dict[str, bool] = {}
    limitations: list[str] = []

    for event, block in operations:
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            limitations.append(
                "A historical file operation does not include reliable structured arguments."
            )
            continue
        relative = _relative_historical_path(
            arguments.get("file_path", arguments.get("path", arguments.get("notebook_path"))),
            repo,
            event,
        )
        if relative is None:
            limitations.append(
                "A historical file operation refers to a path outside the confirmed repository or omits its path."
            )
            continue
        if relative not in originals:
            if empty_baseline:
                ok, original = False, ""
            else:
                ok, original = _git_capture(repo, "show", f"{baseline_commit}:{relative}")
            existed[relative] = ok
            if not ok and block.get("name") not in ("Write", "write_file"):
                limitations.append(
                    f"The baseline contents of {relative} cannot be reconstructed for an edit."
                )
                continue
            originals[relative] = original if ok else ""
            current[relative] = originals[relative]
        if block.get("name") in ("Write", "write_file"):
            content = arguments.get("content")
            if not isinstance(content, str):
                limitations.append(
                    f"The complete historical written contents of {relative} are unavailable."
                )
                continue
            current[relative] = content
            continue
        if block.get("name") in ("Edit", "MultiEdit"):
            edits = arguments.get("edits") if block.get("name") == "MultiEdit" else [arguments]
            if not isinstance(edits, list):
                limitations.append(f"The exact historical edits to {relative} are unavailable.")
                continue
            for edit in edits:
                if not isinstance(edit, dict):
                    limitations.append(f"An unparseable historical edit affects {relative}.")
                    continue
                old = edit.get("old_string")
                new = edit.get("new_string")
                if not isinstance(old, str) or not isinstance(new, str) or not old:
                    limitations.append(
                        f"A historical edit to {relative} does not establish both original and replacement text."
                    )
                    continue
                occurrences = current[relative].count(old)
                replace_all = bool(edit.get("replace_all"))
                if occurrences == 0 or occurrences > 1 and not replace_all:
                    limitations.append(
                        f"The exact location of a historical edit to {relative} cannot be independently established."
                    )
                    continue
                current[relative] = current[relative].replace(old, new, -1 if replace_all else 1)
            continue
        limitations.append(
            f"The historical {block.get('name', 'file')} operation affecting {relative} is not fully reconstructable."
        )

    pieces: list[str] = []
    changed_files: list[str] = []
    for relative in sorted(current):
        before, after = originals[relative], current[relative]
        if before == after:
            continue
        changed_files.append(relative)
        fromfile = f"a/{relative}" if existed[relative] else "/dev/null"
        tofile = f"b/{relative}"
        header = f"diff --git a/{relative} b/{relative}\n"
        body = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=fromfile,
                tofile=tofile,
            )
        )
        if body:
            pieces.append(header + body)
    return ("".join(pieces) or None), changed_files, list(dict.fromkeys(limitations))


def _commit_from_tool_output(output: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"\[[^\]\n]*\s([a-fA-F0-9]{7,64})\]",
        r"(?im)^\s*(?:commit\s+)?([a-fA-F0-9]{40,64})\s*$",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, output):
            value = match.group(1)
            if value not in candidates:
                candidates.append(value)
    return candidates


def recover_historical_solution(
    replay_spec: dict[str, Any],
    baseline_commit: str | None = None,
    *,
    baseline_kind: str = "git_commit",
) -> dict[str, Any]:
    """Recover only transcript-attributable Claude changes against one baseline.

    A complete solution requires an immutable task-attributed Git commit and
    verified baseline ancestry. Transcript diffs and reconstructed structured
    writes are explicitly partial: shell mutations or untracked changes cannot
    be proved absent.
    """

    raw_baseline = baseline_commit or replay_spec.get("baseline_commit")
    result: dict[str, Any] = {
        "provenance": "unavailable",
        "baseline_commit": raw_baseline,
        "baseline_kind": baseline_kind,
        "commit": None,
        "diff": None,
        "changed_files": [],
        "evidence": [],
        "limitations": [],
    }
    raw_repo = replay_spec.get("project_dir")
    raw_source = replay_spec.get("source_path")
    message_uuid = replay_spec.get("message_uuid")
    if baseline_kind not in {"git_commit", "empty_directory"}:
        result["limitations"].append("The historical baseline kind is unsupported.")
        return result
    if baseline_kind == "git_commit" and (
        not isinstance(raw_baseline, str) or not raw_baseline.strip()
    ):
        result["limitations"].append(
            "A historical baseline commit is required before recovering a solution."
        )
        return result
    if not isinstance(raw_repo, str) or not raw_repo.strip():
        result["limitations"].append("The selected historical task has no verified Git repository.")
        return result
    if not isinstance(raw_source, str) or not isinstance(message_uuid, str):
        result["limitations"].append(
            "The selected historical transcript and original user-message UUID are required."
        )
        return result
    repo = Path(raw_repo).expanduser()
    resolved_baseline: str | None = None
    if baseline_kind == "git_commit":
        ok, resolved = _git_capture(repo, "rev-parse", "--verify", f"{raw_baseline}^{{commit}}")
        resolved_baseline = resolved.strip()
        if not ok or re.fullmatch(r"[a-fA-F0-9]{40,64}", resolved_baseline) is None:
            result["limitations"].append(
                "The historical baseline commit is not available in the selected repository."
            )
            return result
        result["baseline_commit"] = resolved_baseline
    else:
        result["baseline_commit"] = None

    pending_commits: dict[str, str] = {}
    pending_diffs: dict[str, str] = {}
    commit_candidates: list[tuple[str, str, str | None]] = []
    observed_diffs: list[tuple[str, str, str | None]] = []
    file_operations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_tools: set[str] = set()
    shell_mutation_observed = False
    non_write_mutation_observed = False
    try:
        for event in _task_events(
            Path(raw_source).expanduser(),
            message_uuid,
            whole_thread=replay_spec.get("task_scope") == "whole_thread",
        ):
            for block in _tool_blocks(event):
                block_id = block.get("id")
                if isinstance(block_id, str) and block_id in seen_tools:
                    continue
                if isinstance(block_id, str):
                    seen_tools.add(block_id)
                name = block["name"]
                arguments = block.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "write_file"):
                    file_operations.append((event, block))
                if _is_mutating_tool(name, arguments) and name not in (
                    "Write",
                    "write_file",
                ):
                    non_write_mutation_observed = True
                if name not in ("Bash", "shell", "exec_command"):
                    continue
                command = arguments.get("command", arguments.get("cmd"))
                if not isinstance(command, str):
                    continue
                if _is_mutating_tool(name, arguments) and not re.search(
                    r"\bgit\b[^\n;&|]*?\bcommit\b", command
                ):
                    shell_mutation_observed = True
                if not isinstance(block_id, str):
                    continue
                if re.search(r"\bgit\b[^\n;&|]*?\bcommit\b", command):
                    pending_commits[block_id] = command
                elif re.search(r"\bgit\b[^\n;&|]*?\bdiff\b", command):
                    pending_diffs[block_id] = command
            for block in _content_blocks(event):
                if block.get("type") != "tool_result" or block.get("is_error"):
                    continue
                tool_id = block.get("tool_use_id")
                if not isinstance(tool_id, str):
                    continue
                output = _tool_result_text(event, block)
                timestamp = _format_timestamp(_parse_timestamp(event.get("timestamp")))
                if tool_id in pending_commits:
                    command = pending_commits.pop(tool_id)
                    for candidate in _commit_from_tool_output(output):
                        commit_candidates.append((candidate, command, timestamp))
                if tool_id in pending_diffs:
                    command = pending_diffs.pop(tool_id)
                    if "diff --git " in output or ("--- " in output and "+++ " in output):
                        observed_diffs.append((output, command, timestamp))
    except HistoricalDiscoveryError as error:
        result["limitations"].append(str(error))
        return result

    if baseline_kind == "empty_directory":
        if not file_operations:
            result["limitations"].append(
                "No reconstructable structured file writes were observed for the empty-directory task."
            )
            return result
        diff, changed_files, limitations = _reconstruct_file_operations(
            file_operations,
            repo,
            None,
            empty_baseline=True,
        )
        result["limitations"].extend(limitations)
        if diff is not None:
            complete = not shell_mutation_observed and not non_write_mutation_observed
            result.update(
                {
                    "provenance": (
                        "selected_task_complete_structured_writes"
                        if complete
                        else "historical_transcript_file_operations"
                    ),
                    "diff": diff,
                    "changed_files": changed_files,
                    "evidence": [
                        {
                            "source": "selected_task_structured_file_operations",
                            "source_path": raw_source,
                            "operation_count": len(file_operations),
                            "greenfield_baseline_observed": True,
                            "only_complete_write_mutations_observed": complete,
                        }
                    ],
                }
            )
            if not complete:
                result["limitations"].append(
                    "Non-Write mutations prevent complete reconstruction against the empty baseline."
                )
            result["limitations"] = list(dict.fromkeys(result["limitations"]))
            return result
        return result

    for candidate, command, timestamp in reversed(commit_candidates):
        ok, commit_output = _git_capture(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}")
        commit = commit_output.strip()
        if (
            not ok
            or re.fullmatch(r"[a-fA-F0-9]{40,64}", commit) is None
            or commit == resolved_baseline
        ):
            continue
        ancestor, _ = _git_capture(repo, "merge-base", "--is-ancestor", resolved_baseline, commit)
        if not ancestor:
            continue
        ok, diff = _git_capture(
            repo, "diff", "--binary", "--no-ext-diff", f"{resolved_baseline}..{commit}"
        )
        if not ok:
            continue
        names_ok, names = _git_capture(
            repo,
            "diff",
            "--name-only",
            "--no-ext-diff",
            f"{resolved_baseline}..{commit}",
        )
        result.update(
            {
                "provenance": "attributed_git_commit",
                "commit": commit,
                "diff": diff,
                "changed_files": (
                    [line for line in names.splitlines() if line] if names_ok else []
                ),
                "evidence": [
                    {
                        "source": "selected_task_git_commit_tool_result",
                        "source_path": raw_source,
                        "timestamp": timestamp,
                        "command": _compact(command, limit=500),
                        "commit": commit,
                        "baseline_ancestry_verified": True,
                    }
                ],
            }
        )
        return result

    if observed_diffs:
        diff, command, timestamp = observed_diffs[-1]
        result.update(
            {
                "provenance": "historical_transcript_diff",
                "diff": diff,
                "changed_files": sorted(set(re.findall(r"(?m)^diff --git a/.+? b/(.+)$", diff))),
                "evidence": [
                    {
                        "source": "selected_task_git_diff_tool_result",
                        "source_path": raw_source,
                        "timestamp": timestamp,
                        "command": _compact(command, limit=500),
                    }
                ],
            }
        )
        result["limitations"].append(
            "A transcript-captured diff cannot prove that staged, unstaged, untracked, binary, or later Claude changes are complete."
        )
        return result

    if file_operations:
        diff, changed_files, limitations = _reconstruct_file_operations(
            file_operations, repo, resolved_baseline
        )
        result["limitations"].extend(limitations)
        if diff is not None:
            result.update(
                {
                    "provenance": "historical_transcript_file_operations",
                    "diff": diff,
                    "changed_files": changed_files,
                    "evidence": [
                        {
                            "source": "selected_task_structured_file_operations",
                            "source_path": raw_source,
                            "operation_count": len(file_operations),
                        }
                    ],
                }
            )
            if shell_mutation_observed:
                result["limitations"].append(
                    "Historical shell mutations cannot be fully reconstructed from structured file-operation evidence."
                )
            result["limitations"].append(
                "Transcript file operations cannot prove that the full historical Claude solution or all repository changes were recovered."
            )
            result["limitations"] = list(dict.fromkeys(result["limitations"]))
            return result

    result["limitations"].append(
        "No task-attributed Git commit, preserved diff, or reconstructable historical file operation was found."
    )
    return result


__all__ = tuple(name for name in globals() if not name.startswith("__"))
