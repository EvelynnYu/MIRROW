"""Cut over legacy cognition channels only after verified data publication."""
import json
from . import books


def completed():
    path = books.ROOT / 'data/cognition_migration.json'
    if not path.exists():
        return False
    # An interrupted migration is not a reason to resume old writers.
    return json.loads(path.read_text('utf-8')).get('status') in {'applying', 'completed'}


class RetiredCognitionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '').rstrip('/')
        retired = (path.startswith('/api/persona/') or path.startswith('/api/world-book/suggestions')
                   or path.startswith('/api/cognition/suggestions')
                   or path in {'/api/music/analyze', '/memory/profile', '/profile/questionnaire',
                               '/api/memory/profile', '/api/profile/questionnaire'})
        retired = retired or (scope.get('method') in {'PUT', 'DELETE', 'POST'} and
                              (path.startswith('/api/self-book/entries/') or path.startswith('/api/world-book/entries/')))
        if scope['type'] == 'http' and retired and completed():
            from starlette.responses import JSONResponse
            await JSONResponse({'detail': '旧认知通道已退役，请使用认知书及更新记录。'}, status_code=410)(scope, receive, send)
            return
        await self.app(scope, receive, send)
