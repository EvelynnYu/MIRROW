"""Deterministic formatting at the proactive-message output boundary."""
import re
from mirrow_core.persona import get_ai_name, get_user_name


def strip_leading_assistant_label(content):
    return re.sub(r'^\s*(?:' + re.escape(get_ai_name()) + r'|AI|K)\s*[:：]\s*', '', str(content or ''), count=1).strip()


def clean_proactive_output(content):
    text = str(content or '').strip()
    previous = None
    while text and previous != text:
        previous = text
        text = re.sub(r'^\s*[\[【［](?:(?:🛡️|💭|⏰)\s*)?\d{1,2}:\d{2}[\]】］]\s*', '', text)
        text = strip_leading_assistant_label(text)
    return text.strip('"').strip("'").strip()


def format_push_history_line(role, content):
    text = str(content or '').strip()
    if role not in {'user', 'assistant'} or not text:
        return text
    speaker = get_user_name() if role == 'user' else get_ai_name()
    return f'{speaker}：{text}'
