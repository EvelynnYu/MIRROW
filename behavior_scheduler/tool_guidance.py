"""One registry-owned, stable AI-facing usage guide per visible tool.

Provider parameter schemas and extractor descriptions are a separate contract.
This renderer reads only static tool definitions, never mode or live state.
"""
from collections.abc import Mapping


def build_stable_tool_guidance(tools: Mapping) -> str:
    lines = []
    for name, entry in sorted(tools.items()):
        if isinstance(entry, Mapping):
            tool = entry.get("tool")
            visible = getattr(tool, "visible_to_pro", entry.get("visible_to_pro", True))
            guide = getattr(tool, "pro_guide", entry.get("pro_guide", ""))
            brief = getattr(tool, "flash_description", entry.get("flash_description", ""))
            description = getattr(tool, "description", entry.get("description", ""))
        else:
            visible = getattr(entry, "visible_to_pro", True)
            guide = getattr(entry, "pro_guide", "")
            brief = getattr(entry, "flash_description", "")
            description = getattr(entry, "description", "")
        if not visible:
            continue
        text = str(guide or brief or description or "").strip()
        if text:
            # Legacy tools without a usage guide retain a one-line capability.
            text = text if guide else text.splitlines()[0]
            lines.append(f"- `{name}`：{text}")
    return "## 工具使用引导\n\n" + "\n".join(lines) if lines else ""
