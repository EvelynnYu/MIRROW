"""Daily history selection and attributed public-module context providers."""
import inspect
from cognition import books, other_book


class ContextSources:
    def __init__(self, store, days, memory=None):
        self.store, self.days, self.memory = store, days, memory
        self.providers = {}

    def register(self, name, reader):
        """Trusted host-only hook. Return timestamped facts, not prompt rules."""
        if not name or not callable(reader):
            raise ValueError('invalid_context_source')
        self.providers[name] = reader

    def select_history(self, *, exclude_current=False):
        rows = self.store.messages()
        dates = sorted({row['active_date'] for row in rows})
        if exclude_current:
            rows = rows[:-1]
        retained = set(dates[-2:])  # active day and the preceding real conversation day
        diaries = []
        for day in dates[:-2]:
            record = self.days.get(day)
            receipt = self.days.result(day, 'diary')
            jobs = [self.days.result(day, name) for name in ('memory', 'cognition', 'diary')]
            try:
                self.days.verify(day)
                complete = all(job and job['state'] == 'completed' for job in jobs)
            except ValueError:
                complete = False
            if not record or not complete or not receipt:
                retained.add(day)  # no successful replacement: retain original source
            elif day == dates[-3]:
                diaries.append({'active_date': day, 'source_digest': record['source_digest'], **receipt['result']})
        return [row for row in rows if row['active_date'] in retained], diaries

    async def facts(self, query='', *, active_date=None):
        profile = self.store.profile()
        facts = {'human_display_name': profile['human_name'], 'agent_display_name': profile['agent_name'],
            'core_self': books.core_context(),
            'other_cognition': {'core': other_book.context(core=True, interlocutor_ids=['human']),
                'retrieved': other_book.context(interlocutor_ids=['human'], user_message=query)},
            'world_cognition': [entry for entry in books.catalog('world') if entry.get('subject_id') == 'human'
                or any(word and word.casefold() in query.casefold() for word in entry.get('keywords', []))],
            'user_status': self.store.setting('user_status', {'status': 'unknown', 'source': 'unknown'})}
        if profile.get('relationship_started_at'):
            facts['configured_relationship_started_at'] = profile['relationship_started_at']
        if self.memory:
            for day in self.days.list():
                if day['state'] != 'closed' or (active_date and day['date'] >= active_date):
                    continue
                receipt = self.days.result(day['date'], 'memory')
                if not receipt or receipt['state'] != 'completed':
                    continue
                self.days.verify(day['date'])
                settlement_id = (receipt['result'].get('settlement') or {}).get('settlement_id')
                settled = self.memory.events.get_day_settlement(settlement_id) if settlement_id else None
                if settled and settled['active_date'] == day['date']:
                    facts['previous_day_handoff'] = {'active_date': day['date'], 'settlement_id': settlement_id,
                        'compact_items': settled['compact_items']}
                break
        if active_date:
            facts['confirmed_module_events'] = self.days.events(active_date)
            with self.store.connection() as db:
                facts['status_history'] = [dict(r) for r in db.execute(
                    'SELECT * FROM local_status_history WHERE active_date=? ORDER BY id', (active_date,))]
        for name, reader in self.providers.items():
            try:
                value = reader(query=query, active_date=active_date)
                if inspect.isawaitable(value):
                    value = await value
                facts[name] = {'availability': 'available', 'facts': value}
            except Exception:
                facts[name] = {'availability': 'unavailable'}
        return facts


def history_messages(rows):
    from memory_v2.source_projection import memory_source_content
    from mirrow_core.model_provenance import annotate_history
    result = []
    for row in rows:
        if row['role'] not in {'user', 'assistant'} and row.get('event_type') != 'model_switch':
            continue
        projected = annotate_history({**row, 'content': memory_source_content(row) or row['content']})
        result.append({'role': projected['role'],
            'name': projected.get('name', 'mirrow_history_' + row['role']),
            'content': f"[时间：{row['timestamp']}；来源：{row['event_type'] or 'private_chat'}；消息：{row['message_id']}]\n{projected['content']}"})
    return result
