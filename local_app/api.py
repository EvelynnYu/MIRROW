"""Loopback single-user application composition, no production singletons."""
from contextlib import asynccontextmanager
from datetime import date
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from cognition.public_router import create_router as cognition_router
from wander_manager.public_api import create_wander_ui_router
from .config import Config, ROOT
from .store import Store
from .provider import Provider, ModelUnavailable
from .memory import Memory
from .wander import Wander
from .chat import Chat


class Profile(BaseModel):
    human_name: str = Field(default='用户', min_length=1, max_length=80)
    agent_name: str = Field(default='AI', min_length=1, max_length=80)
    persona: str = Field(default='', max_length=50000)
    relationship_started_at: str = ''


class ChatInput(BaseModel):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    content: str = Field(min_length=1, max_length=100000)


class DayInput(BaseModel):
    date: date


def create_app(config=None, transport=None):
    config = config or Config.from_env()
    store = Store(config.data_dir)
    provider = Provider(config, store, transport)
    memory = Memory(config, store, provider)
    wander = Wander(config, store, provider, memory)
    chat = Chat(store, provider, memory, wander)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if wander.task is not None:
            await wander.stop()

    app = FastAPI(title='MIRROW Local', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = dict(store=store, provider=provider, memory=memory, wander=wander, chat=chat)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': 'same_origin_required'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'same_origin_required'}, status_code=403)
        return await call_next(request)

    def require_model():
        if not config.configured:
            raise HTTPException(409, '请在本地 .env 配置模型；密钥只由后端读取')
        if not store.profile()['persona'].strip():
            raise HTTPException(409, '请先保存自己的身份设定')

    @app.exception_handler(ModelUnavailable)
    async def model_failure(request, exc):
        return JSONResponse({'detail': '模型调用未完成；请检查本地配置。消息不会自动重试。'}, status_code=502)

    @app.get('/api/local/state')
    def state():
        return {'configured': config.configured, 'profile': store.profile(), 'messages': store.messages(),
            'unfinished_requests': store.pending_or_failed_requests(),
            'wander': wander.status(), 'capabilities': {'chat': True, 'memory': True, 'wander': True,
                'lounge': False, 'taobao': False}}

    @app.put('/api/local/profile')
    async def profile(body: Profile):
        if body.relationship_started_at:
            try:
                date.fromisoformat(body.relationship_started_at)
            except ValueError:
                raise HTTPException(422, '关系开始日期须为 YYYY-MM-DD') from None
        async with chat.lock:
            await wander.stop()
            store.set_setting('profile', body.model_dump())
        return {'saved': True}

    @app.post('/api/local/chat')
    async def send(body: ChatInput):
        require_model()
        if not body.content.strip():
            raise HTTPException(422, '消息为空')
        try:
            return await chat.send(body.request_id, body.content)
        except ValueError:
            raise HTTPException(409, '请求身份冲突或回复为空；请刷新核对消息') from None

    @app.post('/api/local/wander/{action}')
    async def wander_control(action: str):
        async with chat.lock:
            if action == 'start':
                require_model()
                await wander.start()
            elif action == 'stop':
                await wander.stop()
            else:
                raise HTTPException(404)
        return wander.status()

    @app.get('/api/local/memory')
    async def memory_search(q: str = ''):
        return {'hits': await memory.search(q)}

    @app.post('/api/local/memory/encode')
    async def memory_encode(body: DayInput):
        require_model()
        try:
            return await memory.encode(body.date.isoformat())
        except ModelUnavailable:
            raise
        except Exception as exc:
            from memory_v2.encoding import EncodingContractError
            from memory_v2.overflow import OverflowAssemblyError
            from memory_v2.conversation_source import ConversationSourceError
            code = ('encoding_contract_invalid' if isinstance(exc, EncodingContractError) else
                'overflow_incomplete' if isinstance(exc, OverflowAssemblyError) else
                'source_validation_failed' if isinstance(exc, ConversationSourceError) else
                'completed_day_required' if isinstance(exc, ValueError) else 'memory_failed')
            return JSONResponse({'code': code, 'detail': '记忆整理未完成；已提交批次保留。请检查日期及本地来源，重试会校验并跳过已完成批次。'}, status_code=409)

    @app.get('/api/wander/persona')
    def wander_persona():
        profile = store.profile()
        return {'ai_name': profile['agent_name'], 'user_name': profile['human_name']}

    app.include_router(create_wander_ui_router(wander.store, wander.wishes))

    async def authorize():
        return True  # loopback launcher + same-origin boundary above

    async def load_day(day, session_id):
        if date.fromisoformat(day) >= date.today():
            raise HTTPException(409, '当前日保留原始对话；只整理已结束日期')
        return [row for row in store.messages() if row['active_date'] == day]

    async def cognition_llm(prompt):
        require_model()
        return (await provider.complete([{'role': 'user', 'content': prompt}], internal=True))['content']

    app.include_router(cognition_router(authorize=authorize, load_day=load_day,
        llm=cognition_llm, session_id=lambda: store.session_id))
    dist = ROOT / 'wander_frontend' / 'dist'
    if dist.is_dir():
        app.mount('/', StaticFiles(directory=dist, html=True), name='local-ui')
    return app
