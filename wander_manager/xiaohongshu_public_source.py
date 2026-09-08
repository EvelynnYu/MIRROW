"""Anonymous, read-only acquisition of Xiaohongshu public feed cards."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any
from urllib.parse import quote


PUBLIC_FEED_URL = "https://www.xiaohongshu.com/explore"
# Anonymous discovery may be unavailable; hosts may configure a public entry.
# No account-specific note, cookie or captured feed is distributed.
PUBLIC_FEED_BOOTSTRAP_URL = os.getenv("MIRROW_XHS_PUBLIC_FEED_URL") or PUBLIC_FEED_URL


@dataclass(frozen=True)
class PublicPostCard:
    source_id: str
    url: str
    text: str
    cover_url: str = ""
    has_video_cover: bool = False


@dataclass(frozen=True)
class PublicSourceResult:
    status: str
    cards: tuple[PublicPostCard, ...] = ()
    error: str = ""
    source_url: str = PUBLIC_FEED_URL


class PublicXiaohongshuSource:
    """Use installed Edge without cookies, login, posting, liking or scrolling."""

    def __init__(self, timeout_ms: int = 45_000):
        self.timeout_ms = max(10_000, int(timeout_ms))

    async def fetch_cards(self, *, query: str = "", limit: int = 12) -> PublicSourceResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return PublicSourceResult(status="browser_unavailable", error="playwright_not_installed")

        target = (f"https://www.xiaohongshu.com/search_result?keyword={quote(query)}"
                  if query else PUBLIC_FEED_BOOTSTRAP_URL)
        browser = None
        context = None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel="msedge", headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(3000)
                body = (await page.locator("body").inner_text()).lower()
                raw_cards: list[dict[str, Any]] = await page.locator("section.note-item").evaluate_all(
                    """(els, limit) => els.slice(0, limit).map(section => {
                      const noteId = section.dataset.noteId || '';
                      const link = section.querySelector('a.cover[href*="/explore/"]')
                        || section.querySelector('a[href*="/explore/"]');
                      const cover = section.querySelector('img[data-xhs-img]')
                        || section.querySelector('a.cover img');
                      return {
                        note_id: noteId,
                        url: link ? link.href : '',
                        text: (section.innerText || '').trim().slice(0, 1000),
                        cover_url: cover ? (cover.currentSrc || cover.src || '') : '',
                        has_video_cover: Boolean(section.querySelector('.play-icon'))
                      };
                    })""",
                    max(1, min(int(limit), 30)),
                )
        except Exception as exc:
            return PublicSourceResult(status="browser_error", error=type(exc).__name__,
                                      source_url=target)
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

        if not raw_cards:
            wall = "login_required" if ("登录" in body or "验证码" in body) else "no_public_cards"
            return PublicSourceResult(status=wall, error=wall, source_url=target)

        seen: set[str] = set()
        cards: list[PublicPostCard] = []
        for raw in raw_cards:
            note_id = str(raw.get("note_id") or "").strip()
            text = str(raw.get("text") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not note_id or note_id in seen or not text or "/explore/" not in url:
                continue
            seen.add(note_id)
            cards.append(PublicPostCard(
                source_id=note_id,
                url=f"https://www.xiaohongshu.com/explore/{note_id}", text=text,
                cover_url=str(raw.get("cover_url") or "").strip(),
                has_video_cover=bool(raw.get("has_video_cover")),
            ))
        if not cards:
            return PublicSourceResult(status="no_valid_cards", error="no_valid_cards",
                                      source_url=target)
        return PublicSourceResult(status="success", cards=tuple(cards), source_url=target)
