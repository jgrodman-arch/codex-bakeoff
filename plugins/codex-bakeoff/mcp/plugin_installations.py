"""Resolve the newest enabled, locally cached Codex Bakeoff installation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path


def semantic_version(
    value: str | None,
) -> tuple[int, int, int, bool, tuple[tuple[int, int | str], ...]] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        value,
    )
    if match is None:
        return None
    prerelease = match.group(4)
    identifiers: tuple[tuple[int, int | str], ...] = (
        tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split("."))
        if prerelease is not None
        else ()
    )
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        prerelease is None,
        identifiers,
    )


def _enabled_marketplaces(codex_home: Path, plugin_name: str) -> set[str]:
    try:
        config = (codex_home / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()

    section_pattern = re.compile(
        r"^\s*\[\s*plugins\s*\.\s*"
        r"(?:\"(?P<double>(?:[^\"\\]|\\.)+)\"|'(?P<single>[^']+)'|"
        r"(?P<bare>[A-Za-z0-9_.@-]+))\s*\]\s*(?:#.*)?$"
    )
    enabled_pattern = re.compile(r"^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$")
    marketplace_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
    marketplaces: set[str] = set()
    marketplace: str | None = None

    for line in config.splitlines():
        match = section_pattern.fullmatch(line)
        if match is not None:
            name = match.group("double") or match.group("single") or match.group("bare")
            if match.group("double") is not None:
                try:
                    name = json.loads(f'"{name}"')
                except json.JSONDecodeError:
                    marketplace = None
                    continue
            prefix = f"{plugin_name}@"
            candidate = name[len(prefix) :] if name.startswith(prefix) else ""
            marketplace = candidate if marketplace_pattern.fullmatch(candidate) else None
            continue
        if line.lstrip().startswith("["):
            marketplace = None
            continue
        enabled = enabled_pattern.fullmatch(line)
        if marketplace is not None and enabled is not None:
            if enabled.group(1) == "true":
                marketplaces.add(marketplace)
            else:
                marketplaces.discard(marketplace)
    return marketplaces


def latest_enabled_plugin_root(current_root: Path, plugin_name: str, current_version: str) -> Path:
    selected_version = semantic_version(current_version)
    if selected_version is None:
        return current_root

    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    selected_root = current_root
    for marketplace in sorted(_enabled_marketplaces(codex_home, plugin_name)):
        cache_root = codex_home / "plugins" / "cache" / marketplace / plugin_name
        try:
            candidates = list(cache_root.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            try:
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
                payload = json.loads(
                    (candidate / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping) or payload.get("name") != plugin_name:
                continue
            version = semantic_version(payload.get("version"))
            if (
                version is None
                or version <= selected_version
                or not (candidate / "mcp" / "server.py").is_file()
                or not (candidate / "mcp" / "controller.html").is_file()
            ):
                continue
            selected_root = candidate.resolve()
            selected_version = version
    return selected_root
