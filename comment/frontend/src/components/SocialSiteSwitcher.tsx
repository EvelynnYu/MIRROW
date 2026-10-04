import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { SocialSite } from '../pages/SocialRemoteFeed';
import './SocialSiteSwitcher.css';

export default function SocialSiteSwitcher({ sites, current = '', onSwitch, compact=false }: {
  sites: SocialSite[]; current?: string; onSwitch: (id: string) => void; compact?:boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const title = current==='__all__'?'所有已注册域':sites.find(site => site.id === current)?.name || '本家';
  const close = () => { dialog.current?.close(); setOpen(false); };
  const select = (id: string) => { close(); onSwitch(id); };
  return <div className={`social-site-switcher ${compact?'compact':''}`}>
    <button type="button" onClick={() => { setOpen(true); requestAnimationFrame(() => dialog.current?.showModal()); }} aria-haspopup="dialog">
      <span className="social-site-mark" aria-hidden="true">⌂</span><span><small>当前共域</small><strong>{title}</strong></span><span className="social-site-switch-hint">换一家 ›</span>
    </button>
    {open && createPortal(<dialog ref={dialog} className="social-site-dialog" aria-label="切换共域" onCancel={close}>
      <header><div><h3>看哪里的动态？</h3><p>所有域合并浏览；进入一家后查看那家的奇物架与访客礼物。</p></div><button type="button" onClick={close} aria-label="关闭">×</button></header>
      <button type="button" className={current==='__all__'?'selected':''} onClick={()=>select('__all__')}><strong>所有已注册域</strong><small>本家与已配置我的 Key 的共域；远端仅公开</small></button>
      <button type="button" className={current ? '' : 'selected'} onClick={() => select('')}><strong>本家</strong><small>本家共域{!current ? ' · 正在这里' : ''}</small></button>
      {sites.map(site => <button type="button" key={site.id} disabled={!site.enabled || !site.has_human_key} className={site.id === current ? 'selected' : ''} onClick={() => select(site.id)}>
        <strong>{site.name}</strong><small>{!site.enabled ? '暂未启用' : !site.has_human_key ? '尚未配置我的 Key' : site.id === current ? '正在这里' : '可前往这家'}</small>
      </button>)}
      {!sites.length && <p>在「点赞之交 → 已注册的共域」添加朋友家。</p>}
    </dialog>, document.body)}
  </div>;
}
