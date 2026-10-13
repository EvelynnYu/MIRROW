import type {SocialSite} from '../pages/SocialRemoteFeed';
import {readTimelineStream,type TimelineEvent,type TimelinePage} from './timelineStream';
import {saveTimelineSnapshot} from './timelineSnapshot';
import {readView,socialViewKey} from './viewState';

// Read-only owner prefetch. No remote arrival, read receipts or persistent bodies.
export const loadSocialFeedPage=()=>import('../pages/SocialFeedPage');
export const timelineSnapshotKey=(api:string,filter:string,sites:SocialSite[])=>
  JSON.stringify([api,filter,sites.map(s=>[s.id,s.origin,s.enabled,s.has_human_key])]);
const headers={'X-MIRROW-Lounge-Admin':'1'};
const sitesCache=new Map<string,{sites:SocialSite[];at:number}>();
const sitesPending=new Map<string,Promise<SocialSite[]>>();
type Flight={api:string;controller:AbortController;promise:Promise<TimelinePage>;listeners:Set<(e:TimelineEvent)=>void>;last?:TimelineEvent};
const flights=new Map<string,Flight>();
const warmed=new Map<string,number>();

export function cachedSocialSites(api:string){
  const entry=sitesCache.get(api);
  return entry&&Date.now()-entry.at<30000?entry.sites:undefined;
}
export function readSocialSites(api:string):Promise<SocialSite[]> {
  const cached=cachedSocialSites(api);if(cached)return Promise.resolve(cached);
  const pending=sitesPending.get(api);if(pending)return pending;
  const promise=fetch(`${api}/api/social-sites`,{headers,cache:'no-store'})
    .then(async r=>{if(!r.ok)throw Error('共域站点列表不可用');return (await r.json()).sites||[];})
    .then((sites:SocialSite[])=>{
      if(sitesPending.get(api)===promise){sitesCache.set(api,{sites,at:Date.now()});while(sitesCache.size>4)sitesCache.delete(sitesCache.keys().next().value!);}
      return sites;
    }).finally(()=>{if(sitesPending.get(api)===promise)sitesPending.delete(api);});
  sitesPending.set(api,promise);return promise;
}

// Cancelling a page stops its own wait, without cancelling a shared background read.
function waitFor<T>(promise:Promise<T>,signal?:AbortSignal):Promise<T>{
  if(!signal)return promise;
  return new Promise((resolve,reject)=>{
    const abort=()=>{signal.removeEventListener('abort',abort);reject(new DOMException('Aborted','AbortError'));};
    if(signal.aborted){abort();return;}
    signal.addEventListener('abort',abort,{once:true});
    promise.then(resolve,reject).finally(()=>signal.removeEventListener('abort',abort));
  });
}

export async function readSocialTimeline(api:string,filter:string,sites:SocialSite[],
  cursor:string|null=null,signal?:AbortSignal,onEvent?:(event:TimelineEvent)=>void,force=false):Promise<TimelinePage>{
  const snapshotKey=timelineSnapshotKey(api,filter,sites),key=JSON.stringify([snapshotKey,cursor]);
  if(force){flights.get(key)?.controller.abort();flights.delete(key);}
  let flight=flights.get(key);
  if(!flight){
    const controller=new AbortController();
    flight={api,controller,listeners:new Set(),promise:undefined!};
    const current=flight;
    const params=new URLSearchParams({limit:'30',visibility_filter:filter});if(cursor)params.set('cursor',cursor);
    const timeout=setTimeout(()=>controller.abort(),25000);
    current.promise=(async()=>{
      const options={headers,cache:'no-store' as RequestCache,signal:controller.signal};
      let response=await fetch(`${api}/api/social-sites/timeline/stream?${params}`,options);
      if([404,405].includes(response.status))response=await fetch(`${api}/api/social-sites/timeline?${params}`,options);
      if(!response.ok)throw Error('时间线暂时不可用，请确认后端已更新。');
      const page:TimelinePage=response.headers.get('Content-Type')?.includes('application/x-ndjson')?
        await readTimelineStream(response,controller.signal,event=>{current.last=event;current.listeners.forEach(fn=>fn(event));}):await response.json();
      if(controller.signal.aborted)throw new DOMException('Aborted','AbortError');
      if(!cursor&&flights.get(key)===current)saveTimelineSnapshot(snapshotKey,page);
      return page;
    })().finally(()=>{clearTimeout(timeout);if(flights.get(key)===current)flights.delete(key);});
    flights.set(key,current);
  }
  if(onEvent){flight.listeners.add(onEvent);if(flight.last)onEvent(flight.last);}
  try{return await waitFor(flight.promise,signal);}
  finally{if(onEvent)flight.listeners.delete(onEvent);}
}

export function invalidateSocialPrewarm(api:string){
  sitesCache.delete(api);sitesPending.delete(api);
  for(const [key,flight] of flights)if(flight.api===api){flight.controller.abort();flights.delete(key);}
  for(const key of warmed.keys())if(JSON.parse(key)[0]===api)warmed.delete(key);
}
export async function prewarmSocial(api:string){
  // Code and registry load alongside notification reads; only the registry precedes timeline.
  void loadSocialFeedPage().catch(()=>{});
  const filter=readView<string>(socialViewKey(api,'human','timeline','filter'),'all');
  const key=JSON.stringify([api,filter]);
  if(Date.now()-(warmed.get(key)||0)<120000)return;
  const stamp=Date.now();warmed.set(key,stamp);while(warmed.size>4)warmed.delete(warmed.keys().next().value!);
  try{const sites=await readSocialSites(api);if(warmed.get(key)===stamp)await readSocialTimeline(api,filter,sites);}
  catch{if(warmed.get(key)===stamp)warmed.delete(key);}
}
