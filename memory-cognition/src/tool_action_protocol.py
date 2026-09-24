"""Model-facing natural-language tool action protocol.

Agent emits one closed action per standalone line at the end of a response::

    @eyes 看看人类伙伴现在在做什么@

The parser only recognizes registered tool names (including a unique prefix of
at least three characters).  A missing closing ``@`` is accepted solely for
the final non-empty line, which keeps tolerance local without turning ordinary
mentions into executable commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable, Sequence


_CLOSED_ACTION_RE = re.compile(
    r"^\s*[@＠]\s*([A-Za-z][A-Za-z0-9_-]*)\s*(.*?)\s*[@＠]\s*$"
)
_OPEN_ACTION_RE = re.compile(
    r"^\s*[@＠]\s*([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*?))?\s*$"
)
_CLOSE_ONLY_ACTION_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*?))?\s*[@＠]\s*$"
)
_TRAILING_CLOSED_ACTION_RE = re.compile(
    r"[@＠]\s*([A-Za-z][A-Za-z0-9_-]*)\s*([^@＠\r\n]*?)\s*[@＠]\s*$"
)
_LEGACY_LINE_RE = re.compile(
    r"^\s*(?:NATURAL[_\s]LANGUAGE\s*[:：]\s*)?"
    r"(?:TOOL_CALL\s*[:：]\s*.*|RESULTS_USED\s*[:：]\s*\[[^\n]*\])\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ToolActionDirective:
    tool: str
    instruction: str = ""
    closed: bool = True


def _peel_registered_closed_suffixes(
    text: str,
    known_tool_names: Iterable[str],
) -> tuple[str, list[ToolActionDirective]]:
    """Peel registered closed directives glued to the response tail.

    The normal contract keeps actions on standalone lines.  This narrow
    compatibility path accepts a missing line break only when the directive
    is closed, sits at the absolute response tail, and resolves to a known
    tool.  Text in the middle of a reply and unknown ``@name ...@`` blocks
    remain ordinary prose.
    """
    remaining = str(text or "").rstrip()
    known = tuple(known_tool_names)
    reversed_actions: list[ToolActionDirective] = []
    while remaining:
        match = _TRAILING_CLOSED_ACTION_RE.search(remaining)
        if not match:
            break
        resolved = resolve_tool_name(match.group(1), known)
        if not resolved:
            break
        reversed_actions.append(ToolActionDirective(
            resolved,
            (match.group(2) or "").strip(),
            True,
        ))
        remaining = remaining[:match.start()].rstrip()
    reversed_actions.reverse()
    return remaining, reversed_actions


def resolve_tool_name(name: str, known_tool_names: Iterable[str]) -> str | None:
    """Resolve an exact/case-insensitive name or one unique 3+ char prefix."""
    raw = str(name or "").strip()
    known = [str(item) for item in known_tool_names if str(item)]
    if raw in known:
        return raw
    case_matches = [item for item in known if item.lower() == raw.lower()]
    if len(case_matches) == 1:
        return case_matches[0]
    if len(raw) >= 3:
        prefix_matches = [item for item in known if item.lower().startswith(raw.lower())]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    return None


def extract_tool_actions(
    content: str,
    known_tool_names: Iterable[str],
) -> list[ToolActionDirective]:
    """Extract the trailing standalone action block from provider output."""
    lines = str(content or "").splitlines()
    if not lines:
        return []
    known = tuple(known_tool_names)
    last_nonempty = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), -1)
    if last_nonempty < 0:
        return []

    found: list[ToolActionDirective] = []
    index = last_nonempty
    first_action = True
    while index >= 0:
        line = lines[index]
        if not line.strip():
            if found:
                index -= 1
                continue
            break
        match = _CLOSED_ACTION_RE.match(line)
        closed = True
        if not match and first_action:
            # Compatibility only: the final standalone line may omit exactly
            # one delimiter.  Registry resolution is mandatory in both cases.
            match = _CLOSE_ONLY_ACTION_RE.match(line) or _OPEN_ACTION_RE.match(line)
            closed = False
        if not match:
            # Provider tolerance: Agent occasionally omits only the line break
            # before an otherwise closed final directive.  Keep this tied to
            # registry resolution and the response tail so inline mentions do
            # not become executable commands.
            _, inline_actions = _peel_registered_closed_suffixes(line, known)
            if inline_actions:
                found.extend(reversed(inline_actions))
            break
        resolved = resolve_tool_name(match.group(1), known)
        if not resolved:
            break
        instruction = (match.group(2) or "").strip()
        found.append(ToolActionDirective(resolved, instruction, closed))
        first_action = False
        index -= 1
    found.reverse()
    return found


def strip_tool_protocol_text(
    content: object,
    known_tool_names: Sequence[str] | None = None,
) -> str:
    """Remove model-only tool protocols from assistant-visible/history text."""
    text = str(content or "")
    # Remove the old paired block atomically before line processing; removing
    # only its tags would leave the JSON body available for later context.
    text = re.sub(
        r"\[TOOL_CALL\].*?\[/TOOL_CALL\]",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    known = tuple(known_tool_names or ())
    if known:
        # Use the same registered-tail rule as extraction.  This is what keeps
        # a tolerated glued directive out of both the live bubble and SQL.
        text, _ = _peel_registered_closed_suffixes(text, known)
    lines = text.splitlines()
    directives = extract_tool_actions(text, known) if known else []
    remove_unclosed = bool(directives and not directives[-1].closed)
    last_nonempty = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), -1)
    cleaned: list[str] = []
    legacy_json_depth: int | None = None
    for index, line in enumerate(lines):
        if legacy_json_depth is not None:
            stripped = line.strip()
            if legacy_json_depth == 0 and not stripped.startswith("{"):
                legacy_json_depth = None
            else:
                legacy_json_depth += line.count("{") - line.count("}")
                if legacy_json_depth <= 0 and "}" in line:
                    legacy_json_depth = None
                continue
        if re.match(r"^\s*TOOL_CALL\s*[:：]", line, flags=re.IGNORECASE):
            after = re.split(r"TOOL_CALL\s*[:：]", line, maxsplit=1, flags=re.IGNORECASE)[-1]
            depth = after.count("{") - after.count("}")
            legacy_json_depth = depth if (not after.strip() or depth > 0) else None
            continue
        if _LEGACY_LINE_RE.match(line):
            continue
        # Closed directives are safe to identify without a registry because
        # both delimiters and the standalone-line contract are present.
        if _CLOSED_ACTION_RE.match(line):
            continue
        if remove_unclosed and index == last_nonempty and (
            _OPEN_ACTION_RE.match(line) or _CLOSE_ONLY_ACTION_RE.match(line)
        ):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)
    text = re.sub(r"NATURAL[_\s]LANGUAGE\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    try:
        bare = json.loads(text.strip())
        if isinstance(bare, dict) and bare.get("tool") and isinstance(bare.get("parameters"), dict):
            return ""
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return text.strip()


__all__ = [
    "ToolActionDirective",
    "extract_tool_actions",
    "resolve_tool_name",
    "strip_tool_protocol_text",
]
