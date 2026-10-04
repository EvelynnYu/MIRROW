import { useEffect, useRef, useState } from 'react';
import { getApiBase } from '../config';

type Runtime = { mount: (element: HTMLElement, options: Record<string, unknown>) => Promise<() => void> };
let pending: Promise<Runtime> | undefined;
function load(base: string) {
  if (!pending) pending = new Promise((resolve, reject) => {
    const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = `${base}/api/social-profile-ui/style`; document.head.append(css);
    const script = document.createElement('script'); script.src = `${base}/api/social-profile-ui/script`;
    script.onload = () => { const value = (window as Window & { MirrowProfileIdentity?: Runtime }).MirrowProfileIdentity;
      if (value) resolve(value); else { pending = undefined; reject(Error('资料关联页面未加载完成')); } };
    script.onerror = () => { pending = undefined; reject(Error('资料关联页面暂未连通')); }; document.head.append(script);
  });
  return pending;
}
export default function SocialProfileLinkPanel({ onChange }: { onChange: () => void }) {
  const ref = useRef<HTMLDivElement>(null), callback = useRef(onChange); callback.current = onChange;
  const [error, setError] = useState('');
  useEffect(() => {
    let disposed = false, stop: (() => void) | undefined;
    const base = getApiBase();
    void load(base).then(async runtime => {
      if (disposed || !ref.current) return;
      const cleanup = await runtime.mount(ref.current, { api: `${base}/api/social-profiles`,
        headers: { 'X-MIRROW-Lounge-Admin': '1' }, onChange: () => callback.current() });
      if (disposed) cleanup(); else stop = cleanup;
    }).catch(e => { if (!disposed) setError(e.message); });
    return () => { disposed = true; stop?.(); };
  }, []);
  return <><div ref={ref} />{error && <small role="status">{error}</small>}</>;
}
