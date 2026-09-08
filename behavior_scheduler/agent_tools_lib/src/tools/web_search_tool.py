# selfprompter/tools/web_search_tool.py
"""
Web search tool using direct HTTP requests and BeautifulSoup.
Multi-engine fallback: Sogou → 360 → Bing → Baidu.

增强（2026-07-11）：不再只回搜索结果页快照片段——搜完自动点进前 N 条结果页抓正文
（trafilatura 提取，回退 readability → bs4），并支持对指定 URL 的深读。

Tavily（2026-07-11）：SEARCH_PROVIDER=tavily 且有 TAVILY_API_KEY 时，走 Tavily
search+extract（搜索+读正文一体，含 raw_content 正文）；失败自动回退内置免费引擎。
"""

import os
import logging
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import Dict, Any, List, Optional, Tuple
from .tool_base import Tool

logger = logging.getLogger(__name__)

class WebSearchTool(Tool):
    """Tool for performing web searches via direct HTTP requests."""

    single_use = True  # 搜索结果注入上下文后，Pro 不应重复调用

    _REFERENCE_DOMAINS = {
        "baike.baidu.com", "baike.sogou.com", "baike.so.com",
        "zdic.net", "hanyu.baidu.com", "dict.cn", "dict.baidu.com",
        "cidian.baidu.com", "chinesehelper.cn", "xh.5156edu.com",
    }

    # 搜索引擎自身域名：/link 跳转会重定向到真实页（保留），但停留在引擎域名的
    # 提示/相关搜索链接是噪声，抓到后按 final_url 丢弃。
    _ENGINE_DOMAINS = ("sogou.com", "so.com", "bing.com", "baidu.com")

    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return ("Search the web and read the actual result pages. "
                "Returns result snippets plus the extracted main text of the top pages. "
                "Pass a full URL as query to deep-read that single page.")

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query, or a full http(s) URL to deep-read that page"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of search results to list",
                    "default": 3
                },
                "fetch_pages": {
                    "type": "integer",
                    "description": "How many top result pages to open and read the full text of (0 = snippets only)",
                    "default": 2
                }
            },
            "required": ["query"]
        }

    def run(self, input: Dict[str, Any]) -> Dict[str, Any]:
        try:
            query = input.get("query")
            if not query:
                return {"type": "tool_response", "content": "Error: Query is required"}

            if query.startswith(('http://', 'https://')):
                return self._fetch_url(query)

            max_results = min(max(1, input.get("max_results", 3)), 10)
            fetch_pages = min(max(0, input.get("fetch_pages", 2)), 5)

            # Tavily 优先（search+extract 一体），失败自动回退内置免费引擎
            provider = os.getenv("SEARCH_PROVIDER", "builtin").strip().lower()
            tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
            if provider == "tavily" and tavily_key:
                try:
                    return self._search_tavily(query, max_results, tavily_key)
                except Exception as e:
                    logger.warning(f"Tavily 搜索失败，回退内置引擎: {type(e).__name__}: {e}")

            return self._search(query, max_results, fetch_pages)
        except Exception as e:
            return {"type": "tool_response", "content": f"Error: {str(e)}"}

    # ── Tavily（搜索+读正文一体）────────────────────────────

    def _search_tavily(self, query: str, max_results: int, api_key: str) -> Dict[str, Any]:
        """Tavily search：返回 AI 摘要 + 带正文(raw_content)的结果。中文可用、不翻墙。"""
        resp = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "query": query,
                "search_depth": "basic",       # basic=1 credit；够用
                "include_answer": True,
                "include_raw_content": True,    # 直接带正文，省去二次抓取
                "max_results": max(3, max_results),
            },
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", []) or []
        if not results:
            return {"type": "tool_response", "content": f'Tavily 未找到与 "{query}" 相关的结果。'}

        lines = []
        answer = (data.get("answer") or "").strip()
        if answer:
            lines.append(f"【摘要】{answer}\n")
        for i, r in enumerate(results[:max_results], 1):
            lines.append(f"{i}. {r.get('title', '')}")
            lines.append(f"   URL: {r.get('url', '')}")
            c = (r.get('content') or '').strip()
            if c:
                lines.append(f"   {c[:200]}")

        # 正文摘录（raw_content，取前 2 条有正文的）
        blocks = []
        for r in results:
            if len(blocks) >= 2:
                break
            rc = (r.get('raw_content') or '').strip()
            if rc:
                blocks.append(f"【正文摘录】{r.get('title', '')}\nURL: {r.get('url', '')}\n{rc[:1200]}")
        content = "\n".join(lines)
        if blocks:
            content += "\n\n以下是页面正文：\n\n" + "\n\n".join(blocks)
        return {"type": "tool_response", "content": content}

    # ── 正文提取 ────────────────────────────────────────────

    def _extract_main_text(self, html: str, url: str = "") -> str:
        """从 HTML 提取正文：trafilatura → readability-lxml → bs4 朴素兜底。"""
        # 1) trafilatura（质量最好）
        try:
            import trafilatura
            extracted = trafilatura.extract(
                html, url=url or None,
                include_comments=False, include_tables=False,
            )
            if extracted and extracted.strip():
                return extracted
        except Exception:
            pass
        # 2) readability-lxml
        try:
            from readability import Document
            summary_html = Document(html).summary()
            soup = BeautifulSoup(summary_html, 'html.parser')
            t = soup.get_text(separator='\n', strip=True)
            if t and t.strip():
                return t
        except Exception:
            pass
        # 3) bs4 朴素兜底
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
                tag.decompose()
            return soup.get_text(separator='\n', strip=True)
        except Exception:
            return ""

    def _extract_page(self, url: str, char_cap: int = 1200) -> Tuple[Optional[str], str, Optional[str]]:
        """抓取并提取单个页面正文。返回 (text|None, final_url, error|None)。
        自动跟随重定向（Sogou/Baidu 的跳转链接），并修正乱码编码。"""
        try:
            resp = requests.get(url, headers=self.headers, timeout=10, allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            return None, url, f"抓取失败: {type(e).__name__}"
        final_url = str(resp.url)
        # 编码修正：requests 对无 charset 的中文页常误判为 ISO-8859-1
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            try:
                resp.encoding = resp.apparent_encoding
            except Exception:
                pass
        html = resp.text
        text = (self._extract_main_text(html, final_url) or "").strip()
        if not text:
            return None, final_url, "未提取到正文"
        if len(text) > char_cap:
            text = text[:char_cap] + "…"
        return text, final_url, None

    def _fetch_url(self, url: str) -> Dict[str, Any]:
        """深读单个 URL（query 以 http 开头时）。"""
        text, final_url, err = self._extract_page(url, char_cap=3000)
        if not text:
            return {"type": "tool_response", "content": f"抓取失败或未提取到正文：{url}（{err or ''}）"}
        return {"type": "tool_response", "content": f"URL: {final_url}\n\n正文：\n{text}"}

    def _fetch_top_pages(self, results: list, n: int) -> str:
        """点进前 n 条（优先非百科）结果页，抓正文拼成「正文摘录」块。"""
        if n <= 0 or not results:
            return ""
        normal = [r for r in results if not any(
            d in (r.get("url") or "") for d in self._REFERENCE_DOMAINS)]
        candidates = (normal or results)
        blocks = []
        for r in candidates:
            if len(blocks) >= n:
                break
            url = r.get("url", "")
            if not url.startswith(("http://", "https://")):
                continue
            text, final_url, err = self._extract_page(url)
            # 丢弃仍停留在搜索引擎域名的链接（提示/相关搜索等噪声）
            if text and any(d in final_url for d in self._ENGINE_DOMAINS):
                logger.info(f"深读跳过引擎内链: {final_url}")
                continue
            if text:
                blocks.append(f"【正文摘录】{r.get('title', '')}\nURL: {final_url}\n{text}")
            else:
                logger.info(f"深读跳过 {url}: {err}")
        if not blocks:
            return ""
        return "以下是点进部分结果页读到的正文：\n\n" + "\n\n".join(blocks)

    # ── 搜索 ────────────────────────────────────────────────

    def _search(self, query: str, max_results: int, fetch_pages: int = 2) -> Dict[str, Any]:
        logger.info(f"Search query: '{query}'")
        results = self._try_engines(query, max_results)

        if not results:
            logger.warning(f"No results for '{query}'")
            return {
                "type": "tool_response",
                "content": f'未找到与 "{query}" 相关的结果。可以尝试提供具体的 URL 直接抓取。'
            }

        if self._all_reference(results):
            logger.info(f"All reference results for '{query}', retrying with quotes")
            retry = self._try_engines(f'"{query}"', max_results)
            if retry and not self._all_reference(retry):
                results = retry

        logger.info(f"Returning {len(results)} results for '{query}'")
        content = self._format_results(results)

        # 点进前 N 条结果页抓正文（核心增强）
        try:
            fetched = self._fetch_top_pages(results, fetch_pages)
            if fetched:
                content = content + "\n" + fetched
        except Exception as e:
            logger.warning(f"深读结果页失败（非致命）: {e}")

        return {"type": "tool_response", "content": content}

    def _try_engines(self, query: str, max_results: int) -> list:
        engines = [
            ("Sogou", self._search_sogou),
            ("360", self._search_360),
            ("Bing", self._search_bing),
            ("Baidu", self._search_baidu),
        ]
        for name, fn in engines:
            try:
                results = fn(query, max_results)
                if results:
                    ref_count = sum(1 for r in results if any(
                        d in (r.get("url") or "") for d in self._REFERENCE_DOMAINS))
                    logger.info(f"{name}: {len(results)} results ({ref_count} reference)")
                    if not self._all_reference(results):
                        return results
                    logger.info(f"{name}: all reference, trying next engine")
                else:
                    logger.info(f"{name}: no results")
            except Exception as e:
                logger.warning(f"{name} failed: {type(e).__name__}: {e}")
                continue
        return []

    def _all_reference(self, results: list) -> bool:
        if not results:
            return False
        return all(
            any(d in (r.get("url") or "") for d in self._REFERENCE_DOMAINS)
            for r in results
        )

    def _parse_results(self, containers, title_selectors, snippet_selectors,
                       max_results: int, base_url: str = "") -> list:
        """Generic HTML result parser with fallback selectors。
        base_url 用于把引擎相对/跳转链接解析为绝对 URL（供后续点进抓正文）。"""
        results = []
        for item in containers[:max_results]:
            title_el = None
            for sel in title_selectors:
                title_el = item.select_one(sel)
                if title_el:
                    break
            if not title_el:
                continue

            snippet_el = None
            for sel in snippet_selectors:
                snippet_el = item.select_one(sel)
                if snippet_el:
                    break

            href = title_el.get("href", "") or ""
            if href and base_url:
                try:
                    href = urljoin(base_url, href)
                except Exception:
                    pass
            results.append({
                "title": title_el.get_text(strip=True),
                "url": href,
                "snippet": (snippet_el.get_text(strip=True)[:200] if snippet_el else "")
            })
        return results

    # ── engines ──────────────────────────────────────────────

    def _search_sogou(self, query: str, max_results: int) -> list:
        """Search via Sogou (primary Chinese engine)."""
        response = requests.get(
            "https://www.sogou.com/web",
            params={"query": query},
            headers=self.headers,
            timeout=15
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        return self._parse_results(
            soup.select(".vrwrap"),
            ["h3 a", "a[class*='title']", "h3", "a"],
            [".star-wiki", ".fz-mid", ".space-txt", "p", "div[class*='abstract']"],
            max_results,
            base_url="https://www.sogou.com",
        )

    def _search_360(self, query: str, max_results: int) -> list:
        """Search via 360 so.com (secondary Chinese engine)."""
        response = requests.get(
            "https://www.so.com/s",
            params={"q": query},
            headers=self.headers,
            timeout=15
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        return self._parse_results(
            soup.select("li.res-list"),
            ["h3 a", "a[class*='title']", "h3", "a"],
            [".res-desc", ".res-rich", "p", "div[class*='desc']"],
            max_results,
            base_url="https://www.so.com",
        )

    def _search_bing(self, query: str, max_results: int) -> list:
        """Search via Bing (international fallback)."""
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max_results},
            headers={**self.headers, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=15
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        return self._parse_results(
            soup.select("li.b_algo"),
            ["h2 a", "a"],
            [".b_caption p", "p"],
            max_results,
            base_url="https://www.bing.com",
        )

    def _search_baidu(self, query: str, max_results: int) -> list:
        """Search via Baidu HTTP (last resort — often captcha-blocked)."""
        response = requests.get(
            "http://www.baidu.com/s",
            params={"wd": query, "rn": max_results},
            headers=self.headers,
            timeout=15
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        return self._parse_results(
            soup.select(".result, .result-op, div.c-container"),
            ["h3 a", "a[class*='title']", "h3", "a"],
            [".c-abstract", ".c-span-last", ".content-right_8Zs40",
             "div[class*='content']", "span[class*='abstract']", "p"],
            max_results,
            base_url="https://www.baidu.com",
        )

    # ── formatting ──────────────────────────────────────────

    def _format_results(self, results: List[Dict[str, str]]) -> str:
        if not results:
            return "No results found."

        normal_results = []
        reference_results = []
        for r in results:
            url = r.get("url", "")
            if any(d in url for d in self._REFERENCE_DOMAINS):
                reference_results.append(r)
            else:
                normal_results.append(r)

        displayed = normal_results if normal_results else reference_results

        formatted = []
        for i, result in enumerate(displayed, 1):
            url = result.get("url", "")
            tag = " [百科/词典]" if any(d in url for d in self._REFERENCE_DOMAINS) else ""
            formatted.append(f"{i}. {result['title']}{tag}")
            formatted.append(f"   URL: {result['url']}")
            formatted.append(f"   {result['snippet']}")
            formatted.append("")

        return "\n".join(formatted)
