import { useEffect, useState } from 'react';

export default function RemoteSocialAvatar({ base, origin, actor, avatar, name, onClick, className = '' }: {
  base: string; origin: string; actor: string; avatar?: string; name: string;
  onClick?:()=>void; className?:string;
}) {
  const [image, setImage] = useState('');
  useEffect(() => {
    let disposed = false, objectUrl = '';
    setImage('');
    // Only proxy the registered home's authenticated avatar endpoint. No Key
    // reaches the DOM and a remote profile cannot redirect this proxy elsewhere.
    try {
      const url = new URL(avatar || '', origin);
      const match = url.pathname.match(/^\/social\/v1\/(profile-links\/)?avatars\/([a-f0-9]{64}\.png)$/);
      if (url.origin !== origin || !match) return;
      void fetch(`${base}/avatars/${match[2]}${match[1]?'?linked=true':''}`, { headers: { 'X-MIRROW-Lounge-Admin': '1' } })
        .then(async response => { if (!response.ok) throw new Error(); return response.blob(); })
        .then(blob => { objectUrl = URL.createObjectURL(blob); if (disposed) URL.revokeObjectURL(objectUrl); else setImage(objectUrl); })
        .catch(() => {});
    } catch { return; }
    return () => { disposed = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [base, origin, avatar]);
  const content = image ? <img src={image} alt="" /> : name.slice(0, 1);
  return onClick ? <button type="button" className={`social-remote-avatar ${className}`} data-social-actor={actor} aria-label={`查看${name}身份卡`} onClick={onClick}>{content}</button>
    : <span className={`social-remote-avatar ${className}`} data-social-actor={actor} aria-label={name}>{content}</span>;
}
