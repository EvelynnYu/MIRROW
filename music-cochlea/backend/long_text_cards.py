"""长文小卡片的纯数据规则；不调用模型，也不负责持久化。"""
from typing import Any, Dict, Optional
import uuid
import re

MAX_LONG_TEXT_CARD_BODY_LENGTH = 100_000
MAX_LONG_TEXT_CARD_TITLE_LENGTH = 500


def card_message_label(card: Optional[Dict[str, str]]) -> str:
    return "[小红书帖子卡片]" if card and card.get("kind") == "xhs" else "[长文卡片]"


def normalize_long_text_card(value: Any, *, create_id: bool = True) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("长文卡片格式不正确")
    title = str(value.get("title") or "").strip()
    body = str(value.get("body") or "")
    if len(title) > MAX_LONG_TEXT_CARD_TITLE_LENGTH:
        raise ValueError(f"卡片标题不能超过 {MAX_LONG_TEXT_CARD_TITLE_LENGTH} 字符")
    if not body.strip() and value.get("kind") != "xhs":
        raise ValueError("长文卡片正文不能为空")
    if len(body) > MAX_LONG_TEXT_CARD_BODY_LENGTH:
        raise ValueError(f"长文卡片正文不能超过 {MAX_LONG_TEXT_CARD_BODY_LENGTH} 字符")
    card_id = str(value.get("id") or (uuid.uuid4().hex if create_id else ""))
    if not card_id:
        raise ValueError("长文卡片缺少 id")
    if len(card_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", card_id):
        raise ValueError("长文卡片 id 格式不正确")
    card = {"id": card_id, "title": title, "body": body}
    if value.get("kind") == "music":
        from music_system.cards import normalize_music_fields
        card.update(normalize_music_fields(value))
    elif value.get("kind") == "xhs":
        from xhs_posts.links import parse_share_link
        resolved = parse_share_link(str(value.get("link") or body))
        if len(body) > 2000:
            raise ValueError("帖子卡片附言不能超过 2000 字符")
        card.update({"kind": "xhs", "link": resolved["url"],
                     "post_key": resolved["post_key"], "link_status": resolved["link_status"]})
        # Only the owner of a verified K source may attach a read claim.
        card["source_status"] = "owner_link"
    elif value.get("kind"):
        raise ValueError("不支持的卡片类型")
    return card


def card_for_first_turn(card: Optional[Dict[str, str]]) -> str:
    if not card:
        return ""
    if card.get("kind") == "music":
        from music_system.cards import card_context
        return card_context(card)
    if card.get("kind") == "xhs":
        return f"【使用者分享了小红书链接；这不表示 K 已读该帖。{('附言：' + card['body']) if card['body'] else ''} 链接：{card['link']}】"
    title = f"《{card['title']}》" if card["title"] else ""
    return f"【使用者发来了一段长文{title}，内容如下】\n{card['body']}"


def card_for_history(card: Optional[Dict[str, str]], include_body: bool) -> str:
    if not card:
        return ""
    if card.get("kind") == "music":
        from music_system.cards import card_context
        return card_context(card)
    if card.get("kind") == "xhs":
        return f"【使用者此前分享的小红书链接{('《' + card['title'] + '》') if card['title'] else ''}：{card['link']}；未由此证明 K 已读该帖。】"
    title = card["title"]
    if include_body:
        heading = f"使用者此前发送的长文卡片《{title}》" if title else "使用者此前发送的一张无标题长文卡片"
        return f"【{heading}，正文如下】\n{card['body']}"
    if title:
        return f"【使用者此前发送的长文卡片《{title}》。本轮仅提供标题。】"
    return "【使用者此前发送过一张无标题长文卡片，本轮未附带正文。】"
