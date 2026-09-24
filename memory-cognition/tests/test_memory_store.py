"""The public event store initializes without private conversation data."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from memory_v2.store import MemoryV2Store


class MemoryStoreTests(unittest.TestCase):
    def test_empty_store_has_schema_and_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.db"
            MemoryV2Store(path)
            with closing(sqlite3.connect(path)) as connection:
                version = connection.execute(
                    "SELECT schema_version FROM memory_schema WHERE schema_key='memory_v2'"
                ).fetchone()
                events = connection.execute("SELECT COUNT(*) FROM events").fetchone()
            self.assertIsNotNone(version)
            self.assertEqual(0, events[0])


if __name__ == "__main__":
    unittest.main()
