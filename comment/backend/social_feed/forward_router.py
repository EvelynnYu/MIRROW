"""Local human forwarding orchestration, mounted under the existing feed."""
import hashlib
import json
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from routers.lounge_reception_router import local_ui
from .forward_action import forward_moment

router = APIRouter(dependencies=[Depends(local_ui)])


class ForwardRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_site_id: str = Field(default='', max_length=100)
    source_moment_id: str = Field(min_length=1, max_length=200)
    site_id: str = Field(default='', max_length=100)
    content: str = Field(default='', max_length=1200)
    visibility: Literal['private', 'public'] = 'private'
    request_id: str = Field(min_length=8, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


@router.post('/forward')
async def create_forward(body: ForwardRequest):
    signature = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    try:
        return await forward_moment(actor='aning', moment_id=body.source_moment_id,
            source_site_id=body.source_site_id, site_id=body.site_id,
            content=body.content, visibility=body.visibility, source_key=signature)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
