"""Build portable sample configuration at packaging time, never during replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import historical_discovery as discovery

CAPABILITY_FIELDS = (
    "observed_tools",
    "observed_skills",
    "observed_skill_provenance",
    "observed_plugins",
    "task_mutated_claude_skills",
    "observed_instruction_paths",
    "connector_names",
    "configured_connector_names",
)


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def precompute(entry: dict[str, Any], root: Path) -> None:
    """Freeze transcript observations and bind them to the exact packaged artifacts."""
    metadata = {
        k: v for k, v in entry.items() if k not in {"configuration_path", "configuration_sha256"}
    }
    artifacts = [*entry["transcript_parts"], entry["patch_path"], entry["result_path"]]
    result = json.loads((root / entry["result_path"]).read_text())
    with tempfile.TemporaryDirectory(prefix="replay-package-") as temporary:
        transcript = Path(temporary) / "transcript.jsonl"
        transcript.write_bytes(
            b"".join((root / part).read_bytes() for part in entry["transcript_parts"])
        )
        session = {"source_path": str(transcript), "recorded_claude_result": result}
        replay = discovery.build_replay_spec(session, discovery.build_thread_task(session))
        if replay["project_dir"] != entry["original_repository_path_marker"]:
            raise ValueError("Sample working directory must be recorded at the repository root.")
        final_response = discovery.recover_historical_final_response(
            transcript,
            replay["message_uuid"],
            whole_thread=True,
        )
    # These paths and identities are resolved locally, not inferred from the recording.
    for key in (
        "source_path",
        "project_dir",
        "project_dirs",
        "historical_changed_files",
        "prompt_reconstruction_turns",
        "prompt_reconstruction_truncated",
        "linked_sources",
        "request",
        "session_id",
        "imported_thread_id",
        "imported_at",
    ):
        replay.pop(key, None)
    requirements = {key: replay.pop(key, []) for key in CAPABILITY_FIELDS}
    patch = (root / entry["patch_path"]).read_bytes()
    stats = ""
    if patch.strip():
        with tempfile.TemporaryDirectory(prefix="replay-patch-") as temporary:
            stats = subprocess.run(
                ["git", "apply", "--numstat", "-z", "-"],
                input=patch,
                capture_output=True,
                check=True,
                cwd=temporary,
            ).stdout.decode()
    files = sorted({record.split("\t", 2)[2] for record in stats.split("\0") if record})
    configuration = {
        "version": 1,
        "metadata_sha256": canonical_digest(metadata),
        "working_directory": ".",
        "beginning_state": {"kind": "git", "commit": entry["baseline_commit"]},
        "ending_state": {"kind": "git_patch", "patch_path": entry["patch_path"]},
        "attributed_files": files,
        "capability_requirements": requirements,
        "artifact_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in artifacts
        },
        "replay": replay,
        "final_response": final_response,
    }
    path = root / entry["patch_path"]
    path = path.with_name("configuration.json")
    path.write_text(json.dumps(configuration, indent=2, ensure_ascii=False) + "\n")
    entry["configuration_path"] = str(path.relative_to(root))
    entry["configuration_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    args = parser.parse_args()
    catalog = json.loads(args.index.read_text())
    for entry in catalog["samples"]:
        precompute(entry, args.index.parent)
    args.index.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
