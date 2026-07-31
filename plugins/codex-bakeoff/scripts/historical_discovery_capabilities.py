#!/usr/bin/env python3
"""Small, exact-match capability inventory for historical Claude threads."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10.
    tomllib = None  # type: ignore[assignment]

NATIVE_TOOLS = {
    "Bash": "Codex sandboxed shell",
    "Read": "Codex workspace file reading",
    "Write": "Codex workspace file editing",
    "Edit": "Codex workspace file editing",
    "Glob": "Codex workspace search",
    "Grep": "Codex workspace search",
    "Skill": "Codex skill invocation",
    "AskUserQuestion": "Codex user-input request",
    "ToolSearch": "Codex tool discovery",
}
OFFICIAL_MARKETPLACES = {
    "openai-bundled",
    "openai-curated",
    "openai-curated-remote",
    "openai-primary-runtime",
}


class HistoricalDiscoveryError(ValueError):
    """Capability inventory could not be read safely."""


def _configured_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )


def _read_codex_config() -> dict[str, Any]:
    path = _configured_codex_home() / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if tomllib is not None:
        try:
            loaded = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    connectors: dict[str, dict[str, Any]] = {}
    table = re.compile(
        r"""^\s*\[\s*(?:mcp_servers|mcpServers)\s*\.\s*
        (?:"(?P<double>(?:[^"\\]|\\.)+)"|'(?P<single>[^']+)'|(?P<bare>[A-Za-z0-9_-]+))
        (?:\s*\.[^\]]+)?\s*\]\s*(?:\#.*)?$""",
        re.VERBOSE,
    )
    for line in text.splitlines():
        match = table.match(line)
        if match is None:
            continue
        name = match.group("double") or match.group("single") or match.group("bare")
        if match.group("double"):
            try:
                name = json.loads(f'"{name}"')
            except json.JSONDecodeError:
                continue
        connectors.setdefault(name, {})
    return {"mcp_servers": connectors} if connectors else {}


def _identity(value: object) -> str:
    return str(value or "").strip().casefold()


def _plugin_identity(value: object) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    if "@" not in raw or raw.startswith("@"):
        return _identity(raw), None
    name, marketplace = raw.rsplit("@", 1)
    return _identity(name), _identity(marketplace)


def _canonical_plugin_observations(values: Iterable[str]) -> list[str]:
    qualified: dict[str, set[str]] = {}
    bare: set[str] = set()
    for value in values:
        name, marketplace = _plugin_identity(value)
        if not name:
            continue
        if marketplace:
            qualified.setdefault(name, set()).add(marketplace)
        else:
            bare.add(name)
    result = [
        f"{name}@{marketplace}"
        for name, marketplaces in qualified.items()
        for marketplace in marketplaces
    ]
    result.extend(name for name in bare if name not in qualified)
    return sorted(result)


def _manifest_skill_names(plugin_root: Path, manifest: Mapping[str, Any]) -> set[str]:
    raw = manifest.get("skills", "./skills/")
    if not isinstance(raw, str):
        return set()
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        return set()
    root = plugin_root / relative
    if not root.is_dir():
        return set()
    names: set[str] = set()
    for skill_file in root.glob("*/SKILL.md"):
        names.add(_identity(skill_file.parent.name))
        try:
            first = skill_file.read_text(encoding="utf-8")[:4_000]
        except (OSError, UnicodeError):
            continue
        match = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", first)
        if match:
            names.add(_identity(match.group(1)))
    return names


def _installed_plugins() -> list[dict[str, Any]]:
    cache = _configured_codex_home() / "plugins" / "cache"
    plugins: list[dict[str, Any]] = []
    try:
        manifests = sorted(cache.glob("*/*/*/.codex-plugin/plugin.json"))
    except OSError:
        return []
    for manifest_path in manifests:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        plugin_root = manifest_path.parent.parent
        try:
            marketplace, path_name, version = plugin_root.relative_to(cache).parts
        except ValueError:
            continue
        name = payload.get("name")
        if not isinstance(name, str) or _identity(name) != _identity(path_name):
            continue
        plugins.append(
            {
                "name": path_name,
                "marketplace": marketplace,
                "version": str(payload.get("version") or version),
                "plugin_id": f"{path_name}@{marketplace}",
                "source_root": str(plugin_root.resolve()),
                "official": marketplace in OFFICIAL_MARKETPLACES,
                "skills": sorted(_manifest_skill_names(plugin_root, payload)),
            }
        )
    return plugins


