import { useEffect, useRef, useState } from 'react';
import { getApiBase } from '../config';
import './SocialPersonCard.css';
import RemoteSocialAvatar from './RemoteSocialAvatar';

export type SocialPerson = {
  actor_id:string;
  name:string;
  nickname:string;
  avatar:string;
  remark:string;
  cognition_bound?:boolean;
  remark_locked?:boolean;
};

const imageSource = (value:string) => value.startsWith('/api/social-feed/') ? `${getApiBase()}${value}` : value;

export default function SocialPersonCard({ actorId, onClose, onSaved, remote }:{
  actorId:string;
  onClose:()=>void;
  onSaved:(person:SocialPerson)=>void;
  remote?:{base:string;origin:string};
}) {
  const base = remote?.base || `${getApiBase()}/api/social-feed`;
  const headers = remote ? {'X-MIRROW-Lounge-Admin':'1'} : undefined;
  const dialog = useRef<HTMLDialogElement>(null);
  const [person, setPerson] = useState<SocialPerson|null>(null);
  const [remark, setRemark] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    let current = true;
    fetch(`${base}/people/${encodeURIComponent(actorId)}`, {cache:'no-store', headers})
      .then(async response => {
        if (!response.ok) throw new Error(`身份卡读取失败 (${response.status})`);
        return response.json() as Promise<{person:SocialPerson}>;
      })
      .then(result => { if (current) { setPerson(result.person); setRemark(result.person.remark || ''); } })
      .catch(reason => { if (current) setError(reason instanceof Error ? reason.message : '身份卡读取失败'); });
    return () => { current = false; node?.close(); };
  }, [actorId, base]);

  async function save() {
    if (!person || person.remark_locked || saving) return;
    setSaving(true);
    setError('');
    try {
      const response = await fetch(`${base}/people/${encodeURIComponent(actorId)}/remark`, {
        method:'PUT', headers:{...headers, 'Content-Type':'application/json'}, body:JSON.stringify({remark:remark.trim()}),
      });
      if (!response.ok) throw new Error(`备注保存失败 (${response.status})`);
      const result = await response.json() as {person:SocialPerson};
      setPerson(result.person);
      setRemark(result.person.remark || '');
      onSaved(result.person);
      onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '备注保存失败'); }
    finally { setSaving(false); }
  }

  return <dialog ref={dialog} className="social-person-card" aria-label="好友身份卡"
    onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="social-person-card-inner">
      <header><h3>身份卡</h3><button type="button" onClick={onClose} aria-label="关闭身份卡">×</button></header>
      {person ? <>
        <div className="social-person-card-avatar">
          {remote ? <RemoteSocialAvatar {...remote} actor={actorId} avatar={person.avatar} name={person.nickname || person.name} /> : person.avatar && (person.avatar.startsWith('https://') || person.avatar.startsWith('/'))
            ? <img src={imageSource(person.avatar)} alt={`${person.nickname}头像`} />
            : <span>{person.avatar || person.nickname.slice(0, 1) || '友'}</span>}
        </div>
        <strong className="social-person-card-name">{person.nickname}</strong>
        <p className="social-person-card-hint">头像和网名由对方设置。{person.remark_locked ? '本家认知已关联；显示称呼锁定为实体主名。' : person.cognition_bound === false ? '本家尚未关联认知；不影响对方的 Key 身份。' : ''}</p>
        <label htmlFor="social-person-remark">{person.remark_locked ? '本家认知主名 · 在认知书维护' : remote ? '我在这家给 TA 的备注 · 仅此登录身份可见' : '我给 TA 的备注 · 只在本家可见'}</label>
        <input id="social-person-remark" maxLength={40} value={remark} onChange={event => setRemark(event.target.value)} placeholder="留空则只显示网名" disabled={person.remark_locked} />
        {!person.remark_locked && <div className="social-person-card-actions"><button type="button" onClick={() => void save()} disabled={saving}>{saving ? '保存中…' : '保存备注'}</button></div>}
      </> : !error && <p className="social-person-card-hint">正在读取身份卡…</p>}
      {error && <p className="social-person-card-error" role="alert">{error}</p>}
    </div>
  </dialog>;
}
