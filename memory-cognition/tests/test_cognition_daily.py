"""The published cognition day-end entry point runs without MIRROW services."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from cognition import books, daily, maintenance
from cognition.maintenance_sources import sources
from cognition.public_router import create_router


class CognitionDailyTests(unittest.IsolatedAsyncioTestCase):
    async def test_wander_activity_is_self_evidence_not_human_statement(self):
        rows = [{
            "message_id": "activity-1", "role": "system", "event_type": "wander_activity",
            "content": "这次阅读让我觉得很安静。", "timestamp": "2026-09-24T10:00:00+08:00",
            "tool_calls": [{"description": "阅读一篇文章"}],
        }]
        self.assertEqual(sources(rows), [])
        evidence = sources(rows, include_wander_activities=True)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["source_kind"], "wander_activity")
        self.assertEqual(evidence[0]["speaker"], "agent")
        self.assertIn("阅读一篇文章", evidence[0]["text"])

    async def test_three_lane_day_end_runs_with_injected_model(self):
        rows = [{
            "message_id": "chat-1", "role": "user", "content": "今天想聊聊家里的植物。",
            "timestamp": "2026-09-24T11:00:00+08:00", "session_id": "synthetic-session",
        }, {
            "message_id": "activity-1", "role": "system", "event_type": "wander_activity",
            "content": "看完植物图鉴后很平静。", "timestamp": "2026-09-24T11:05:00+08:00",
            "tool_calls": [{"description": "阅读植物图鉴"}], "session_id": "synthetic-session",
        }]
        prompts = []

        async def fake_model(prompt):
            prompts.append(prompt)
            if "长期主观认识整理过程" in prompt:
                return json.dumps({"entities": [], "entries": []})
            if "其他实体的长期主观认识分析器" in prompt:
                return json.dumps({"queries": [], "entities": [], "patches": [], "observations": []})
            if "domain=world" in prompt:
                return json.dumps({"proposals": []})
            raise AssertionError("unexpected model call")

        with tempfile.TemporaryDirectory() as temp_root, patch.object(books, "ROOT", Path(temp_root)):
            try:
                result = await daily.run_daily(
                    rows, fake_model, source_date="2026-09-24", session_id="synthetic-session"
                )
            except books.BookError:
                self.fail(f"day-end failed: {maintenance.status()}; prompts: {[p[:90] for p in prompts]}")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["lanes"], {
                "world": "completed", "self": "completed", "other": "completed"
            })
            self.assertTrue(any("source_kind\": \"wander_activity" in p for p in prompts))
            self.assertEqual(books.catalog("self"), [])


class PublicRouterTests(unittest.TestCase):
    def test_protected_day_end_and_books(self):
        foreign_rows = [False]

        def authorize(x_local_token: str = Header("")):
            if x_local_token != "synthetic-only":
                raise HTTPException(401, "unauthorized")

        async def load_day(source_date, session_id):
            self.assertEqual(source_date, "2026-09-24")
            self.assertEqual(session_id, "synthetic-session")
            return [{"message_id": "chat-1", "role": "user", "content": "今天聊植物。",
                     "session_id": "foreign-session" if foreign_rows[0] else session_id,
                     "timestamp": "2026-09-24T11:00:00+08:00"}]

        async def fake_model(prompt):
            if "domain=world" in prompt:
                return '{"proposals": []}'
            if "长期主观认识整理过程" in prompt:
                return '{"entities": [], "entries": []}'
            if "其他实体的长期主观认识分析器" in prompt:
                return '{"queries": [], "entities": [], "patches": [], "observations": []}'
            raise AssertionError("unexpected model call")

        with tempfile.TemporaryDirectory() as temp_root, patch.object(books, "ROOT", Path(temp_root)):
            app = FastAPI()
            app.include_router(create_router(authorize=authorize, load_day=load_day,
                                             llm=fake_model, session_id=lambda: "synthetic-session"))
            client = TestClient(app)
            self.assertEqual(client.get("/api/cognition/books/self").status_code, 401)
            headers = {"x-local-token": "synthetic-only"}
            self.assertEqual(client.get("/api/cognition/books/self", headers=headers).json()["entries"], [])
            self.assertEqual(client.post("/api/cognition/maintenance/run", headers=headers,
                                         json={"source_date": "2026-9-24"}).status_code, 422)
            foreign_rows[0] = True
            self.assertEqual(client.post("/api/cognition/maintenance/run", headers=headers,
                                         json={"source_date": "2026-09-24"}).status_code, 422)
            foreign_rows[0] = False
            result = client.post("/api/cognition/maintenance/run", headers=headers,
                                 json={"source_date": "2026-09-24"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "completed")
            created = client.put("/api/cognition/books/self/Quiet", headers=headers,
                                 json={"revision": "new", "name": "Quiet", "body": "Synthetic preference.",
                                       "subject_id": "agent"})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(len(client.get("/api/cognition/books/self", headers=headers).json()["entries"]), 1)
            stale = client.put("/api/cognition/books/self/Quiet", headers=headers,
                               json={"revision": "stale", "name": "Quiet", "body": "Wrong overwrite"})
            self.assertEqual(stale.status_code, 409, stale.text)


if __name__ == "__main__":
    unittest.main()
