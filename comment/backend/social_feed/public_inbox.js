/* Opening the source-wall bell acknowledges its displayed notification snapshot. */
window.MirrowInbox = {mount({api,actor,currentActor,openDetail}) {
  let disposed=false,busy=false,rows=[],listed=[],initialized=false;
  const known=new Set(),confirmed=new Set(),inflight=new Set();
  const node=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
  const button=(text,fn)=>{const n=node('button',text);n.type='button';n.onclick=fn;return n;};
  const bell=button('🔔',openPanel);
  bell.className='wall-inbox-bell';bell.setAttribute('aria-label','共域通知');
  const panel=node('dialog','','wall-inbox-dialog'),toast=button('',openPanel);
  toast.className='wall-inbox-toast';toast.hidden=true;toast.setAttribute('role','status');
  const heading=node('header',''),list=node('div',''),problem=node('p',''),tools=node('div','','wall-inbox-tools');
  heading.append(node('h2','共域通知'),button('×',()=>panel.close()));
  panel.append(heading,tools,problem,list);
  document.querySelector('body>header>.row').prepend(bell);document.body.append(panel,toast);
  const valid=()=>!disposed&&currentActor()===actor;
  const redraw=()=>{
    bell.textContent=rows.length?`🔔 ${rows.length>99?'99+':rows.length}`:'🔔';
    bell.setAttribute('aria-label',`共域通知，${rows.length} 条未读`);
    list.replaceChildren();
    const labels={mention:'艾特了你',comment:'留下了评论/回复',like:'点了赞',moment:'发了新动态'};
    const groups=new Map();
    const shown=panel.open?listed:rows;
    for(const row of shown){const key=row.kind==='like'?'like:'+row.moment_id:row.id;groups.set(key,[...(groups.get(key)||[]),row]);}
    for(const group of groups.values()){
      const row=group[0],b=button('',()=>void inspect(row));b.className='wall-inbox-row';
      const names=[...new Set(group.map(r=>r.actor_name||'朋友'))].join('、');
      b.append(node('strong',names+(labels[row.kind]||'有新动静')),node('span',String(row.comment_content||row.moment_content||'').slice(0,160)),node('small',new Date(row.created_at*1000).toLocaleString()));list.append(b);
    }
    if(!shown.length)list.append(node('p','当前没有新通知。'));
  };
  const mark=async(ids)=>{
    if(!valid())return;
    ids=ids.filter(id=>!inflight.has(id)&&!confirmed.has(id));if(!ids.length)return;
    ids.forEach(id=>inflight.add(id));
    try{
      for(let i=0;i<ids.length;i+=100){const batch=ids.slice(i,i+100);if(!valid())return;
        await api('/notifications/read','POST',{notification_ids:batch});if(!valid())return;
        const read=new Set(batch);batch.forEach(id=>confirmed.add(id));
        while(confirmed.size>1000)confirmed.delete(confirmed.values().next().value);
        rows=rows.filter(r=>!read.has(r.id));redraw();
      }
    }finally{ids.forEach(id=>inflight.delete(id));}
  };
  async function inspect(row){
    if(!valid()||busy)return;busy=true;panel.close();
    try{
      const presented=await openDetail(row.moment_id);
    if(!valid()||!presented)return;
      if(row.comment_id)document.querySelector(`#detail [data-comment-id="${CSS.escape(row.comment_id)}"]`)?.scrollIntoView({block:'center'});
      await mark(rows.filter(r=>r.moment_id===row.moment_id).map(r=>r.id));
    }catch{if(valid()){problem.textContent='已读确认暂时失败，原未读保留。';}}
    finally{busy=false;}
  }
  async function refresh(){
    if(!valid()||busy||document.visibilityState==='hidden')return;busy=true;
    try{
      const payload=await api('/notifications');if(!valid())return;
      const next=(payload.items||[]).filter(r=>!confirmed.has(r.id)),fresh=next.filter(r=>!known.has(r.id)&&r.kind!=='like'&&(r.kind!=='moment'||r.actor==='k'));
      next.forEach(r=>known.add(r.id));while(known.size>1000)known.delete(known.values().next().value);
      if(initialized&&fresh.length){toast.textContent=`共域有 ${fresh.length} 条新互动`;toast.hidden=false;}
      initialized=true;rows=next;problem.textContent='';
      if(panel.open)listed=[...new Map([...listed,...next].map(r=>[r.id,r])).values()];
      redraw();if(panel.open)await mark(rows.map(r=>r.id));
    }catch{if(valid())problem.textContent='通知暂时不可用，未读保留。';}finally{busy=false;}
  }
  function openPanel(){
    if(!valid())return;listed=rows.slice();toast.hidden=true;panel.showModal();redraw();
    void mark(rows.map(r=>r.id)).catch(()=>{if(valid())problem.textContent='已读确认暂时失败，可刷新重试。';});void refresh();
  }
  tools.append(button('刷新',()=>void refresh()),button('当前列表全部已读',()=>{if(!valid()||busy)return;busy=true;void mark(rows.map(r=>r.id)).catch(()=>{problem.textContent='已读确认失败，稍后重试。';}).finally(()=>busy=false);}));
  const tick=()=>void refresh(),timer=setInterval(tick,60000);document.addEventListener('visibilitychange',tick);void refresh();
  return()=>{disposed=true;clearInterval(timer);document.removeEventListener('visibilitychange',tick);panel.close();panel.remove();bell.remove();toast.remove();rows=[];listed=[];known.clear();confirmed.clear();};
}};
