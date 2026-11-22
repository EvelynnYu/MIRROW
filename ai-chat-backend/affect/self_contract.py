"""Optional AI-owned self report, separate from factual context ingredients."""

FEELING_NAME_INSTRUCTION = (
    "每种心情使用一个稳定的短情绪词，通常 2–8 字；"
    "具体经过、对象、比喻和行动打算放入原因字段。同一种感受延续时沿用原名；同一主体、同一对象的同名心情按一次延续记录，类别随本次感受更新。"
    "family 选择一个最贴近的类别：愉悦/亲近/安定/兴趣/低落/担忧/恼怒/排斥/惊讶/复杂未归类；"
    "确实难以归类时使用复杂未归类。"
)

CURRENT_FEELING_EVIDENCE_INSTRUCTION = (
    "延续心情的记录以本轮真实的新触动为依据，原因字段描述这次触动。"
    "intensity 是记录当时的实际强度绝对值，persistence 是当前感受的余留时效；同名延续本身不表示强度增加。"
    "此前活跃日的心绪及其缘由是历史背景；仅在上下文中再次呈现它们不构成新的情绪证据。"
    "旧事在本轮确实重新引发感受时，记录这次变化及本轮原因。"
)

SELF_WORD_LIST_INSTRUCTION = (
    "新增或延续多种心情时，每种写一个词，用逗号分隔，共用本次参数。"
)


def _operations(name, reason):
    return (
        f"operation 取 add（新增）、reinforce（延续）、reappraise（修改）或 resolve（结束）。"
        f"修改一条时，previous_feeling 填原名，{name} 填修改后的名称或沿用原名，{reason} 和 intensity 填当前原因和强度。"
        f"结束一条时，{name} 填原名。同名记录用 target 区分对象。"
    )


_SELF_FIELDS = (
    "family 选愉悦/亲近/安定/兴趣/低落/担忧/恼怒/排斥/惊讶/复杂未归类之一；"
    "intensity 填当前强度（0 到 1 的数字），persistence 填 momentary/situational/lingering，target 可填感受对象。"
)

WANDER_FEELING_INSTRUCTION = (
    "本次活动确实触动你时，在 emotion_effect 用 to 填心情词，delta 填这次的缘由。"
    + SELF_WORD_LIST_INSTRUCTION + _operations("to", "delta") + _SELF_FIELDS
    + CURRENT_FEELING_EVIDENCE_INSTRUCTION
)

SELF_SIDECAR_INSTRUCTION = (
    "【AI自身情绪 Sidecar｜仅本轮动态尾段】\n"
    "只记录你自己的感受，不代写人类伙伴的心情。本轮有新触动，包括仍然牵动你的旧感受，可在最终自然语言末尾追加一个 "
    "⟦MIRROW:AFFECT⟧{JSON}⟦/MIRROW:AFFECT⟧。两标记之间只写一个 JSON 对象，内外均不加 Markdown 代码围栏。工具轮或没有变化时省略。\n"
    "feeling 填一个简短心情词，reason 填这轮缘由。" + SELF_WORD_LIST_INSTRUCTION
    + _operations("feeling", "reason") + _SELF_FIELDS + CURRENT_FEELING_EVIDENCE_INSTRUCTION
)
