"""One service for chat and autonomous publishing."""
from .store import PublishStore,account_config,receipt
from .publisher import execute,NativePublisher

async def request(*,session_id,source_key,action,task_id='',draft=None,store=None,publisher=None):
    if not session_id: raise ValueError('session_id_required')
    if action not in {'preview','publish','status','reconcile'}: raise ValueError('invalid_publication_action')
    store=store or PublishStore()
    if task_id:
        record=store.get(task_id,session_id)
        if not record: raise ValueError('publication_not_found')
        if any((draft or {}).values()) and draft!=record['draft']:
            raise ValueError('frozen_draft_mismatch')
    else:
        if action in {'status','reconcile'}: raise ValueError('task_id_required')
        record=store.create(session_id,source_key,draft,account_config())
    if action=='status': return receipt(record)
    if record['account']!=account_config(): raise ValueError('bound_account_changed')
    if action=='reconcile':
        if record['state'] not in {'submitting','submitted_unverified'}: return receipt(record)
        p=publisher or NativePublisher()
        with await p.device.lease_async('xhs_publication_reconcile'):
            try: evidence=await p.verify(record)
            except Exception: evidence=None
            if evidence:
                record=store.transition(record['task_id'],session_id,{record['state']},'published',receipt=evidence,error='')
        return receipt(record)
    return await execute(store,record,preview=action=='preview',publisher=publisher)

def experience(value):
    if not isinstance(value,dict) or not value.get('task_id'): return ''
    state=value.get('status','unknown')
    verb={'published':'已在平台核验发布','previewed':'已完成发布前预览',
          'submitted_unverified':'已尝试提交，平台发布结果尚未核验',
          'submitting':'提交结果未知','blocked':'准备发布遇到阻碍','draft':'已写成草稿',
          'preparing':'准备过程未确认完成'}.get(state,'发布状态未知')
    return ('AI的小红书创作记录：'+verb+'；任务='+value['task_id']+
        '；账号='+str(value.get('account',{}).get('account_name',''))+
        '\nAI创作的标题：'+str(value.get('title',''))+'\nAI创作的正文：'+str(value.get('body',''))+
        '\nAI创作的文字封面：'+str(value.get('cover_text',''))+
        ('\n执行阻碍：'+value['error'] if value.get('error') else ''))
