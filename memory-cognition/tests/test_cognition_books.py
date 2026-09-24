"""Synthetic cognition-book checks; no private entries are read."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cognition import books


class CognitionBookTests(unittest.TestCase):
    def test_entry_has_revision_and_rejects_stale_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(books, "ROOT", Path(temporary)):
                created = books.save(
                    "self",
                    "偏好安静",
                    {
                        "revision": "new",
                        "name": "偏好安静",
                        "body": "智能体发现自己在安静环境里更容易专注。",
                        "subject_id": "agent",
                    },
                )
                self.assertEqual("agent", created["subject_id"])
                self.assertEqual(1, len(books.catalog("self")))
                with self.assertRaises(books.RevisionConflict):
                    books.save(
                        "self",
                        "偏好安静",
                        {"revision": "stale", "name": "偏好安静", "body": "覆盖旧认识"},
                    )
                self.assertEqual(created["revision"], books.catalog("self")[0]["revision"])


if __name__ == "__main__":
    unittest.main()
