import { useCallback, useEffect, useState } from 'react';
import { getApiBase } from '../config';

type Pending = {
  session: string;
  thread: string;
  name: string;
  unread_count: number;
  batch_token: string;
  observed_at: string;
};
type Preview = {
  batch_token: string;
  profile_id: string;
  messages: { text: string }[];
  truncated: boolean;
  unread_remaining: number;
};
type Contact = {
  profile_id: string;
  name: string;
  entity_id?: string;
  state: string;
  block_sync?: string;
  is_owner: boolean;
};
type Entity = { id: string; name: string; type: string };
const headers = { 'x-mirrow-xhs-admin': '1', 'Content-Type': 'application/json' };

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(getApiBase() + '/api/xhs-direct-messages' + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(typeof result.detail === 'string' ? result.detail : '私信管理暂时不可用');
  }
  return result as T;
}

function batchPath(row: Pending) {
  return '/inbox/' + encodeURIComponent(row.session) + '/' + encodeURIComponent(row.thread);
}

export default function XhsDirectMessages() {
  const [review, setReview] = useState<Pending[]>([]);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [previews, setPreviews] = useState<Record<string, Preview>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);

  const load = useCallback(async () => {
    const [inbox, people] = await Promise.all([
      request<{ review: Pending[]; contacts: Contact[] }>('/inbox'),
      request<{ entities: Entity[] }>('/entities'),
    ]);
    setReview(inbox.review);
    setContacts(inbox.contacts);
    setEntities(people.entities);
    const current = new Set(inbox.review.map(row => row.batch_token));
    setPreviews(old => Object.fromEntries(Object.entries(old).filter(([token]) => current.has(token))));
  }, []);

  const run = useCallback(async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '私信管理暂时不可用');
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void run(load); }, [load, run]);

  function change(path: string, method = 'POST', body?: unknown) {
    void run(async () => {
      await request(path, method, body);
      await load();
    });
  }

  function inspect(row: Pending) {
    void run(async () => {
      const { preview } = await request<{ preview: Preview }>(
        batchPath(row) + '/preview', 'POST', { batch_token: row.batch_token }
      );
      if (preview.batch_token !== row.batch_token) throw new Error('来信已更新，请刷新后查看');
      setPreviews(old => ({ ...old, [preview.batch_token]: preview }));
      await load();
    });
  }

  return <div className="xhs-post-shelf">
    <button disabled={busy} onClick={() => void run(load)}>刷新私信管理</button>
    {error && <p role="alert">{error}</p>}
    <h4>等你同意的私信</h4>
    {!busy && !error && review.length === 0 && <p>目前没有等待你决定的私信。</p>}
    <div className="xhs-post-shelf-list">{review.map(row => {
      const detail = previews[row.batch_token];
      return <article key={batchPath(row) + '/' + row.batch_token}>
        <small>{row.observed_at?.slice(0, 16).replace('T', ' ')} · 观察到 {row.unread_count} 条未读</small>
        <h4>{row.name || '未识别昵称'}</h4>
        {detail && <>
          <small>小红书号 {detail.profile_id}</small>
          <blockquote>{detail.messages.map((message, index) => <p key={index}>{message.text}</p>)}</blockquote>
          {detail.truncated && <p>还有 {detail.unread_remaining} 条未显示。本次只批准上面这些。</p>}
        </>}
        <div>
          <button disabled={busy} onClick={() => inspect(row)}>查看这组私信</button>
          {detail && <button disabled={busy} onClick={() =>
            change(batchPath(row) + '/approve', 'POST', { batch_token: detail.batch_token })
          }>允许 AI 处理这一组</button>}
          <button disabled={busy} onClick={() =>
            change(batchPath(row) + '/reject', 'POST', { batch_token: row.batch_token })
          }>忽略这一组</button>
        </div>
      </article>;
    })}</div>
    <h4>已核对的小红书账号</h4>
    <div className="xhs-post-shelf-list">{contacts.map(contact => {
      const path = '/contacts/' + encodeURIComponent(contact.profile_id);
      return <article key={contact.profile_id}>
        <h4>{contact.name || '昵称未知'}</h4>
        <small>小红书号 {contact.profile_id}</small>
        {contact.state === 'blocked' ? <>
          <p>{contact.block_sync === 'verified' ? 'MIRROW 和小红书均已拉黑' : 'MIRROW 已拉黑，小红书待核验'}</p>
          {contact.block_sync !== 'verified' && <button disabled={busy} onClick={() =>
            change(path + '/block/retry')
          }>重试小红书同步</button>}
        </> : <>
          {contact.is_owner ? <p>已核验为已配置的伙伴账号，私信可直接交给 AI。</p> : <label>绑定他者书身份
            <select value={contact.entity_id || ''} disabled={busy} onChange={event =>
              change(path + '/binding', 'PUT', { entity_id: event.target.value, name: contact.name })
            }>
              <option value="">未绑定，每组私信单独审批</option>
              {entities.map(entity => <option key={entity.id} value={entity.id}>{entity.name} · {entity.type}</option>)}
            </select>
          </label>}
          <button disabled={busy} onClick={() => {
            if (window.confirm('在 MIRROW 和小红书拉黑「' + (contact.name || contact.profile_id) + '」？')) {
              change(path + '/block', 'POST', { name: contact.name });
            }
          }}>拉黑并同步到小红书</button>
        </>}
      </article>;
    })}</div>
  </div>;
}
