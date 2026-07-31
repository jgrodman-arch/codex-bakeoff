#!/usr/bin/env python3
"""Immediate shared checks for lean historical bakeoffs.

Checks are discovered from the stated baseline and run immediately against the
baseline and both candidate patches. No verification plan is persisted or
hash-bound to another phase.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from historical_file_selection import (  # noqa: E402
    FileSelectionError,
    materialize_directory_baseline,
)

MAX_TEST_FILES = 40
MAX_LOG_CHARS = 20_000
DEFAULT_TIMEOUT_SECONDS = 300


def _absolute_no_follow(raw: Path | str) -> Path:
    return Path(os.path.abspath(Path(raw).expanduser()))


class VerificationError(RuntimeError):
    """Shared checks could not be prepared safely."""


def _safe_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if path.parts and path.parts[0] == ".git":
        return None
    return path.as_posix()


def _git(
    root: Path,
    *arguments: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Git command failed."
        raise VerificationError(detail[:2_000])
    return result


def _baseline_files(baseline: Mapping[str, Any]) -> tuple[str, ...]:
    kind = baseline.get("kind")
    repository = Path(str(baseline.get("repository", ""))).expanduser()
    commit = baseline.get("commit")
    if kind == "git_commit" and isinstance(commit, str):
        result = _git(repository, "ls-tree", "-r", "--name-only", commit)
        return tuple(
            path
            for line in result.stdout.splitlines()
            if (path := _safe_relative(line)) is not None
        )
    raw = baseline.get("before_files", [])
    if isinstance(raw, list):
        return tuple(
            path
            for item in raw
            if isinstance(item, Mapping) and (path := _safe_relative(item.get("path"))) is not None
        )
    return ()


def discover_checks(
    baseline: Mapping[str, Any],
    replay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Find a small, deterministic set of baseline-owned test commands."""

    del replay
    files = _baseline_files(baseline)
    node_tests = sorted(
        path
        for path in files
        if re.search(r"(?:^|/)(?:test|tests)/", path)
        and path.endswith((".js", ".mjs", ".cjs"))
        or re.search(r"\.(?:test|spec)\.(?:js|mjs|cjs)$", path)
    )[:MAX_TEST_FILES]
    python_tests = sorted(
        path
        for path in files
        if (
            re.search(r"(?:^|/)tests?/test_[^/]+\.py$", path)
            or re.search(r"(?:^|/)[^/]+_test\.py$", path)
        )
    )[:MAX_TEST_FILES]

    checks: list[dict[str, Any]] = []
    if node_tests:
        checks.append(
            {
                "id": "node-test",
                "command": ["node", "--test", *node_tests],
                "cwd": ".",
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "source": "baseline",
            }
        )
    if python_tests:
        checks.append(
            {
                "id": "python-unittest",
                "command": ["python3", "-m", "unittest", *python_tests],
                "cwd": ".",
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "source": "baseline",
            }
        )
    return {
        "status": "ready" if checks else "not_verifiable",
        "checks": checks,
        "limitations": (
            [] if checks else ["No directly executable baseline-owned tests were discovered."]
        ),
    }


def _copy_directory_baseline(
    destination: Path,
    baseline: Mapping[str, Any],
) -> None:
    repository = _absolute_no_follow(str(baseline.get("repository", "")))
    raw_files = baseline.get("before_files", [])
    if not isinstance(raw_files, list):
        return
    if not raw_files:
        return
    try:
        materialize_directory_baseline(
            {
                "source_root": str(repository),
                "before_files": raw_files,
            },
            destination,
        )
    except FileSelectionError as error:
        raise VerificationError(str(error)) from error


def _materialize_baseline(
    destination: Path,
    baseline: Mapping[str, Any],
) -> None:
    kind = baseline.get("kind")
    repository = _absolute_no_follow(str(baseline.get("repository", "")))
    commit = baseline.get("commit")
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "git_commit" and isinstance(commit, str):
        template = destination.parent / f"{destination.name}-template"
        template.mkdir()
        _git(
            destination.parent,
            "clone",
            "--quiet",
            "--shared",
            "--no-checkout",
            f"--template={template}",
            "--",
            str(repository),
            str(destination),
        )
        _git(destination, "checkout", "--quiet", "--detach", commit)
        return
    _copy_directory_baseline(destination, baseline)
    _git(destination, "init", "--quiet")
    _git(destination, "config", "user.email", "bakeoff@localhost")
    _git(destination, "config", "user.name", "Codex Bakeoff")
    _git(destination, "config", "core.autocrlf", "false")
    _git(destination, "config", "core.safecrlf", "false")
    info = destination / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text(
        "* -filter -ident -text -working-tree-encoding\n",
        encoding="utf-8",
    )
    _git(destination, "add", "-f", "-A")
    _git(destination, "commit", "--quiet", "--allow-empty", "-m", "baseline")


def _apply_patch(root: Path, patch: str | None) -> str | None:
    if patch is None:
        return "No attributable candidate patch was available."
    if not patch.strip():
        return None
    result = _git(
        root,
        "apply",
        "--binary",
        "--whitespace=nowarn",
        "-",
        input_text=patch,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Patch application failed."
        return detail[:2_000]
    return None


def _environment(home: Path) -> dict[str, str]:
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "SYSTEMROOT",
            "TMPDIR",
        }
    }
    return {
        **allowed,
        "HOME": str(home),
        "CI": "1",
        "NO_COLOR": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }


