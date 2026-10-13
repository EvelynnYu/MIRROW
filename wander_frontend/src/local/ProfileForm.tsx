import { useState } from 'react';
import { request, type Profile } from './api';
export default function ProfileForm({profile, onSaved}: {profile: Profile; onSaved: () => Promise<void>}) {
  const [draft, setDraft] = useState(profile);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  return <form className="local-form" onSubmit={async e => {
    e.preventDefault(); setBusy(true); setMessage('');
    try { await request('/api/local/profile', draft, 'PUT'); await onSaved(); setMessage('已保存到本地。'); }
    catch (error) { setMessage((error as Error).message); } finally { setBusy(false); }
  }}><h2>你们自己的设定</h2>
    <label>你的称呼<input value={draft.human_name} required maxLength={80} onChange={e => setDraft({...draft, human_name: e.target.value})}/></label>
    <label>AI 的名字<input value={draft.agent_name} required maxLength={80} onChange={e => setDraft({...draft, agent_name: e.target.value})}/></label>
    <label>身份与人格<textarea rows={8} value={draft.persona} maxLength={50000} onChange={e => setDraft({...draft, persona: e.target.value})}/></label>
    <label>关系开始日期（可留空）<input type="date" value={draft.relationship_started_at} onChange={e => setDraft({...draft, relationship_started_at: e.target.value})}/></label>
    <p>模型地址、模型名和密钥仅填写在后端 .env 文件，页面不会读取或显示密钥。更改设定会停止当前漫想。</p>
    <button disabled={busy}>保存设定</button><p role="status">{message}</p>
  </form>;
}
