import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from music_system import library
from music_system.selection_catalog import (
    annotate_selection, merge_shelf_candidates, preview_candidates, selected_origin,
)


class SelectionCatalogTests(unittest.TestCase):
    def test_bound_playlists_from_all_owners_are_readable_candidates(self):
        bindings = [
            {"id": "1", "name": "使用者的歌", "subject": "owner"},
            {"id": "2", "name": "K 的歌", "subject": "k"},
            {"id": "3", "name": "一起听", "subject": "shared"},
        ]
        playlists = {
            "playlist:1": {"songs": [{"id": "11", "name": "共同曲", "artist": "歌手", "duration": 180000}]},
            "playlist:2": {"songs": [{"id": "22", "name": "K 曲", "artist": "歌手", "duration": 180000}]},
            "playlist:3": {"songs": [{"id": "11", "name": "共同曲", "artist": "歌手", "duration": 180000}]},
        }
        store = SimpleNamespace(bindings=lambda: bindings, material=lambda key: playlists.get(key))
        with patch.object(library, "get_service", return_value=SimpleNamespace(store=store)):
            songs = asyncio.run(library.candidates())
        self.assertEqual({song["netease_song_id"] for song in songs}, {"11", "22"})
        shared_song = next(song for song in songs if song["netease_song_id"] == "11")
        self.assertEqual({item["subject"] for item in shared_song["playlist_memberships"]}, {"owner", "shared"})

    def test_preview_keeps_all_sources_visible_and_search_is_not_relabelled(self):
        recent = [{"netease_song_id": str(n), "fingerprint": str(n), "title": str(n)} for n in range(40)]
        shelf = [
            {"netease_song_id": "101", "fingerprint": "a", "playlist_memberships": [{"id": "1", "name": "使用者", "subject": "owner"}]},
            {"netease_song_id": "102", "fingerprint": "s", "playlist_memberships": [{"id": "3", "name": "共有", "subject": "shared"}]},
            {"netease_song_id": "103", "fingerprint": "k", "playlist_memberships": [{"id": "2", "name": "K", "subject": "k"}]},
        ]
        merged = merge_shelf_candidates(recent, shelf)
        preview = preview_candidates(merged, recent_fingerprints={"0"}, limit=12)
        self.assertTrue({"101", "102", "103"}.issubset({song["netease_song_id"] for song in preview}))
        self.assertNotIn("0", [song["fingerprint"] for song in preview])
        self.assertEqual(selected_origin({"from_cache": False, "source_playlist_id": "1"}, shelf[0]),
                         ("search_result", None))
        origin, source = selected_origin({"from_cache": True, "source_playlist_id": "1"}, shelf[0])
        self.assertEqual(origin, "owner_playlist_candidate")
        self.assertEqual(source["name"], "使用者")
        searched = annotate_selection({"netease_song_id": "101", "title": "曲"},
                                      {"from_cache": False}, shelf)
        self.assertEqual(searched["selection_origin"], "search_result")
        self.assertEqual(searched["playlist_ids"], ["1"])
        self.assertEqual(searched["source_playlist_id"], "")

    def test_stale_cache_membership_cannot_claim_unbound_playlist(self):
        song = {"netease_song_id": "101", "playlist_memberships": [
            {"id": "old", "name": "已解绑", "subject": "owner"}]}
        selected = annotate_selection(song, {"from_cache": True, "source_playlist_id": "old"}, [])
        self.assertEqual(selected["playlist_memberships"], [])
        self.assertEqual(selected["selection_origin"], "cache_candidate")
