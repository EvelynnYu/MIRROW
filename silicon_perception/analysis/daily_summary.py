"""日终身体数据摘要 — 供日记 prompt 注入

从 daily_health_summary 表查询指定日期的身体数据，
格式化为自然语言摘要。无 LLM 调用，纯数据拼接。
"""

from typing import Optional


async def build_sentinel_diary_section(target_date: str) -> Optional[str]:
    """查询今日哨兵数据，返回自然语言摘要字符串。无数据时返回 None。"""
    try:
        from silicon_perception.recording.health_store import get_store
        store = get_store()
        if not store:
            return None

        rows = store.get_daily_summaries(target_date, target_date)
        if not rows:
            return None
        row = rows[0]

        parts = []

        # 步数
        steps = row.get("steps")
        if steps and steps > 0:
            part = f"走了{steps}步"
            avg_7d = row.get("avg_steps_7d")
            if avg_7d and avg_7d > 0:
                pct = int(steps / avg_7d * 100)
                part += f"（7日均值{int(avg_7d)}的{pct}%）"
            parts.append(part)

        # 心率
        hr_avg = row.get("hr_avg")
        hr_min = row.get("hr_min")
        hr_max = row.get("hr_max")
        if hr_avg and hr_avg > 0:
            parts.append(f"心率约{int(hr_avg)}（{hr_min or '?'}-{hr_max or '?'}）")

        # 睡眠（含子项）
        sleep_min = row.get("sleep_min")
        if sleep_min and sleep_min > 0:
            h = sleep_min // 60
            m = sleep_min % 60
            sleep_str = f"睡眠约{h}小时{m}分钟"
            # 子项展开（仅存在时）
            deep = row.get("deep_sleep_min")
            shallow = row.get("shallow_sleep_min")
            rem = row.get("rem_sleep_min")
            awake = row.get("awake_min")
            score = row.get("sleep_score")
            if any(v is not None for v in [deep, shallow, rem, awake, score]):
                detail_parts = []
                if deep is not None: detail_parts.append(f"深睡{deep}min")
                if shallow is not None: detail_parts.append(f"浅睡{shallow}min")
                if rem is not None: detail_parts.append(f"REM {rem}min")
                if awake is not None: detail_parts.append(f"清醒{awake}min")
                if score is not None: detail_parts.append(f"评分{score}")
                sleep_str += f"（{'·'.join(detail_parts)}）"
            parts.append(sleep_str)

        # PC 活跃
        pc_active = row.get("pc_active_min", 0) or 0
        if pc_active > 30:
            h = pc_active // 60
            m = pc_active % 60
            parts.append(f"PC活跃约{h}小时{m}分钟")

        # GPS 位置变化链
        gps_chain = row.get("gps_chain")
        if gps_chain:
            parts.append(f"位置：{gps_chain}")

        # 天气
        weather_temp = row.get("weather_temp")
        weather_desc = row.get("weather_desc")
        if weather_temp is not None:
            w_str = f"天气{weather_desc or ''} {weather_temp}°C"
            if row.get("weather_humidity") is not None:
                w_str += f" 湿度{row['weather_humidity']}%"
            parts.append(w_str)

        if not parts:
            return None

        return "【今日身体数据】" + "；".join(parts) + "。"
    except Exception:
        return None
