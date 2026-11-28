"""Render narrator aliases without rewriting quotations or work titles."""
import re

_HUMAN_LABEL = re.compile(r"(?<![A-Za-z0-9_])Human(?![A-Za-z0-9_])", re.I)
_K = re.compile(r"(?<![A-Za-z0-9_])AI(?![A-Za-z0-9_])")
_CLOSE = {"“": "”", "「": "」", "『": "』", "《": "》", '"': '"'}


def render_aliases(value, *, k_second_person=True):
    text = str(value or "")
    result, narrator, quoted, stack = [], [], [], []

    def flush_narrator():
        chunk = _HUMAN_LABEL.sub("人类伙伴", "".join(narrator))
        result.append(_K.sub("你", chunk) if k_second_person else chunk)
        narrator.clear()

    escaped = False
    for char in text:
        if stack:
            quoted.append(char)
            if not escaped:
                if char == stack[-1]:
                    stack.pop()
                elif char in _CLOSE:
                    stack.append(_CLOSE[char])
            if not stack:
                result.append("".join(quoted))
                quoted.clear()
        elif char in _CLOSE:
            flush_narrator()
            quoted.append(char)
            stack.append(_CLOSE[char])
        else:
            narrator.append(char)
        escaped = char == "\\" and not escaped
    flush_narrator()
    result.extend(quoted)  # An unfinished quote stays literal as well.
    return "".join(result)
