"""Exercise the downloadable illustration contract without a provider or live store."""
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from wander_manager import essay_illustration as artwork
from wander_manager.event_handlers import BrowseBookmarksHandler
from wander_manager.reading_illustrations import bind, entry


class IllustrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_receives_scene_and_only_pixels_are_saved(self):
        mock = types.ModuleType("sample_illustrator")
        def draw(scene):
            self.assertEqual(scene, "一条纸船")
            out = io.BytesIO()
            meta = PngImagePlugin.PngInfo()
            meta.add_text("Comment", "synthetic-metadata-marker")
            Image.new("RGBA", (2, 3), (5, 10, 15, 128)).save(out, format="PNG", pnginfo=meta)
            return out.getvalue()
        mock.draw = draw
        with tempfile.TemporaryDirectory() as tmp, patch.object(artwork, "_DIRECTORY", Path(tmp)), \
                patch.dict("sys.modules", {"sample_illustrator": mock}), \
                patch.dict(os.environ, {"MIRROW_ESSAY_IMAGE_ADAPTER": "sample_illustrator:draw"}):
            card = await artwork.render("一条纸船")
            self.assertEqual(card["status"], "ready")
            self.assertRegex(card["url"], r"^/static/essay_illustrations/[a-f0-9]{32}\.png$")
            with Image.open(Path(tmp) / Path(card["url"]).name) as result:
                self.assertEqual(result.getpixel((0, 0)), (5, 10, 15, 128))
                self.assertNotIn("Comment", result.info)

    async def test_missing_adapter_gives_an_honest_receipt(self):
        store = types.SimpleNamespace(merge_node_side_effect_refs=lambda node_id, receipts: receipts)
        with patch.dict(os.environ, {"MIRROW_ESSAY_IMAGE_ADAPTER": ""}):
            receipt = await artwork.attach(store, "sample-node", "纸船")
        self.assertEqual(receipt, {"essay_illustration": {"status": "failed"}})

    def test_essay_passage_beyond_chat_preview_is_actually_read(self):
        content = "雨" * 400 + "纸船靠岸。"
        work = {"source": "essays", "id": "sample-work", "title": "雨天", "content": content}
        row = {"message_id": work["id"], "content": content, "role": "assistant"}
        material = {"source_mode": "essays", "bookshelf_items": [work], "snapshot": [row], "candidates": [row]}
        reader = BrowseBookmarksHandler.__new__(BrowseBookmarksHandler)
        prompt = reader._prompt_material(material)
        self.assertIn(content, prompt)
        self.assertEqual(material["_reading_snapshot"]["messages"][0]["text"], content)
        choice = {"quote": "纸船靠岸。", "scene": "纸船"}
        card = bind(choice, [work])
        self.assertEqual(content[card["start"]:card["end"]], choice["quote"])
        card.update(status="ready", url="/static/essay_illustrations/" + "a"*32 + ".png")
        node = {"node_id":"read", "execution_status":"succeeded", "side_effect_refs":{"reading_illustration":card}}
        public = entry(node)
        self.assertEqual((public["status"], public["url"]), ("ready", card["url"]))
        card["source"] = "other"
        self.assertIsNone(entry(node))
        self.assertEqual(bind(choice, [{**work, "content": content + choice["quote"]}]), {"status": "unmatched"})
