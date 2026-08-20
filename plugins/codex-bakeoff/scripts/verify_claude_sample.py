#!/usr/bin/env python3
"""Run objective checks for recorded Claude Code samples without changing their checkout."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TypedDict

URI_TASK_ID = "jqlang__jq-2157"
ARGUMENT_TASK_ID = "jqlang__jq-2919"

_HOMEBREW_PREFIX = Path("/opt/homebrew/opt")
_FULL_LIBRARY_SOURCES = (
    "builtin.c",
    "bytecode.c",
    "compile.c",
    "execute.c",
    "jq_test.c",
    "jv.c",
    "jv_alloc.c",
    "jv_aux.c",
    "jv_dtoa.c",
    "jv_file.c",
    "jv_parse.c",
    "jv_print.c",
    "jv_unicode.c",
    "linker.c",
    "locfile.c",
    "util.c",
    "jv_dtoa_tsd.c",
    "lexer.c",
    "parser.c",
    "decNumber/decContext.c",
    "decNumber/decNumber.c",
)


class BuildResult(TypedDict):
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class CheckResult(TypedDict):
    name: str
    arguments: list[str]
    expected_stdout: str
    stdout: str
    stderr: str
    returncode: int
    passed: bool


class VerificationResult(TypedDict):
    task_id: str
    repository: str
    passed: bool
    build: BuildResult
    checks: list[CheckResult]


@dataclass(frozen=True)
class _Check:
    name: str
    arguments: tuple[str, ...]
    expected_stdout: str
    standard_input: str | None = None


_CHECKS: dict[str, tuple[_Check, ...]] = {
    URI_TASK_ID: (
        _Check(
            name="rfc3986_reserved_characters",
            arguments=("-Rr", "@uri"),
            expected_stdout="-_.~%21%27%28%29%2A",
            standard_input="-_.~!'()*\n",
        ),
        _Check(
            name="unicode_remains_percent_encoded",
            arguments=("-Rr", "@uri"),
            expected_stdout="%CE%BC",
            standard_input="μ\n",
        ),
        _Check(
            name="spaces_remain_percent_encoded",
            arguments=("-Rr", "@uri"),
            expected_stdout="hello%20world",
            standard_input="hello world\n",
        ),
    ),
    ARGUMENT_TASK_ID: (
        _Check(
            name="script_after_separator",
            arguments=("--args", "-rn", "--", "$ARGS.positional[0]", "bar"),
            expected_stdout="bar",
        ),
        _Check(
            name="script_before_separator",
            arguments=("--args", "-rn", "1", "--", "$ARGS.positional[0]", "bar"),
            expected_stdout="1",
        ),
        _Check(
            name="plain_filter_after_separator",
            arguments=("-n", "--", "1"),
            expected_stdout="1",
        ),
    ),
}


def _prepare_generated_files(repository: Path, build_directory: Path) -> None:
    generated = build_directory / "src"
    generated.mkdir()
    (generated / "version.h").write_text(
        '#define JQ_VERSION "sample-verification"\n', encoding="utf-8"
    )
    (generated / "config_opts.inc").write_text(
        '#define JQ_CONFIG "sample-verification"\n', encoding="utf-8"
    )

    builtins = repository / "src" / "builtin.jq"
    if builtins.is_file():
        lines = (
            '"' + line.replace("\\", "\\\\").replace('"', '\\"') + '\\n"'
            for line in builtins.read_text(encoding="utf-8").splitlines()
        )
        (generated / "builtin.inc").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_command(task_id: str, repository: Path, build_directory: Path) -> list[str]:
    command = [
        "cc",
        "-std=gnu11",
        f"-I{build_directory}",
        f"-I{repository}",
        f"-I{repository / 'src'}",
    ]

    if task_id == URI_TASK_ID:
        oniguruma = _HOMEBREW_PREFIX / "oniguruma"
        command.extend(
            (
                "-DHAVE_LIBONIG",
                "-DHAVE_STRPTIME",
                "-DIEEE_8087",
                f"-I{oniguruma / 'include'}",
            )
        )
        command.extend(
            str(repository / "src" / source)
            for source in _FULL_LIBRARY_SOURCES
            if (repository / "src" / source).is_file()
        )
        command.extend((str(repository / "src" / "main.c"), f"-L{oniguruma / 'lib'}", "-lonig"))
    else:
        command.extend(
            (
                str(repository / "src" / "main.c"),
                f"-L{_HOMEBREW_PREFIX / 'jq' / 'lib'}",
                "-ljq",
            )
        )

    command.extend(("-lm", "-o", str(build_directory / "jq")))
    return command


def _build_jq(task_id: str, repository: Path, build_directory: Path) -> BuildResult:
    command = _build_command(task_id, repository, build_directory)
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as error:
        return {"command": command, "returncode": 127, "stdout": "", "stderr": str(error)}

    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_check(executable: Path, check: _Check) -> CheckResult:
    completed = subprocess.run(
        [str(executable), *check.arguments],
        input=check.standard_input,
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = completed.stdout.rstrip("\n")
    return {
        "name": check.name,
        "arguments": list(check.arguments),
        "expected_stdout": check.expected_stdout,
        "stdout": stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
        "passed": completed.returncode == 0 and stdout == check.expected_stdout,
    }


def verify_task(task_id: str, repository: Path | str) -> VerificationResult:
    """Compile and verify a task using generated files outside its repository."""
    if task_id not in _CHECKS:
        raise ValueError(f"Unsupported Claude Code sample task: {task_id}")

    repository_path = Path(repository).resolve()
    if not repository_path.is_dir():
        raise ValueError(f"Claude Code sample repository does not exist: {repository_path}")

    with tempfile.TemporaryDirectory(prefix="codex-bakeoff-sample-verification-") as temporary:
        build_directory = Path(temporary)
        _prepare_generated_files(repository_path, build_directory)
        build = _build_jq(task_id, repository_path, build_directory)
        checks = (
            [_run_check(build_directory / "jq", check) for check in _CHECKS[task_id]]
            if build["returncode"] == 0
            else []
        )

    return {
        "task_id": task_id,
        "repository": str(repository_path),
        "passed": build["returncode"] == 0 and all(check["passed"] for check in checks),
        "build": build,
        "checks": checks,
    }


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=tuple(_CHECKS))
    parser.add_argument("--repository", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    result = verify_task(parsed.task, parsed.repository)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
