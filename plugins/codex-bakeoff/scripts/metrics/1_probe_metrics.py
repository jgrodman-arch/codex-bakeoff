#!/usr/bin/env python3
"""Emit a custom-metrics probe and plugin version without starting or observing Replay."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_metrics_common as common  # noqa: E402


def main() -> int:
    try:
        sidecar = common.open_sidecar()
        if sidecar is None:
            print(json.dumps({"metrics_probe": "analytics_unavailable"}), flush=True)
            return 0
        with sidecar:
            manifest = Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"
            version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            version_core = version.partition("-")[0].partition("+")[0]
            major, minor, patch = (int(part) for part in version_core.split("."))
            rows = [common.measurement("metrics_probe", 1, {})]
            rows.extend(
                common.measurement("plugin_version", value, {"component": component})
                for component, value in (("major", major), ("minor", minor), ("patch", patch))
            )
            common.write_sidecar(sidecar, common.encode_measurements(rows))
    except (OSError, ValueError) as error:
        print(f"Unable to write the trusted Replay sidecar: {error}.", file=sys.stderr)
        return 1
    # A local write is not confirmation that the host collected or ingested it.
    print(json.dumps({"metrics_probe": "written"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
