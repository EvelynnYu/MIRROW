/** Resolve only a registered home's authenticated PNG routes; never proxy arbitrary URLs. */
export function remoteAvatarUrl(base:string, origin:string, avatar?:string):string {
  try {
    const home=new URL(origin), url=new URL(avatar||'',origin);
    const match=url.pathname.match(/^\/social\/v1\/(profile-links\/)?avatars\/([a-f0-9]{64}\.png)$/);
    if(url.origin!==home.origin||!match||url.username||url.password)return '';
    return `${base}/avatars/${match[2]}${match[1]?'?linked=true':''}`;
  } catch {return '';}
}
