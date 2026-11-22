"""Durable Codex questions answered by AI, separate from plan confirmations."""
import hashlib
import json
from .store import now


def save(store, job, questions, source_item):
    if not isinstance(questions,list) or not 1<=len(questions)<=3:
        raise ValueError('bounded_questions_required')
    normalized=[]
    for row in questions:
        title=row.get('title') or row.get('question')
        if not isinstance(title,str) or not title.strip() or len(title)>4000 or row.get('isSecret'):
            raise ValueError('bounded_nonsecret_question_required')
        options=row.get('options') or []
        options=[option.get('label') if isinstance(option,dict) else option for option in options]
        if len(options)>8 or any(not isinstance(option,str) or len(option)>1000 for option in options):
            raise ValueError('bounded_question_options_required')
        normalized.append({'title':title,'options':options})
    identity=json.dumps([job['id'],job['revision'],job['plan_version'],normalized],ensure_ascii=False,sort_keys=True)
    decision_id=hashlib.sha256(identity.encode()).hexdigest()[:24]
    key='decision:'+decision_id
    with store.db() as db:
        old=db.execute('SELECT result FROM commands WHERE key=?',(key,)).fetchone()
        if old:return json.loads(old[0])
        result={'id':decision_id,'job_id':job['id'],'revision':job['revision'],'plan_version':job['plan_version'],
                'status':'pending','questions':normalized,'source_item':source_item,'created_at':now()}
        db.execute('INSERT INTO commands VALUES(?,?)',(key,json.dumps(result,ensure_ascii=False)))
        store._event(db,job['id'],'decision_required',{'revision':job['revision'],'decision_id':decision_id,
            'questions':normalized,'stage':'discussing','source':'codex_structured_question',
            'summary':'工具手提出了待 AI 回答的问题；原方案确认保留，回答后按开工条件继续。'})
    return result


def pending(store, job_id):
    job=store.job(job_id)
    with store.db() as db:
        rows=db.execute("SELECT result FROM commands WHERE key LIKE 'decision:%' ORDER BY rowid").fetchall()
    return [record for row in rows if (record:=json.loads(row[0]))['job_id']==job_id
            and record['revision']==job['revision'] and record['plan_version']==job['plan_version']
            and record['status']=='pending']


def capture(store, executing_job, questions, source_item):
    current=store.job(executing_job['id'])
    bound=current if current['revision']==executing_job['revision'] else executing_job
    return save(store,bound,questions,source_item)


def answered(store, job_id):
    job=store.job(job_id)
    with store.db() as db:
        rows=db.execute("SELECT result FROM commands WHERE key LIKE 'decision:%' ORDER BY rowid DESC").fetchall()
    return [record for row in rows if (record:=json.loads(row[0]))['job_id']==job_id
            and record['revision']==job['revision'] and record['plan_version']==job['plan_version']
            and record['status']=='answered'][:3]


def answer(store, job, args):
    decision_id=args.get('decision_id'); answers=args.get('answers')
    with store.db() as db:
        row=db.execute('SELECT result FROM commands WHERE key=?',('decision:'+str(decision_id),)).fetchone()
        if not row:raise ValueError('current_decision_required')
        record=json.loads(row[0])
        if record['job_id']!=job['id'] or record['revision']!=job['revision'] or record['plan_version']!=job['plan_version']:
            raise ValueError('current_decision_required')
        if record['status']=='answered':return record
        if job['state'] in ('executing','discussing','stopping','accepted','canceled'):
            raise ValueError('wait_for_question_handoff')
        if not isinstance(answers,list) or len(answers)!=len(record['questions']) or any(
                not isinstance(text,str) or not text.strip() or len(text)>4000 for text in answers):
            raise ValueError('answer_each_current_question')
        record.update(status='answered',answers=answers,answered_at=now(),source='k')
        db.execute('UPDATE commands SET result=? WHERE key=?',(json.dumps(record,ensure_ascii=False),'decision:'+decision_id))
        text='\n'.join(question['title']+'\nAI：'+text for question,text in zip(record['questions'],answers))
        db.execute('INSERT INTO messages(job_id,role,body,created_at) VALUES(?,?,?,?)',(job['id'],'k',text,now()))
        queued=job['state']=='awaiting_input' and job['reason']=='k_decision_required'
        db.execute('UPDATE jobs SET pending_text=?,state=?,reason=?,updated_at=? WHERE id=?',
            ((job['pending_text']+'\n[AI 对工具手问题的结构化答复]\n'+text).strip(),
             'queued' if queued else job['state'],'' if queued else job['reason'],now(),job['id']))
        store._event(db,job['id'],'decision_answered',{'revision':job['revision'],'decision_id':decision_id,
            'answers':answers,'source':'k','stage':'discussing','summary':'已保存 AI 的问题答复；按现有确认和开工条件继续，扩大范围仍重新讨论方案。'})
    return record
