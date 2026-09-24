"""Explicit API experiment: private artifacts only, no book/entity/receipt writes."""
import asyncio
import json
from datetime import datetime
from uuid import uuid4
from . import books, other_daily
from .maintenance_sources import sources

_lock = asyncio.Lock()


def fingerprints():
    paths = [p for domain in books.DOMAINS for p in books.folder(domain).glob('*.md')]
    paths += [books.ROOT / 'data/other_book_entities.json']
    return {p.relative_to(books.ROOT).as_posix(): books.revision(p.read_text('utf-8')) for p in paths if p.exists()}


async def preview(rows, llm, *, source_date, session_id):
    async with _lock:
        target = books.ROOT / 'backups' / ('other_preview_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8])
        target.mkdir(parents=True)
        before = fingerprints()
        calls = 0
        async def traced(prompt):
            nonlocal calls
            calls += 1
            books.atomic_text(target / f'call_{calls}_input.txt', prompt)
            output = await llm(prompt)
            books.atomic_text(target / f'call_{calls}_output.txt', output if isinstance(output,str) else json.dumps(output,ensure_ascii=False))
            return output
        evidence = sources(rows)
        started = datetime.now()
        try:
            analysis = await other_daily.analyse_rows(rows, traced, source_date=source_date, session_id=session_id)
            result = {'status':'completed', 'analysis':analysis}
        except Exception as exc:
            result = {'status':'error','error_type':type(exc).__name__, 'detail': str(exc) if isinstance(exc,books.BookError) else '试分析失败，私有调用快照已保留'}
        result.update(source_date=source_date, rows=len(rows), evidence_count=len(evidence), evidence_chars=sum(len(e['text']) for e in evidence),
                      calls=calls, seconds=round((datetime.now()-started).total_seconds(),1), artifact=target.name,
                      books_unchanged=before==fingerprints())
        books.atomic_text(target/'result.json',json.dumps(result,ensure_ascii=False))
        books.atomic_text(target/'manifest.json',json.dumps(before,ensure_ascii=False))
        return result
