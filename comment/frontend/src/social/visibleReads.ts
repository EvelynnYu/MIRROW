// Volatile evidence only. The source wall remains the authority for read receipts.
export type VisibleRead = {site_id:string;moment_id:string;part:'content'|'comment'|'likes';
  comment_id?:string;actors?:string[];through:number;created_at?:number};
export type ReadNotice = {site_id:string;moment_id:string;kind:string;comment_id?:string;actor:string;created_at:number};
export const readEvidence=(site_id:string,moment_id:string,part:VisibleRead['part'],extra:Partial<VisibleRead>={})=>
  JSON.stringify({site_id,moment_id,part,through:Date.now()/1000,...extra});
export function noticeWasVisible(n:ReadNotice,seen:VisibleRead):boolean {
  if(n.site_id!==seen.site_id||n.moment_id!==seen.moment_id||n.created_at>seen.through)return false;
  if(seen.part==='comment')return ['comment','mention'].includes(n.kind)&&Boolean(n.comment_id)&&n.comment_id===seen.comment_id;
  if(seen.part==='likes')return n.kind==='like'&&Boolean(seen.actors?.includes(n.actor));
  return n.kind==='moment'||(n.kind==='mention'&&!n.comment_id);
}

export function observeSocialReads(receive:(seen:VisibleRead)=>void):()=>void {
  const candidates=new Map<HTMLElement,{since:number;value:string;sent:boolean}>();
  const observer=new IntersectionObserver(entries=>{
    for(const entry of entries){const node=entry.target as HTMLElement;
      if(entry.isIntersecting){if(!candidates.has(node))candidates.set(node,{since:0,value:'',sent:false});}
      else candidates.delete(node);
    }
  });
  const tracked=new Set<HTMLElement>();
  const scan=()=>{
    for(const node of tracked)if(!node.isConnected||!node.hasAttribute('data-social-read')){observer.unobserve(node);tracked.delete(node);candidates.delete(node);}
    document.querySelectorAll<HTMLElement>('.social-feed-page [data-social-read]').forEach(node=>{
      if(!tracked.has(node)){tracked.add(node);observer.observe(node);}
    });
  };
  const mutations=new MutationObserver(scan);mutations.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['data-social-read']});scan();
  const timer=window.setInterval(()=>{
    for(const [node,state] of candidates){
      const value=node.dataset.socialRead||'';
      if(value!==state.value){state.value=value;state.since=0;state.sent=false;}
      const rect=node.getBoundingClientRect(),x=Math.max(0,Math.min(innerWidth-1,rect.left+rect.width/2)),y=Math.max(0,Math.min(innerHeight-1,rect.top+Math.min(rect.height/2,60)));
      const top=document.elementFromPoint(x,y);
      const visible=document.visibilityState==='visible'&&node.isConnected&&rect.width>0&&rect.height>0&&
        y>=rect.top&&y<=rect.bottom&&Boolean(top&&(node===top||node.contains(top)))&&
        !document.querySelector('dialog[open], [aria-modal="true"]');
      if(!visible){state.since=0;continue;}
      if(!state.since)state.since=Date.now();
      if(!state.sent&&Date.now()-state.since>=800){
        try{const evidence=JSON.parse(value) as VisibleRead;if(evidence.moment_id&&typeof evidence.site_id==='string'){state.sent=true;receive(evidence);}}catch{/* Incomplete DOM evidence is not a read claim. */}
      }
    }
  },250);
  return()=>{window.clearInterval(timer);mutations.disconnect();observer.disconnect();};
}
