import { useEffect, useRef, useState } from 'react';
import { getApiBase } from '../config';

type DecorRuntime = { mount: (element: HTMLElement, options: Record<string, unknown>) => Promise<() => void> };
const loads = new Map<string, Promise<DecorRuntime>>();
function loadRuntime(base: string) {
  if (!loads.has(base)) loads.set(base, new Promise((resolve, reject) => {
    const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = `${base}/api/social-decor-ui/style`;
    document.head.append(style);
    const script = document.createElement('script'); script.src = `${base}/api/social-decor-ui/script`;
    script.onload = () => {
      const runtime = (window as Window & { MirrowDecor?: DecorRuntime }).MirrowDecor;
      if (runtime) resolve(runtime); else reject(new Error('装饰页面未加载完成'));
    };
    script.onerror = () => { loads.delete(base); reject(new Error('奇物架暂时无法加载，请确认后端已更新')) };
    document.head.append(script);
  }));
  return loads.get(base)!;
}

export default function SocialDecorPanel({ siteId = '', compact = false }: { siteId?: string; compact?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let disposed = false, cleanup: (() => void) | undefined;
    const base = getApiBase();
    void loadRuntime(base).then(async runtime => {
      if (disposed || !ref.current) return;
      const stop = await runtime.mount(ref.current, {
        api: siteId ? `${base}/api/social-sites/${encodeURIComponent(siteId)}/decor` : `${base}/api/social-decor`,
        localApi: `${base}/api/social-decor`,
        localOwner: !siteId,
        headers: { 'X-MIRROW-Lounge-Admin': '1' },
        root: ref.current.closest('.social-feed-page,.social-remote-page'),
      });
      if (disposed) stop(); else cleanup = stop;
    }).catch(e => { if (!disposed) setError(e.message); });
    return () => { disposed = true; cleanup?.(); };
  }, [siteId]);
  // React owns className; retain the runtime's base class on every toggle.
  return <><div ref={ref} className={`md-panel${compact ? ' social-decor-compact' : ''}`} />{error && <p role="status">{error}</p>}</>;
}
