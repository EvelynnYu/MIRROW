(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const state = {sites: [], friends: [], saving: false};

  async function request(method, path, body) {
    const response = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: {'X-MIRROW-Lounge-Admin': '1', 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
    return data;
  }

  function message(value) { byId('socialSitesStatus').textContent = value; }

  const profileReasons = {
    profile_human_key_required: '请先填写人的 Key，由人关联本家机',
    profile_household_mismatch: '人和机的 Key 不属于对方登记的同一家庭',
    profile_key_kind_mismatch: '人／机 Key 的类型填反了，请核对',
    profile_public_nickname_required: '请先在本家资料管理保存人和机的公开网名',
    profile_uploaded_avatar_required: '请先在本家上传共域头像',
    profile_link_conflict: '对方身份已关联另一张资料卡，请核对 Key',
    profile_source_locked: '对方身份已有另一资料来源，不能自动覆盖',
    profile_has_outgoing_grants: '对方身份仍在向其他家提供资料，不能改为投影',
    profile_schema_not_ready: '本家资料同步尚未启用',
    profile_registration_schema_not_ready: '对方资料来源登记尚未启用',
    social_site_key_rejected: 'Key 无效、已撤销，或身份尚未登记',
  };

  function profileStatus(result) {
    const label = role => {
      const row = result.profiles?.[role] || {};
      return row.status === 'linked' ? '资料已关联' : row.status === 'not_configured' ? '未配置'
        : `资料未关联（${profileReasons[row.reason] || '对方未升级资料协议或暂时不可用；可稍后重试'}）`;
    };
    return `你：${label('human')}；AI：${label('ai')}。`;
  }

  function setTab(outbound) {
    byId('socialInboundPane').hidden = outbound;
    byId('socialOutboundPane').hidden = !outbound;
    byId('socialInboundTab').setAttribute('aria-selected', String(!outbound));
    byId('socialOutboundTab').setAttribute('aria-selected', String(outbound));
    if (outbound) refresh().catch(error => message(error.message));
  }

  function text(tag, value, className = '') {
    const element = document.createElement(tag);
    element.textContent = value;
    element.className = className;
    return element;
  }

  function button(label, onClick) {
    const element = text('button', label, 'card-button');
    element.type = 'button';
    element.addEventListener('click', async () => {
      if (element.disabled) return;
      element.disabled = true;
      try { await onClick(); } finally { element.disabled = false; }
    });
    return element;
  }

  function clearForm() {
    byId('socialSiteForm').reset();
    byId('socialSiteId').value = '';
    byId('socialSiteCancel').hidden = true;
  }

  function edit(site) {
    byId('socialSiteId').value = site.id;
    byId('socialSiteName').value = site.name;
    byId('socialSiteOrigin').value = site.origin;
    byId('socialSiteHumanKey').value = '';
    byId('socialSiteAiKey').value = '';
    byId('socialSiteImport').value = '';
    byId('socialSiteCancel').hidden = false;
    byId('socialSiteName').focus();
    message('正在修改这家共域；Key 留空会保留原值。');
  }

  function render() {
    const list = byId('socialSitesList');
    const rows = state.sites.map(site => {
      const card = document.createElement('article');
      card.className = 'friend-card';
      card.append(text('h4', site.name), text('p', site.origin, 'section-description'));
      card.append(text('p', `你：${site.has_human_key ? 'Key 已配置' : '尚未配置'} · AI：${site.has_ai_key ? 'Key 已配置' : '尚未配置'}`,
        'section-description'));
      const actions = document.createElement('div');
      actions.className = 'dialog-actions';
      actions.append(button('修改', () => edit(site)), button('测试连接', async () => {
        message(`正在分别检查「${site.name}」的人／机连接…`);
        try {
          const result = await request('POST', `/api/social-sites/${encodeURIComponent(site.id)}/test`);
          const label = actor => {
            const row = result.identities?.[actor] || {};
            return row.status === 'connected' ? `连通${row.name ? `（${row.name}）` : ''}`
              : row.status === 'not_configured' ? '未配置' : `未连通（${row.reason || '请检查地址和 Key'}）`;
          };
          message(`${site.name}：你 ${label('human')}；AI ${label('ai')}。`);
        } catch (error) { message(error.message); }
      }), button('同步本家资料', async () => {
        message(`正在用「${site.name}」的人／机 Key 验证并关联资料…`);
        try {
          const result = await request('POST', `/api/social-sites/${encodeURIComponent(site.id)}/profile-sync`);
          message(profileStatus(result));
        } catch (error) { message(error.message); }
      }), button('删除', async () => {
        if (!confirm(`移除「${site.name}」的共域地址和本机保存的两把 Key？不会删除对方家的动态。`)) return;
        try {
          await request('DELETE', `/api/social-sites/${encodeURIComponent(site.id)}`);
          message('已移除这家共域。');
          clearForm();
          await refresh();
        } catch (error) { message(error.message); }
      }));
      card.append(actions);
      return card;
    });
    list.replaceChildren(...(rows.length ? rows : [text('p', '还没有注册其他共域。', 'empty-state')]));
    const options = [new Option('不导入', '')];
    for (const friend of state.friends) {
      if (friend.has_ai_key) options.push(new Option(friend.name, friend.id));
    }
    const selection = byId('socialSiteImport').value;
    byId('socialSiteImport').replaceChildren(...options);
    if (state.friends.some(friend => friend.id === selection)) byId('socialSiteImport').value = selection;
  }

  async function refresh() {
    const [sites, imports] = await Promise.all([
      request('GET', '/api/social-sites'), request('GET', '/api/social-sites/lounge-imports'),
    ]);
    state.sites = sites.sites || [];
    state.friends = imports.friends || [];
    render();
  }

  async function save(event) {
    event.preventDefault();
    if (state.saving) return;
    const id = byId('socialSiteId').value;
    const payload = {
      name: byId('socialSiteName').value.trim(),
      origin: byId('socialSiteOrigin').value.trim(),
      import_lounge_friend_id: byId('socialSiteImport').value,
    };
    const human = byId('socialSiteHumanKey').value;
    const ai = byId('socialSiteAiKey').value;
    if (human) payload.human_key = human;
    if (ai) payload.ai_key = ai;
    if (!id && !human && !ai && !payload.import_lounge_friend_id) {
      message('至少填写一把 Key，或导入会客室里 AI 已保存的 Key。');
      return;
    }
    message('正在保存共域并验证关联本家人／机资料…');
    state.saving = true;
    const submit = byId('socialSiteForm').querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const saved = await request(id ? 'PUT' : 'POST', id ? `/api/social-sites/${encodeURIComponent(id)}` : '/api/social-sites', payload);
      clearForm();
      await refresh();
      try {
        const linked = await request('POST', `/api/social-sites/${encodeURIComponent(saved.site.id)}/profile-sync`);
        message(`站点已保存。${profileStatus(linked)}`);
      } catch (error) { message(`站点已保存，资料暂未关联：${error.message}`); }
    } catch (error) { message(error.message); }
    finally { state.saving = false; submit.disabled = false; }
  }

  function initialize() {
    const tab = byId('socialInboundTab');
    if (!tab || tab.dataset.socialSitesReady) return;
    tab.dataset.socialSitesReady = '1';
    byId('socialInboundTab').addEventListener('click', () => setTab(false));
    byId('socialOutboundTab').addEventListener('click', () => setTab(true));
    byId('socialSiteForm').addEventListener('submit', save);
    byId('socialSiteCancel').addEventListener('click', clearForm);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
