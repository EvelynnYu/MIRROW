import { useEffect, useState } from 'react';
import {remoteAvatarUrl} from '../social/remoteAvatarUrl';

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
      const url=remoteAvatarUrl(base,origin,avatar);
      if(!url)return;
      void fetch(url, { headers: { 'X-MIRROW-Lounge-Admin': '1' } })
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
