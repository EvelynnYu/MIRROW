"""SCP 百科 API 路由 — 从 main.py 提取"""

import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scp", tags=["scp"])


@router.get("/entries")
async def get_scp_entries():
    """返回 SCP 百科全部词条（供前端百科页面使用）"""
    entries = []
    try:
        from scp_encyclopedia import get_global_scp_encyclopedia
        enc = get_global_scp_encyclopedia()
        order = []
        for fn, entry in enc._entries.items():
            grp = entry.group
            if entry.granularity == "core":
                if grp == "A":
                    order.append((0, entry.name, entry))
                elif grp == "B":
                    order.append((1, entry.name, entry))
                else:
                    order.append((2, entry.name, entry))
            else:
                order.append((3, entry.name, entry))
        order.sort(key=lambda x: (x[0], x[1]))
        for _, _, entry in order:
            related = []
            for line in entry.body.split("\n"):
                if line.startswith("## 相关词条"):
                    continue
                if line.startswith("- [["):
                    start = line.find("[[") + 2
                    end = line.find("]]")
                    if start > 1 and end > start:
                        related.append(line[start:end])
            entries.append({
                "name": entry.name, "aliases": entry.aliases,
                "category": entry.category, "granularity": entry.granularity,
                "group": entry.group, "body": entry.body, "related": related,
            })
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"获取SCP词条失败: {e}")
    return {"entries": entries}
