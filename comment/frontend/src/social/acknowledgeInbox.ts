export type InboxReference={id:string;site_id:string};
export const inboxReference=(row:InboxReference)=>JSON.stringify([row.site_id,row.id]);

// Each source confirms independently: an offline friend's wall cannot block home.
export async function acknowledgeInbox<T extends InboxReference>(rows:T[],
  mark:(site:string,ids:string[])=>Promise<unknown>,confirmed:(rows:T[])=>void):Promise<boolean>{
  const sources=new Map<string,T[]>();
  for(const row of rows)sources.set(row.site_id,[...(sources.get(row.site_id)||[]),row]);
  const results=await Promise.allSettled([...sources].map(async([site,items])=>{
    for(let i=0;i<items.length;i+=100){const batch=items.slice(i,i+100);await mark(site,batch.map(row=>row.id));confirmed(batch);}
  }));
  return results.every(result=>result.status==='fulfilled');
}
