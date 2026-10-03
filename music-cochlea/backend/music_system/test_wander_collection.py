import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from music_system import library


class _Store:
    def __init__(self):
        self.materials = {}

    def material(self, key):
        return self.materials.get(key)

    def save_material(self, key, value):
        self.materials[key] = dict(value)

    def bindings(self):
        return [{"id": "123", "name": "K 的歌单", "subject": "k"},
                {"id": "456", "name": "另一张歌单", "subject": "k"},
                {"id": "567", "name": "共有歌单", "subject": "shared"},
                {"id": "678", "name": "使用者歌单", "subject": "owner"}]


class WanderCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_replayed_song_in_target_playlist_does_not_call_provider(self):
        store = _Store()
        payload = {"netease_song_id": "789", "playlist_ids": ["123"], "title": "重听的歌"}
        with patch.object(library, "get_service", return_value=SimpleNamespace(store=store)), \
                patch.object(library, "change_tracks", new_callable=AsyncMock) as change:
            receipt = await library.apply_wander_action(
                "node-1", payload, {"action": "add_current_song", "playlist_id": "123"})
            retry = await library.apply_wander_action(
                "node-1", payload, {"action": "add_current_song", "playlist_id": "123"})
        change.assert_not_awaited()
        self.assertEqual(receipt, retry)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["outcome"], "already_present_at_selection")
        self.assertFalse(receipt["changed"])

    async def test_membership_in_one_playlist_does_not_block_another(self):
        store = _Store()
        payload = {"netease_song_id": "789", "playlist_ids": ["123"], "title": "这首歌"}
        with patch.object(library, "get_service", return_value=SimpleNamespace(store=store)), \
                patch.object(library, "change_tracks", new_callable=AsyncMock, return_value={
                    "id": "456", "name": "另一张歌单", "change_applied": True,
                }) as change:
            receipt = await library.apply_wander_action(
                "node-2", payload, {"action": "add_current_song", "playlist_id": "456"})
        change.assert_awaited_once()
        self.assertTrue(receipt["changed"])

    async def test_shared_playlist_is_editable_but_aning_and_shared_delete_are_not(self):
        store = _Store()
        payload = {"netease_song_id": "789", "playlist_ids": [], "title": "这首歌"}
        with patch.object(library, "get_service", return_value=SimpleNamespace(store=store)), \
                patch.object(library, "change_tracks", new_callable=AsyncMock, return_value={
                    "id": "567", "name": "共有歌单", "change_applied": True,
                }) as change, \
                patch.object(library, "delete", new_callable=AsyncMock) as delete:
            added = await library.apply_wander_action(
                "shared-add", payload, {"action": "add_current_song", "playlist_id": "567"})
            owner = await library.apply_wander_action(
                "owner-add", payload, {"action": "add_current_song", "playlist_id": "678"})
            shared_delete = await library.apply_wander_action(
                "shared-delete", payload, {"action": "delete_playlist", "playlist_id": "567"})
        change.assert_awaited_once()
        delete.assert_not_awaited()
        self.assertEqual(added["status"], "completed")
        self.assertEqual(added["playlist_subject"], "shared")
        self.assertEqual(owner["status"], "error")
        self.assertEqual(shared_delete["status"], "error")
