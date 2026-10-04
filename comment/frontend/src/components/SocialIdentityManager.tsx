import { useCallback, useEffect, useRef, useState } from 'react';
import { getApiBase } from '../config';
import SocialProfileLinkPanel from './SocialProfileLinkPanel';
import './SocialIdentityManager.css';

type Subject = 'aning'|'k';
type Person = {nickname?:string; name?:string; avatar?:string; profile_identity?:{linked:boolean; status:string}};
const names = {aning:'站主',k:'AI'};

async function uploadImage(file:File) {
  if (!['image/png','image/jpeg'].includes(file.type) || file.size>10*1024*1024) throw Error('请选择 10 MB 内的 PNG/JPG。');
  const source=await createImageBitmap(file);
  try {
    const scale=Math.min(1,512/Math.max(source.width,source.height));
    const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(source.width*scale));canvas.height=Math.max(1,Math.round(source.height*scale));
    canvas.getContext('2d')!.drawImage(source,0,0,canvas.width,canvas.height);
    const blob=await new Promise<Blob>((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(Error('图片处理失败')),'image/png'));
    if(blob.size>2*1024*1024)throw Error('缩图后仍过大，请换一张照片。');
    return blob;
  } finally {source.close();}
}

export default function SocialIdentityManager({initialSubject,onClose,onSaved,remote=false}:{
  initialSubject?:Subject|null;onClose?:()=>void;onSaved:()=>void;remote?:boolean;
}) {
  const base=getApiBase(),dialog=useRef<HTMLDialogElement>(null),fileInput=useRef<HTMLInputElement>(null);
  const [open,setOpen]=useState(false),[subject,setSubject]=useState<Subject>('aning');
  const [people,setPeople]=useState<Partial<Record<Subject,Person>>>({}),[nickname,setNickname]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[status,setStatus]=useState('');
  const load=useCallback(async()=>{
    const rows=await Promise.all((['aning','k'] as Subject[]).map(async id=>{
      const response=await fetch(`${base}/api/social-feed/people/${id}`,{cache:'no-store'});
      if(!response.ok)throw Error('共域身份资料暂时不可用');
      return [id,(await response.json()).person] as const;
    }));setPeople(Object.fromEntries(rows));
  },[base]);
  useEffect(()=>{let alive=true;void load().catch(e=>{if(alive)setError(e.message);});return()=>{alive=false;};},[load]);
  useEffect(()=>{if(initialSubject){setSubject(initialSubject);setOpen(true);}},[initialSubject]);
  useEffect(()=>{if(open){dialog.current?.showModal();void load().catch(e=>setError(e.message));}return()=>dialog.current?.close();},[open,load]);
  useEffect(()=>{setNickname(people[subject]?.nickname||'');},[people,subject]);
  useEffect(()=>{setStatus('');setError('');},[subject]);
  const close=()=>{if(busy)return;setOpen(false);onClose?.();};
  const person=people[subject],locked=Boolean(person?.profile_identity?.linked);
  const src=(value?:string)=>value?.startsWith('/api/social-feed/')?base+value:value;
  async function save(file?:File){
    setBusy(true);setError('');setStatus('');
    try {
      const response=await fetch(`${base}/api/social-feed/people/${subject}/${file?'avatar':'profile'}`,{
        method:file?'POST':'PUT',headers:{'Content-Type':file?'image/png':'application/json'},
        body:file?await uploadImage(file):JSON.stringify({nickname:nickname.trim()})});
      const result=await response.json();
      if(!response.ok)throw Error(result.detail==='profile_edit_at_source'?'资料已关联，请到资料主站修改或解除关联。':typeof result.detail==='string'?result.detail:'资料保存失败');
      setPeople(old=>({...old,[subject]:result.person}));setStatus(file?'头像已更新。':'网名已更新。');onSaved();
    }catch(e){setError(e instanceof Error?e.message:'资料保存失败');}finally{setBusy(false);}
  }
  return <>
    <button type="button" className="social-identity-entry" aria-label={remote?'管理本家共域资料':'编辑共域头像、网名与资料'} onClick={()=>{setSubject('aning');setOpen(true);}}>
      <span data-social-actor="aning">{people.aning?.avatar?<img src={src(people.aning.avatar)} alt=""/>:<span>{(people.aning?.nickname||names.aning).slice(0,1)}</span>}</span>
      <span className="social-identity-entry-label">{remote?'本家资料':'资料'}</span>
    </button>
    {open&&<dialog ref={dialog} className="social-identity-dialog" aria-label="共域身份资料管理" onCancel={event=>{event.preventDefault();close();}}>
      <header><h3>共域身份资料</h3><button type="button" onClick={close} disabled={busy} aria-label="关闭资料管理">×</button></header>
      <p>头像与主聊天、人设卡独立。这里修改本家公开资料，网站与内站共用。</p>
      {remote&&<p>当前正在朋友家浏览：此处管理本家资料；对方尚未关联的资料仍须在对方网页维护。</p>}
      <div className="social-identity-tabs" role="tablist" aria-label="管理身份">{(['aning','k'] as Subject[]).map(id=><button key={id} type="button" role="tab" aria-selected={subject===id} disabled={busy} onClick={()=>setSubject(id)}>{names[id]}的资料</button>)}</div>
      <div className="social-identity-editor">
        {person?.avatar?<img className="social-identity-picture" src={src(person.avatar)} alt={`${names[subject]}共域头像`}/>:<span className="social-identity-picture">{names[subject].slice(0,1)}</span>}
        {locked&&<p>统一资料来源已关联（{person?.profile_identity?.status}）；请在资料主站修改。</p>}
        <input ref={fileInput} type="file" hidden accept="image/png,image/jpeg" onChange={event=>{const file=event.target.files?.[0];event.target.value='';if(file)void save(file);}}/>
        <button type="button" disabled={busy||locked||!person} onClick={()=>fileInput.current?.click()}>{busy?'保存中…':'上传头像 · PNG/JPG'}</button>
        <label>公开网名<input aria-label="公开网名" maxLength={40} value={nickname} disabled={busy||locked||!person} onChange={event=>setNickname(event.target.value)}/></label>
        <button type="button" disabled={busy||locked||!nickname.trim()||!person} onClick={()=>void save()}>保存网名</button>
      </div>
      {error&&<p role="alert">{error}</p>}{status&&<p role="status">{status}</p>}
      <SocialProfileLinkPanel onChange={()=>{void load().catch(e=>setError(e.message));onSaved();}}/>
    </dialog>}
  </>;
}