def _local_skills(plugins: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    roots = (
        _configured_codex_home() / "skills",
        Path.home() / ".agents" / "skills",
    )
    for root in roots:
        try:
            skill_files = root.glob("*/SKILL.md")
        except OSError:
            continue
        for skill_file in skill_files:
            name = _identity(skill_file.parent.name)
            result.setdefault(
                name,
                {
                    "name": skill_file.parent.name,
                    "source": str(skill_file.parent.resolve()),
                    "kind": "local_skill",
                },
            )
    for plugin in plugins:
        for skill in plugin.get("skills", []):
            result.setdefault(
                _identity(skill),
                {
                    "name": skill,
                    "source": plugin.get("source_root"),
                    "kind": "plugin_skill",
                    "plugin_id": plugin.get("plugin_id"),
                    "official": plugin.get("official"),
                },
            )
    return result


def _configured_connectors(config: Mapping[str, Any]) -> set[str]:
    raw = config.get("mcp_servers")
    if not isinstance(raw, Mapping):
        raw = config.get("mcpServers")
    if not isinstance(raw, Mapping):
        return set()
    return {_identity(name) for name in raw}


def _resolution_action(
    *,
    action_id: str,
    action: str,
    title: str,
    capability_id: str,
    remediation: str,
    readiness_action_id: str | None = None,
) -> dict[str, Any]:
    suggested_steps = [action]
    readiness_ids: list[str] = []
    if readiness_action_id:
        suggested_steps.append("verify_access")
        readiness_ids.append(readiness_action_id)
    return {
        "id": action_id,
        "kind": "resolution",
        "title": title,
        "status": "optional",
        "action": action,
        "suggested_steps": suggested_steps,
        "readiness_action_ids": readiness_ids,
        "capability_ids": [capability_id],
        "remediation_action": remediation,
        "remediation_actions": [remediation],
    }


def inspect_capabilities(
    replay_spec: dict[str, Any],
    verified_action_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Inventory only capabilities observed in the selected thread.

    This reports current matches and actions. It does not copy, hash, or bind
    capability files to a later execution.
    """

    verified = {
        str(item).strip() for item in verified_action_ids if isinstance(item, str) and item.strip()
    }
    plugins = _installed_plugins()
    skills = _local_skills(plugins)
    config = _read_codex_config()
    connectors = _configured_connectors(config)
    items: list[dict[str, Any]] = []
    actions: dict[str, dict[str, Any]] = {}
    known_readiness: set[str] = set()
    applied_readiness: set[str] = set()

    def add(
        *,
        capability_id: str,
        kind: str,
        name: str,
        status: str,
        resolution: str,
        description: str,
        equivalent: str | None = None,
        action: dict[str, Any] | None = None,
        readiness_action_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        item = {
            "id": capability_id,
            "kind": kind,
            "name": name,
            "title": f"{kind.title()} · {name}",
            "status": status,
            "resolution": resolution,
            "description": description,
            "evidence": description,
            "equivalent": equivalent,
            "readiness": "ready" if status != "not_available" else "not_available",
            "readiness_action_id": readiness_action_id,
            "resolution_action_id": action.get("id") if action else None,
            "resolution_action": action.get("action") if action else None,
            "remediation_action": action.get("remediation_action") if action else None,
            "guidance": action.get("remediation_action") if action else None,
        }
        if details:
            item.update(details)
        items.append(item)
        if readiness_action_id:
            known_readiness.add(readiness_action_id)
            if readiness_action_id in verified:
                applied_readiness.add(readiness_action_id)
        if action:
            existing = actions.get(action["id"])
            if existing is None:
                actions[action["id"]] = action
            elif capability_id not in existing["capability_ids"]:
                existing["capability_ids"].append(capability_id)

    for name in sorted(set(replay_spec.get("observed_tools") or [])):
        capability_id = f"tool:{name}"
        equivalent = NATIVE_TOOLS.get(name)
        if equivalent:
            add(
                capability_id=capability_id,
                kind="tool",
                name=name,
                status="codex_native_equivalent",
                resolution="local_equivalent",
                description="Codex has a native equivalent.",
                equivalent=equivalent,
            )
        else:
            add(
                capability_id=capability_id,
                kind="tool",
                name=name,
                status="best_effort",
                resolution="best_effort",
                description="No exact generic-tool match is required; Codex will use its available tools.",
            )

    for raw_name in sorted(set(replay_spec.get("observed_skills") or [])):
        name = str(raw_name)
        capability_id = f"skill:{name}"
        match = skills.get(_identity(name))
        if match:
            add(
                capability_id=capability_id,
                kind="skill",
                name=name,
                status="available_and_ready",
                resolution="local_equivalent",
                description="An exact local Codex skill is available.",
                details={"local_match": match},
            )
            continue
        action_id = f"import:claude-skill:{_identity(name)}"
        action = _resolution_action(
            action_id=action_id,
            action="import_from_claude",
            title=f"Import Claude skill · {name}",
            capability_id=capability_id,
            remediation="Go to Settings > Import to review and import this Claude skill.",
        )
        add(
            capability_id=capability_id,
            kind="skill",
            name=name,
            status="not_available",
            resolution="ported_from_claude",
            description="No exact local Codex skill is available.",
            action=action,
        )

    installed_by_name: dict[str, list[dict[str, Any]]] = {}
    for plugin in plugins:
        installed_by_name.setdefault(_identity(plugin["name"]), []).append(plugin)
    for raw_name in _canonical_plugin_observations(
        [str(item) for item in replay_spec.get("observed_plugins") or []]
    ):
        name, marketplace = _plugin_identity(raw_name)
        capability_id = f"plugin:{raw_name}"
        matches = [
            plugin
            for plugin in installed_by_name.get(name, [])
            if marketplace is None or _identity(plugin["marketplace"]) == marketplace
        ]
        if len(matches) == 1:
            add(
                capability_id=capability_id,
                kind="plugin",
                name=raw_name,
                status="available_and_ready",
                resolution="local_equivalent",
                description="An exact installed Codex plugin is available.",
                details={"installed_plugin": matches[0]},
            )
            continue
        action_id = f"import:claude-plugin:{raw_name}"
        action = _resolution_action(
            action_id=action_id,
            action="import_from_claude",
            title=f"Import Claude plugin · {raw_name}",
            capability_id=capability_id,
            remediation="Go to Settings > Import to review and import this Claude plugin.",
        )
        add(
            capability_id=capability_id,
            kind="plugin",
            name=raw_name,
            status="not_available",
            resolution="ported_from_claude",
            description="No exact installed Codex plugin is available.",
            action=action,
        )

    observed_connectors = {
        str(item)
        for item in (
            list(replay_spec.get("connector_names") or [])
            + list(replay_spec.get("observed_mcp_servers") or [])
        )
        if str(item).strip()
    }
    for name in sorted(observed_connectors):
        capability_id = f"connector:{name}"
        readiness_id = f"verify:connector:{_identity(name)}"
        if _identity(name) in connectors and readiness_id in verified:
            add(
                capability_id=capability_id,
                kind="connector",
                name=name,
                status="available_and_ready",
                resolution="local_equivalent",
                description="The configured connector was exercised successfully.",
                readiness_action_id=readiness_id,
            )
            continue
        configured = _identity(name) in connectors
        action = _resolution_action(
            action_id=(
                readiness_id if configured else f"import:claude-connector:{_identity(name)}"
            ),
            action="verify_access" if configured else "import_from_claude",
            title=(f"Verify connector · {name}" if configured else f"Import connector · {name}"),
            capability_id=capability_id,
            remediation=(
                "Exercise this configured connector, then verify access."
                if configured
                else "Go to Settings > Import to review and import this Claude connector."
            ),
            readiness_action_id=None if configured else readiness_id,
        )
        add(
            capability_id=capability_id,
            kind="connector",
            name=name,
            status="not_available",
            resolution=("local_equivalent" if configured else "not_available"),
            description="Connector readiness has not been observed in this session.",
            action=action,
            readiness_action_id=readiness_id,
        )

    for raw_path in sorted(set(replay_spec.get("observed_instruction_paths") or [])):
        name = str(raw_path)
        capability_id = f"instruction:{name}"
        path = Path(name).expanduser()
        if not path.is_absolute() and isinstance(replay_spec.get("project_dir"), str):
            path = Path(replay_spec["project_dir"]).expanduser() / path
        if path.name == "AGENTS.md" and path.is_file():
            add(
                capability_id=capability_id,
                kind="instruction",
                name=name,
                status="codex_native_equivalent",
                resolution="local_equivalent",
                description="This AGENTS.md file is natively supported by Codex.",
                equivalent="Codex AGENTS.md instructions",
            )
            continue
        if path.name != "CLAUDE.md" or not path.is_file() or path.with_name("AGENTS.md").exists():
            add(
                capability_id=capability_id,
                kind="instruction",
                name=name,
                status="not_available",
                resolution="not_available",
                description="The observed instruction file has no verified Codex equivalent.",
            )
            continue
        action_id = f"import:claude-instruction:{_identity(name)}"
        action = _resolution_action(
            action_id=action_id,
            action="import_from_claude",
            title=f"Import Claude instruction · {name}",
            capability_id=capability_id,
            remediation="Go to Settings > Import to import this CLAUDE.md as AGENTS.md.",
        )
        add(
            capability_id=capability_id,
            kind="instruction",
            name=name,
            status="not_available",
            resolution="ported_from_claude",
            description="The observed Claude instruction has no exact local Codex match.",
            action=action,
        )

    counts = dict(sorted(Counter(item["status"] for item in items).items()))
    resolution_actions = sorted(actions.values(), key=lambda item: item["id"])
    return {
        "items": items,
        "summary": counts,
        "unavailable_capabilities": [
            item for item in items if item["status"] == "not_available"
        ],
        "resolution_actions": resolution_actions,
        "known_readiness_action_ids": sorted(known_readiness),
        "verified_readiness_action_ids": sorted(applied_readiness),
        "unapplied_verified_action_ids": sorted((verified & known_readiness) - applied_readiness),
        "invalid_verified_action_ids": sorted(verified - known_readiness),
        "official_plugin_catalog": {
            "status": "available",
            "marketplaces": sorted(OFFICIAL_MARKETPLACES),
            "plugin_count": sum(1 for plugin in plugins if plugin["official"]),
            "invalid_entry_count": 0,
        },
        "limitations": [
            "Capability matching uses exact local names and current configuration.",
            "Configured connectors still require one observed successful operation.",
        ],
    }


__all__ = tuple(name for name in globals() if not name.startswith("__"))
