"""Local UI host. Run from repository root: uvicorn examples.wander_ui:app --host 127.0.0.1

This opens the local module stores, but never starts Wander or calls an LLM.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from wander_manager.public_api import create_wander_ui_router
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.wish_store import WishStore


def create_app(data_dir=None):
    root = Path(__file__).resolve().parents[1]
    directory = Path(data_dir or os.getenv('MIRROW_UI_DATA_DIR') or root / 'events')

    @asynccontextmanager
    async def lifespan(app):
        directory.mkdir(parents=True, exist_ok=True)
        runtime = WanderRuntimeStore(directory / 'wander_runtime.db')
        runtime.initialize()
        wishes = WishStore(str(directory / 'wishes.db'))
        # Put API before optional SPA mount so it cannot be swallowed by static routing.
        app.include_router(create_wander_ui_router(runtime, wishes))
        dist = root / 'wander_frontend' / 'dist'
        if dist.is_dir():
            app.mount('/', StaticFiles(directory=dist, html=True), name='wander-ui')
        yield

    return FastAPI(title='MIRROW Wander UI (local)', lifespan=lifespan)


app = create_app()
