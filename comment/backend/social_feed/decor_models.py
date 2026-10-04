"""Portable decoration data: preset tokens and owned assets, never arbitrary CSS."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class MusicReference(StrictModel):
    id: str = Field(pattern=r'^[0-9]{1,20}$')
    title: str = Field(max_length=200)
    artist: str = Field(default='', max_length=200)


class Theme(StrictModel):
    preset: Literal['cream', 'rose', 'ink', 'forest', 'ocean', 'lavender', 'amber', 'graphite', 'aurora', 'blueprint', 'sakura'] = 'cream'
    font: Literal['sans', 'serif', 'hand', 'calligraphy', 'flower', 'neon', 'outline', 'gold'] = 'sans'
    text_size: Literal['small', 'normal', 'large'] = 'normal'
    accent: str = Field(default='#b97862', pattern=r'^#[0-9a-fA-F]{6}$')
    background: str = Field(default='', max_length=80)
    background_fit: Literal['width', 'cover', 'tile'] = 'width'
    card_background: str = Field(default='', max_length=80)
    music: str = Field(default='', max_length=80)
    music_title: str = Field(default='', max_length=200)
    music_track: MusicReference | None = None
    shelf_name: str = Field(default='家里的奇物架', max_length=40)


class Exhibit(StrictModel):
    id: str = Field(default='', max_length=40)
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=500)
    image: str = Field(default='', max_length=80)
    gift_image: str = Field(default='', max_length=80)
    human_note: str = Field(default='', max_length=200)
    ai_note: str = Field(default='', max_length=200)


class HomeDesign(StrictModel):
    revision: int = Field(default=0, ge=0)
    theme: Theme = Field(default_factory=Theme)
    exhibits: list[Exhibit] = Field(default_factory=list, max_length=5)


class HomeEdit(HomeDesign):
    sync_gift_exhibits: list[str] = Field(default_factory=list, max_length=5)


class PersonalDesign(StrictModel):
    frame: Literal['none', 'orbit', 'lace', 'spark', 'comet', 'ribbon', 'halo', 'petal', 'circuit', 'polar', 'diamond'] = 'none'
    frame_asset: str = Field(default='', max_length=80)
    card_asset: str = Field(default='', max_length=80)
    card: Literal['inherit', 'paper', 'rose', 'ink'] = 'inherit'
    font: Literal['inherit', 'sans', 'serif', 'hand', 'calligraphy', 'flower', 'neon', 'outline', 'gold'] = 'inherit'


class SyncChoice(StrictModel):
    scope: Literal['local', 'all', 'selected'] = 'local'
    site_ids: list[str] = Field(default_factory=list, max_length=50)


class GiftChoice(StrictModel):
    kind: Literal['human', 'ai']
    exhibit_id: str = Field(default='', max_length=40)
    request_id: str = Field(min_length=8, max_length=80, pattern=r'^[a-zA-Z0-9_-]+$')


class DraftRequest(StrictModel):
    facts: str = Field(min_length=1, max_length=1500)
    current: Exhibit
