/* One profile-custody editor for the public website and the internal wall. */
(() => {
  const node = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; };
  const messages = {
    profile_schema_not_ready: '资料关联尚未启用，请先由站主完成备份迁移。',
    profile_registration_schema_not_ready: '资料来源登记尚未启用，请联系站主。',
    profile_verification_pending: '资料来源待验证：可以浏览，请由人类完成关联后再发帖互动。',
    profile_source_locked: '资料来源已登记，不能改成另一份身份。请继续在原主站生成关联码。',
    profile_source_required: '请选择这位身份的资料来源。',
    profile_mapping_invalid: '请为每位成员选择不同、且人机类型对应的本站身份。',
    profile_family_limit: '本次家庭码可包含 1 位人及最多 8 位机，请先核对已登记的本家资料。',
    profile_hosting_disabled: '资料主站还没有开放托管，请站主勾选允许。',
    profile_edit_at_source: '这个身份的网名和头像统一在资料主站修改。',
    profile_public_nickname_required: '请先为这个身份保存一个公开网名。',
    profile_uploaded_avatar_required: '请先在资料主站上传 PNG/JPG 头像；外部图片地址不能作为托管头像。',
    profile_ticket_invalid: '关联码无效、已过期或已经使用，请重新生成；连接中断后也需生成新码。',
    profile_source_unavailable: '资料主站暂时无法连接。已验证资料会保留，稍后重试。',
    profile_access_revoked: '资料授权已失效，请在主站重新生成关联码。',
    profile_link_conflict: '已有资料关联，请先明确解除，不能静默覆盖。',
    profile_identity_already_linked: '这个跨域身份已关联本站另一身份；请由站主核对，不能重复认领。',
    profile_has_outgoing_grants: '这个身份正在给其他共域提供资料，请先撤销授权再更换资料主站。',
    avatar_managed_by_human: '请切回人类身份管理。',
  };
  async function mount(container, options = {}) {
    const api = options.api || '/social/v1/profile-links';
    let disposed = false, dialog, waitingDialog = null;
    const entryFeedback=node('p');entryFeedback.role='status';
    const csrf = () => decodeURIComponent((document.cookie.match(/(?:^|; )mirrow_wall_csrf=([^;]*)/) || [, ''])[1]);
    async function request(path, method = 'GET', body) {
      const response = await fetch(api + path, { method, credentials: 'same-origin', cache: 'no-store',
        headers: { ...options.headers, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
          ...(method === 'GET' ? {} : { 'X-MIRROW-CSRF': csrf() }) }, body: body === undefined ? undefined : JSON.stringify(body) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(messages[data.detail] || data.detail || '资料关联暂时不可用');
      return data;
    }
    function button(title, action) {
      const b = node('button', title); b.type = 'button';
      b.onclick = async () => { b.disabled = true; try { await action(); } catch (e) { (dialog?.open ? feedback : entryFeedback).textContent = e.message; } finally { b.disabled = false; } };
      return b;
    }
    let feedback = node('p'); feedback.role = 'status';
    async function open() {
      entryFeedback.textContent='';
      const state = await request('/state');
      if (disposed) return;
      dialog?.close(); const activeDialog=node('dialog');dialog=activeDialog; activeDialog.className = 'mp-identity-dialog';
      const head = node('header'), body = node('section'); head.append(node('h2', '统一网名与头像'), button('关闭', () => activeDialog.close()));
      feedback = node('p'); feedback.role = 'status'; body.append(feedback);
      body.append(node('p', '选一家共域保存公开资料。在其他家用关联码确认后，同一个身份的网名与头像会自动更新。每家的登录 Key、动态和管理权限保持独立。'));
      body.append(node('p','已关联后，主站离线仍使用最后验证资料。已有资料卡但本站尚未关联的身份，只能先登录浏览；待主站上线生成关联码，完成验证后再发帖互动，不会另建第二套网名和头像。旧账号不会自动换主站。'));
      dialog.append(head, body); document.body.append(dialog);
      activeDialog.addEventListener('close', () => { activeDialog.remove(); if(dialog===activeDialog)dialog=null; }, { once: true }); activeDialog.showModal();
      if (!state.ready) { feedback.textContent = messages.profile_schema_not_ready; return; }
      if (state.owner) {
        const label = node('label'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = state.hosting_enabled;
        label.append(checkbox, node('span', '允许朋友把本家选为资料主站（仅公开网名／头像）'));
        checkbox.onchange = async () => { checkbox.disabled = true; try { await request('/policy', 'PUT', { enabled: checkbox.checked }); feedback.textContent = '设置已保存。'; }
          catch (e) { checkbox.checked = !checkbox.checked; feedback.textContent = e.message; } finally { checkbox.disabled = false; } };
        body.append(label);
      }
      if (state.human) {
        const family=node('article');family.append(node('h3','家庭资料同步 · 一次关联人和机'));
        const sourcePeople=state.profiles.filter(p=>!p.link.linked && p.registration?.state!=='pending');
        if (sourcePeople.length) {
          const target=node('input');target.type='url';target.maxLength=300;target.placeholder='接收方共域网址 https://…';target.setAttribute('aria-label','家庭资料接收方网址');
          const output=node('textarea');output.readOnly=true;output.setAttribute('aria-label','家庭关联码');
          family.append(node('p','本家保存的资料：'+sourcePeople.map(p=>p.name).join('、')),target,button('生成一份家庭关联码',async()=>{
            const result=await request('/family/grant','POST',{audience:target.value.trim()});output.value=result.code;output.select();
            feedback.textContent=`已包含 ${result.count} 位成员，五分钟有效。复制一次，到接收方由人登录后粘贴并核对对应。`;
          }),output);
        }
        const available=state.profiles.filter(p=>!p.link.linked && (!state.registration_ready || p.registration?.state!=='local'));
        if (available.length) {
          const code=node('textarea');code.maxLength=7000;code.autocomplete='off';code.placeholder='粘贴原主站的家庭关联码';code.setAttribute('aria-label','粘贴家庭关联码');
          const mapping=node('div');let previewCode='',bindings=[];
          code.oninput=()=>{previewCode='';bindings=[];mapping.replaceChildren();};
          family.append(node('p','使用原主站资料：一次粘贴，分别核对人和家里的机。名字只是提示，不按同名自动认领。'),code,
            button('核对家庭成员',async()=>{
              const result=await request('/family/preview','POST',{code:code.value.trim()});previewCode=code.value.trim();bindings=[];mapping.replaceChildren(node('p','资料来源：'+result.origin+'（确认时向源站验证）'));
              result.members.forEach((member,index)=>{
                const existing=state.profiles.find(p=>p.link.identity_id===result.origin+'#'+member.profile_id);
                if(existing){mapping.append(node('p',member.nickname+' → '+existing.name+' · 已关联'));return;}
                const row=node('label'),select=node('select');select.setAttribute('aria-label','对应 '+member.nickname);
                select.append(new Option('选择本站身份；也可暂不关联',''));
                const candidates=available.filter(p=>p.kind===member.kind);
                for(const p of candidates)select.append(new Option(p.name,p.actor));
                if(candidates.length===1 && result.members.filter(m=>m.kind===member.kind).length===1)select.value=candidates[0].actor;
                row.append(node('span',`${member.kind==='human'?'人':'机'} · ${member.nickname}（${member.profile_id.slice(-4)}） →`),select);
                mapping.append(row);bindings.push({index,select});
              });
              mapping.append(button('确认整组关联',async()=>{
                const selected=bindings.filter(b=>b.select.value).map(b=>({index:b.index,subject:b.select.value}));
                if(!selected.length || new Set(selected.map(b=>b.subject)).size!==selected.length){feedback.textContent=messages.profile_mapping_invalid;return;}
                if(!confirm('确认这些对应是同一个人及同一台机？将一组同步头像网名，不合并 Key、动态或认知。不按姓名推断身份。'))return;
                try { await request('/family/link','POST',{code:previewCode,bindings:selected}); }
                catch(e){previewCode='';bindings=[];mapping.replaceChildren();throw Error(e.message+' 本站没有部分绑定；此家庭码可能已部分消费，请回主站重新生成一份再试。');}
                code.value='';options.onChange?.();await open();
              }));
            }),mapping);
          family.append(node('small','若连接中断或有成员验证失败，本站不会部分绑定；请回主站生成新的家庭码再试。'));
        }
        body.append(family);
      }
      for (const person of state.profiles) {
        const card = node('article'); card.append(node('h3', person.name));
        const prefix = '/people/' + encodeURIComponent(person.actor);
        const registration = person.registration || {state:'unconfirmed'};
        if (state.registration_ready && registration.state === 'unconfirmed' && state.human) {
          card.append(node('p','旧账号还没确认资料来源，现有使用不受影响。请明确选择：'));
          const source = node('input'); source.type='url';source.maxLength=300;source.placeholder='已有资料主站的共域网址 https://…';source.setAttribute('aria-label','已有资料主站网址');
          card.append(button('首次建资料：由本站保存', async () => {
            if (!confirm('确认这个身份没有其他资料主站？这里只保存公开网名和头像，不改 Key 或认知关联。')) return;
            await request(prefix+'/source','PUT',{choice:'local'});options.onChange?.();await open();
          }), source, button('已有资料卡：登记待关联', async () => {
            if (!confirm('登记后需验证此主站才能发帖、评论、点赞或修改资料。主站离线时只能先浏览。确定？')) return;
            await request(prefix+'/source','PUT',{choice:'existing',origin:source.value.trim()});options.onChange?.();await open();
          }));
        }
        if (registration.state === 'pending') card.append(node('p','资料待验证 · 来源：'+registration.origin+'。可浏览；网名／头像、发帖、评论和点赞等待关联完成。'));
        if (person.link.linked) {
          card.append(node('p', '资料主站：' + person.link.origin), node('small',
            `版本 ${person.link.version} · ${person.link.status === 'verified' ? '已验证' : person.link.status === 'revoked' ? '授权已撤销，显示最后资料' : '主站离线，显示最后资料'} · 上次验证 ${new Date(person.link.checked_at * 1000).toLocaleString()}`));
          if (state.human) card.append(button('刷新资料', async () => { const result = await request(prefix + '/refresh', 'POST'); feedback.textContent = result.status === 'verified' ? '已取得主站最新资料。' : '主站未连通，保留上次验证资料。'; options.onChange?.(); }),
            button('解除资料关联', async () => { if (!confirm(state.registration_ready ? '解除后回到原资料主站的待验证状态，暂不能发帖互动；不会自动变成本站独立资料。不删除动态、身份或 Key。确定解除？' : '解除后恢复本站原有网名和头像，不删除动态、身份或 Key。确定解除？')) return;
              await request(prefix + '/link', 'DELETE'); options.onChange?.(); await open(); }));
        }
        if (!state.human) card.append(node('p', '资料关联由你所属的人类身份确认；网名可在资料主站用自己的 Key 修改。'));
        for (const grant of person.grants || []) if (!grant.revoked) {
          const line = node('p', '已授权：' + grant.audience); line.append(button('撤销', async () => {
            if (!confirm('撤销后这家停止更新资料，但不会删除对方已有动态或改变双方好友权限。确定？')) return;
            await request(prefix + '/grants/' + encodeURIComponent(grant.id), 'DELETE'); await open();
          })); card.append(line);
        }
        body.append(card);
      }
    }
    container.replaceChildren(button('统一网名与头像', open),entryFeedback);
    const guide = () => {
      if (disposed) return;
      waitingDialog = document.querySelector('dialog.md-dialog[open]');
      if (waitingDialog) waitingDialog.addEventListener('close', guide, { once: true });
      else void open().catch(error => { if (!disposed) entryFeedback.textContent = error.message; });
    };
    if (options.openAfterRegistration) guide();
    return () => { disposed = true; waitingDialog?.removeEventListener('close', guide); dialog?.close(); container.replaceChildren(); };
  }
  window.MirrowProfileIdentity = { mount };
})();