def _run_check(root: Path, check: Mapping[str, Any]) -> dict[str, Any]:
    command = check.get("command")
    cwd = _safe_relative(check.get("cwd", "."))
    timeout = check.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
        or cwd is None
        or not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout < 1
        or timeout > 14_400
    ):
        return {
            "id": check.get("id", "unknown"),
            "status": "not_verifiable",
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": "The discovered check is invalid.",
        }
    working_directory = root if cwd == "." else root / cwd
    if not working_directory.is_dir():
        return {
            "id": check.get("id", "unknown"),
            "status": "not_verifiable",
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": "The check working directory is absent.",
        }
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_environment(root / ".bakeoff-home"),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        status = "timed_out" if isinstance(error, subprocess.TimeoutExpired) else "not_verifiable"
        return {
            "id": check.get("id", "unknown"),
            "status": status,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(error)[:MAX_LOG_CHARS],
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }
    return {
        "id": check.get("id", "unknown"),
        "status": "passed" if completed.returncode == 0 else "failed",
        "command": command,
        "command_text": shlex.join(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-MAX_LOG_CHARS:],
        "stderr": completed.stderr[-MAX_LOG_CHARS:],
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def _side_status(checks: Sequence[Mapping[str, Any]]) -> str:
    statuses = {str(item.get("status")) for item in checks}
    if not checks or "not_verifiable" in statuses:
        return "not_verifiable"
    if "timed_out" in statuses:
        return "timed_out"
    if "failed" in statuses:
        return "failed"
    return "passed"


def verify_candidates(
    *,
    baseline: Mapping[str, Any],
    candidate_patches: Mapping[str, str | None],
    checks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one currently discovered check set against three disposable copies."""

    discovery = discover_checks(baseline)
    selected = list(checks if checks is not None else discovery["checks"])
    if not selected:
        return {
            "status": "not_verifiable",
            "checks": [],
            "baseline": {"status": "not_verifiable", "checks": []},
            "candidates": {
                provider: {"status": "not_verifiable", "checks": []}
                for provider in ("claude", "codex")
            },
            "limitations": discovery["limitations"],
        }

    with tempfile.TemporaryDirectory(prefix="codex-bakeoff-verify-") as raw:
        temporary = Path(raw)
        roots = {
            "baseline": temporary / "baseline",
            "claude": temporary / "claude",
            "codex": temporary / "codex",
        }
        for root in roots.values():
            _materialize_baseline(root, baseline)

        limitations: list[str] = []
        patch_errors: dict[str, str | None] = {}
        for provider in ("claude", "codex"):
            patch_errors[provider] = _apply_patch(roots[provider], candidate_patches.get(provider))
            if patch_errors[provider]:
                limitations.append(f"{provider.title()}: {patch_errors[provider]}")

        baseline_checks = [_run_check(roots["baseline"], check) for check in selected]
        baseline_statuses = {
            str(item.get("id")): str(item.get("status")) for item in baseline_checks
        }
        candidates: dict[str, Any] = {}
        for provider in ("claude", "codex"):
            if patch_errors[provider]:
                candidates[provider] = {"status": "not_verifiable", "checks": []}
                continue
            results = [_run_check(roots[provider], check) for check in selected]
            for result in results:
                result["baseline_status"] = baseline_statuses.get(
                    str(result.get("id")), "not_verifiable"
                )
                result["regression"] = (
                    result["baseline_status"] == "passed" and result.get("status") != "passed"
                )
            candidates[provider] = {
                "status": _side_status(results),
                "checks": results,
            }

        baseline_result = {
            "status": _side_status(baseline_checks),
            "checks": baseline_checks,
        }
        status = (
            "completed"
            if baseline_result["status"] != "not_verifiable"
            and all(
                candidates[provider]["status"] != "not_verifiable"
                for provider in ("claude", "codex")
            )
            else "not_verifiable"
        )
        return {
            "status": status,
            "checks": selected,
            "baseline": baseline_result,
            "candidates": candidates,
            "limitations": limitations,
        }


def has_comparable_verification_results(verification: Mapping[str, Any]) -> bool:
    if verification.get("status") != "completed":
        return False
    candidates = verification.get("candidates")
    if not isinstance(candidates, Mapping):
        return False
    return all(
        isinstance(candidates.get(provider), Mapping) and bool(candidates[provider].get("checks"))
        for provider in ("claude", "codex")
    )


def verification_evidence_for_provider(
    verification: Mapping[str, Any],
    provider: str,
) -> tuple[str, ...]:
    candidates = verification.get("candidates")
    candidate = candidates.get(provider) if isinstance(candidates, Mapping) else None
    checks = candidate.get("checks") if isinstance(candidate, Mapping) else None
    if not isinstance(checks, list) or not checks:
        return ("Shared tests: not verifiable.",)
    evidence: list[str] = []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        command = check.get("command")
        command_text = (
            shlex.join(command)
            if isinstance(command, list) and all(isinstance(item, str) for item in command)
            else "unavailable"
        )
        evidence.append(
            f"Shared test {check.get('id', 'unknown')}: "
            f"{check.get('status', 'not_verifiable')}; "
            f"baseline: {check.get('baseline_status', 'not_verifiable')}; "
            f"command: {command_text}"
        )
    return tuple(evidence)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
