/* Reference-only forwarding. Source material stays at its authenticated home. */
(() => {
  const node=(tag,text='')=>{const value=document.createElement(tag);value.textContent=text;return value;};
  const button=(text,fn)=>{const value=node('button',text);value.type='button';value.addEventListener('click',fn);return value;};
  window.MirrowForwards={
    render(item,{name,onSource}) {
      const forward=item.forward;if(!forward?.reference)return null;
      const card=node('aside');card.className='forward-card';card.append(node('small','↗ 转发来源'));
      if(forward.status==='available'&&typeof forward.source?.content==='string'){
        card.append(node('strong',name(forward.source.author)),node('p',forward.source.content),button('查看原动态',()=>onSource(forward.source.id)));
      }else if(forward.status==='link_only'){
        card.append(node('p','原文保留在来源共域，需要在那里登录查看。'));
        try{const origin=new URL(forward.reference.origin);if(origin.protocol==='https:'&&!origin.username&&!origin.password){const link=node('a','去 '+origin.hostname+' 查看 ↗');link.href=origin.href.replace(/\/$/,'')+'/?moment='+encodeURIComponent(forward.reference.moment_id);link.target='_blank';link.rel='noopener noreferrer';card.append(link);}}catch{/* Optional malformed reference stays unavailable. */}
      }else card.append(node('p','原动态已私密、删除或迁移，当前无法查看。'));
      return card;
    },
    open(item,{actor,api,onDone,status}) {
      const dialog=node('dialog');dialog.className='forward-dialog';
      const header=node('div');header.className='row between';header.append(node('h2','转发这条动态'),button('关闭',()=>dialog.close()));
      const text=node('textarea');text.maxLength=1200;text.placeholder='顺便说一句，可留空';
      const hint=node('p','转发公开在当前共域。只保留来源引用，原动态的公开状态仍由作者管理。');hint.className='muted';
      const feedback=node('p');feedback.setAttribute('role','status');
      const send=button('确认转发',async()=>{
        if(send.disabled)return;
        const payload={content:text.value.trim()||'转发了一条动态',forward_ref:{origin:'',moment_id:item.id}};
        const ticket=window.MirrowWallState.begin(actor,'forward:'+item.id,payload);if(!ticket)return;
        send.disabled=true;send.textContent='正在转发…';let confirmed=false;
        try{await api('/moments','POST',{...payload,request_id:ticket.requestId});confirmed=true;feedback.textContent='已转发';onDone();dialog.close();status('已转发；原文仍保留在来源动态。');}
        catch(error){feedback.textContent=error.message||'转发未确认，请核对目标动态后再试。';}
        finally{window.MirrowWallState.finish(ticket,confirmed);send.disabled=false;send.textContent='确认转发';}
      });send.className='primary';dialog.append(header,text,hint,feedback,send);dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();
    }
  };
})();
