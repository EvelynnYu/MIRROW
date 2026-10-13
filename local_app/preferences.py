"""Validated local lifecycle preferences; no production defaults or accounts."""
from datetime import time
from pydantic import BaseModel, Field, field_validator


class Preferences(BaseModel):
    covenant_word: str = Field(default='', max_length=80)
    window_start: str = '22:00'
    window_end: str = '03:00'
    wait_minutes: int = Field(default=30, ge=1, le=1440)
    day_change_hour: int = Field(default=6, ge=0, le=23)
    automatic_digest: bool = True
    autonomous_wander: bool = False
    idle_minutes: int = Field(default=15, ge=1, le=1440)
    sentinel_enabled: bool = False

    @field_validator('window_start', 'window_end')
    @classmethod
    def clock_format(cls, value):
        parsed = time.fromisoformat(value)
        if parsed.strftime('%H:%M') != value:
            raise ValueError('use HH:MM')
        return value

    @field_validator('covenant_word')
    @classmethod
    def trim_word(cls, value):
        return value.strip()


def in_window(now, start, end):
    current = now.strftime('%H:%M')
    return start <= current <= end if start < end else current >= start or current <= end
