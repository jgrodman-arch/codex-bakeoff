from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = PLUGIN_ROOT / "scripts" / "historical_discovery.py"
MODULE_SPEC = importlib.util.spec_from_file_location("historical_discovery", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
discovery = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = discovery
MODULE_SPEC.loader.exec_module(discovery)


class HistoricalDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.ledger = self.root / "imports.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def user(self, uuid: str, text: str, timestamp: str) -> dict:
        return {
            "type": "user",
            "uuid": uuid,
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": timestamp,
            "message": {"role": "user", "content": text},
        }

    def assistant(self, uuid: str, text: str, timestamp: str) -> dict:
        return {
            "type": "assistant",
            "uuid": uuid,
            "sessionId": "claude-session",
            "cwd": str(self.project),
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [{"type": "text", "text": text}],
            },
        }

    def write_transcript(self, name: str, events: list[dict]) -> Path:
        path = self.root / f"{name}.jsonl"
        path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return path

    def record(self, source: Path, *, thread: str, imported_at: int) -> dict:
        return {
            "source_path": str(source),
            "content_sha256": "0" * 64,
            "imported_thread_id": thread,
            "imported_at": imported_at,
            "source_modified_at": imported_at - 1,
            "connector_names": [],
        }

    def write_ledger(self, records: list[dict]) -> None:
        self.ledger.write_text(json.dumps({"records": records}), encoding="utf-8")

    def select(self, events: list[dict]) -> tuple[dict, dict]:
        source = self.write_transcript("selected", events)
        self.write_ledger([self.record(source, thread="thread-1", imported_at=100)])
        session = discovery.list_imported_sessions(self.ledger)[0]
        return session, discovery.build_thread_task(session)

    def test_sessions_sort_by_original_creation_time(self) -> None:
        older = self.write_transcript(
            "older",
            [self.user("old", "old request", "2026-01-01T10:00:00Z")],
        )
        newer = self.write_transcript(
            "newer",
            [self.user("new", "new request", "2026-01-02T10:00:00Z")],
        )
        self.write_ledger(
            [
                self.record(newer, thread="new", imported_at=100),
                self.record(older, thread="old", imported_at=200),
            ]
        )

        sessions = discovery.list_imported_sessions(self.ledger)

        self.assertEqual([session["imported_thread_id"] for session in sessions], ["new", "old"])

    def test_duplicate_import_uses_latest_record_without_hash_validation(self) -> None:
        source = self.write_transcript(
            "duplicate",
            [self.user("u1", "request", "2026-01-01T10:00:00Z")],
        )
        self.write_ledger(
            [
                self.record(source, thread="older", imported_at=100),
                self.record(source, thread="newer", imported_at=200),
            ]
        )

        sessions = discovery.list_imported_sessions(self.ledger)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["imported_thread_id"], "newer")
        self.assertNotIn("source_hash_status", sessions[0])
        self.assertNotIn("source_sha256", sessions[0])

    def test_whole_thread_replay_combines_user_requests(self) -> None:
        session, task = self.select(
            [
                self.user("u1", "build it", "2026-01-01T10:00:00Z"),
                self.assistant("a1", "working", "2026-01-01T10:01:00Z"),
                self.user("u2", "make it blue", "2026-01-01T10:02:00Z"),
                self.assistant("a2", "done", "2026-01-01T10:03:00Z"),
            ]
        )

        replay = discovery.build_replay_spec(session, task)

        self.assertEqual(task["request"], "build it\n\nmake it blue")
        self.assertEqual(replay["request"], task["request"])
        self.assertEqual(replay["message_uuids"], ["u1", "u2"])
        self.assertEqual(replay["preceding_context"], [])
        self.assertNotIn("source_sha256", replay)
        self.assertNotIn("configuration_fingerprint", replay)

    def test_source_changes_do_not_invalidate_replay(self) -> None:
        session, task = self.select([self.user("u1", "request", "2026-01-01T10:00:00Z")])
        replay = discovery.build_replay_spec(session, task)
        source = Path(replay["source_path"])
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(self.assistant("a1", "later observation", "2026-01-01T10:01:00Z")) + "\n"
            )

        current = discovery.validate_replay_sources(replay)

        self.assertEqual(current["source_path"], str(source))
        self.assertEqual(current["linked_sources"], [])
        self.assertEqual(
            discovery.recover_historical_final_response(
                source,
                task["message_uuid"],
                whole_thread=True,
            ),
            "later observation",
        )


if __name__ == "__main__":
    unittest.main()
