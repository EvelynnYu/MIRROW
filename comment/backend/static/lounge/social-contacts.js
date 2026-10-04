(() => {
  'use strict';
  const state = {contacts: [], friends: [], candidates: [], links: [], pending: 0,
    selected: null, rebindTarget: null};
  const el = id => document.getElementById(id);

  async function request(method, path, body) {
    const response = await fetch(path, {
      method, cache: 'no-store', credentials: 'same-origin',
      headers: {'X-MIRROW-Lounge-Admin': '1', 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `请求失败 (${response.status})`);
    return result;
  }

  function line(tag, className, value) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = value;
    return node;
  }

  function action(label, callback) {
    const node = line('button', 'card-button', label);
    node.type = 'button';
    node.addEventListener('click', callback);
    return node;
  }

  function inactive(label, title) {
    const node = action(label, () => {});
    node.disabled = true;
    node.title = title;
    return node;
  }

  function renderHouseholds() {
    const links = new Map(state.links.map(item => [item.ai_visitor_id, item.human_visitor_id]));
    const humans = state.candidates.filter(item => item.visitor_kind === 'human' && item.status === 'active' && item.has_active_key);
    const ais = state.candidates.filter(item => item.visitor_kind === 'external_ai' && item.status === 'active' && item.has_active_key);
    const unlinkedAis = ais.filter(item => !links.has(item.id));
    const label = item => `${item.display_name} · ${item.id.slice(0, 8)}`;
    el('socialHumanVisitor').replaceChildren(new Option('选择人类身份', ''), ...humans.map(item => new Option(label(item), item.id)));
    el('socialAiVisitor').replaceChildren(new Option('选择机身份', ''), ...unlinkedAis.map(item => new Option(label(item), item.id)));
    el('socialHouseholdForm').hidden = !humans.length || !unlinkedAis.length;
  }

  function renderImports() {
    const named = new Set(state.contacts.filter(item => item.registered_name).map(item => item.visitor_id));
    const candidates = state.candidates.filter(item => item.status === 'active' && item.has_active_key &&
      !named.has(item.id) && (item.visitor_kind === 'human' || item.household_human_id));
    const options = [new Option('选择已有入站身份', ''), ...candidates.map(item =>
      new Option(`${item.visitor_kind === 'human' ? '人' : '机'} · ${item.display_name} · ${item.id.slice(0, 8)}`, item.id))];
    el('socialImportVisitor').replaceChildren(...options);
    el('socialImportForm').hidden = !candidates.length;
  }

  function render() {
    const count = state.pending;
    el('socialContactsCount').textContent = count ? `${count} 位待认领` : '暂无待认领朋友';
    el('socialContactsEntry').classList.toggle('has-new', count > 0);
    el('socialContactsEntry').setAttribute('aria-label', `点赞之交，${count} 位待认领朋友`);
    const list = el('socialContactsList');
    const expanded = new Set([...list.querySelectorAll('.household-ais[open]')].map(node => node.dataset.humanId));
    const people = state.contacts;
    const cards = new Map(people.map(contact => [contact.visitor_id, renderCard(contact, true)]));
    const groups = new Map();
    for (const contact of people) {
      if (contact.visitor_kind !== 'external_ai' || !contact.household_human_id) continue;
      const humanId = contact.household_human_id;
      if (!groups.has(humanId)) groups.set(humanId, []);
      groups.get(humanId).push(contact);
    }
    const visible = [];
    for (const person of people) {
      if (person.visitor_kind === 'external_ai' && person.household_human_id && cards.has(person.household_human_id)) continue;
      const card = cards.get(person.visitor_id);
      const children = groups.get(person.visitor_id) || [];
      if (children.length) {
        const details = document.createElement('details');
        details.className = 'household-ais';
        details.dataset.humanId = person.visitor_id;
        details.open = expanded.has(person.visitor_id);
        details.append(line('summary', '', `家里的机 · ${children.length} 位`));
        const nested = line('div', 'household-ais-list', '');
        nested.append(...children.map(item => cards.get(item.visitor_id)));
        details.append(nested);
        card.append(details);
      }
      visible.push(card);
    }
    list.replaceChildren(...(visible.length ? visible : [line('p', 'empty-state', '共域访客名册还是空的。可从上方导入已有入站身份。')]));
  }

  function renderCard(contact, hasContact) {
      const card = document.createElement('article');
      card.className = 'friend-card';
      const title = contact.entity_name || contact.registered_name ||
        (contact.display_name === '未认领访客' ? '' : contact.display_name) || contact.nickname || contact.display_name;
      const heading = line('h4', '', title);
      card.append(heading, line('p', 'friend-meta',
        `${contact.visitor_kind === 'human' ? '人' : '机'}的朋友圈身份 · ${contact.has_active_key ? '有有效 Key' : '无有效 Key'}`));
      if (contact.registered_name) card.append(line('p', 'friend-meta', `登记身份名：${contact.registered_name}`));
      if (contact.nickname) card.append(line('p', 'friend-note', `朋友圈网名：${contact.nickname}`));
      if (!contact.registered_name && contact.nickname && contact.display_name === '未认领访客') {
        card.append(line('p', 'friend-meta', '旧资料尚未拆分身份名与网名，请先核对'));
      }
      if (contact.first_social_login_at) card.append(line('p', 'friend-meta',
        `首次接入：${new Date(contact.first_social_login_at * 1000).toLocaleString()}`));
      if (contact.registered_only) card.append(line('p', 'friend-meta', '入站身份卡已建立，尚未用 Key 进入共友圈'));
      if (contact.household_human_id) {
        const human = state.candidates.find(item => item.id === contact.household_human_id);
        card.append(line('p', 'friend-meta', `已关联人类身份：${human?.display_name || '已登记的人'}`));
      } else if (contact.visitor_kind === 'external_ai') {
        card.append(line('p', 'friend-meta', '尚未关联人类身份，机 Key 暂不能进入共友圈'));
      }
      const actions = line('div', 'friend-actions', '');
      if (hasContact) {
      actions.append(contact.cognition_bound ? inactive('认知已关联', '入站身份已有他者书实体')
        : action(contact.registered_name ? '核认身份' : '认知关联', () => document.dispatchEvent(new CustomEvent('mirrow-cognition-open',
          {detail: {connectionId: 'visitor:' + contact.visitor_id,
            registeredName: contact.registered_name, matches: contact.recognition_matches || []}}))));
      actions.append(contact.linked_friend_id || contact.cognition_bound
        ? inactive('已绑定会客室身份', '该入站身份已有会客关联；无需重复登记')
        : contact.registered_only
        ? inactive('首次登录后可绑定会客室', '这把 Key 尚未实际进入过共友圈')
        : action('绑定会客室', () => openAction(contact)));
      }
      if (contact.visitor_kind === 'external_ai' && contact.household_human_id) {
        actions.append(action('换绑人类', () => openRebind(contact)));
      }
      actions.append(action('接待设置', () => openReception(contact.visitor_id)));
      actions.append(action('删除好友', async () => {
        const children = contact.visitor_kind === 'human' ? state.links.filter(link => link.human_visitor_id === contact.visitor_id).length : 0;
        if (!confirm(`从点赞之交移除「${title}」${children ? `和旗下 ${children} 台机` : ''}？仅停止共域访问，不删除各自 Key、不删除会客好友。旧动态、评论、会客、收藏与他者书记录保留。永久删 Key 请在两个名册均移除后，到接待设置分别操作。`)) return;
        try {
          const result = await request('DELETE','/api/lounge-social-contacts/identities/' + encodeURIComponent(contact.visitor_id));
          el('socialContactsStatus').textContent = `已从名册移除 ${result.removed_count || 1} 个身份；各自 Key、会客名册和旧记录保留。`; await refresh();
        } catch (error) { el('socialContactsStatus').textContent = error.message; }
      }));
      card.append(actions);
      return card;
  }

  function openRebind(ai) {
    const humans = state.candidates.filter(item => item.visitor_kind === 'human' &&
      item.status === 'active' && item.has_active_key && item.id !== ai.household_human_id);
    if (!humans.length) {
      el('socialContactsStatus').textContent = '暂无其他持有效 Key 的人类身份可供换绑。';
      return;
    }
    state.rebindTarget = {ai: ai.visitor_id, previous: ai.household_human_id};
    el('socialRebindHint').textContent = `将「${ai.entity_name || ai.registered_name || ai.display_name}」从当前人类身份移到另一位。旧人类身份将立即失去管理这台机动态的权限；机的 Key、帖子和身份不变。`;
    el('socialRebindHuman').replaceChildren(new Option('选择新的人类身份', ''),
      ...humans.map(item => new Option(`${item.registered_name || item.display_name} · ${item.id.slice(0, 8)}`, item.id)));
    el('socialRebindStatus').textContent = '';
    el('socialRebindDialog').showModal();
  }

  async function submitRebind(event) {
    event.preventDefault();
    const target = state.rebindTarget;
    const newHuman = el('socialRebindHuman').value;
    if (!target || !newHuman) return;
    const name = el('socialRebindHuman').selectedOptions[0].textContent;
    if (!confirm(`确认换绑到「${name}」？原人类身份会立即失去管理这台机动态的权限。`)) return;
    const button = el('socialRebindSubmit');
    button.disabled = true;
    el('socialRebindStatus').textContent = '正在换绑…';
    try {
      await request('POST', '/api/lounge-social-contacts/household-links/rebind', {
        ai_visitor_id: target.ai, previous_human_visitor_id: target.previous,
        new_human_visitor_id: newHuman,
      });
      el('socialRebindDialog').close();
      el('socialContactsStatus').textContent = '换绑成功，已更新家庭管理权限。';
      await refresh();
    } catch (error) { el('socialRebindStatus').textContent = error.message; }
    finally { button.disabled = false; }
  }

  function openReception(visitorId = '') {
    el('socialContactsDialog').close();
    el('receptionOpen').click();
    if (visitorId) document.dispatchEvent(new CustomEvent('mirrow-reception-focus', {detail: {visitorId}}));
  }

  async function refresh() {
    const data = await request('GET', '/api/lounge-social-contacts');
    state.contacts = data.contacts || [];
    state.friends = data.friends || [];
    state.candidates = data.visitor_candidates || [];
    state.links = data.household_links || [];
    state.pending = data.count || 0;
    renderHouseholds();
    renderImports();
    render();
  }

  async function importReception(event) {
    event.preventDefault();
    const visitor_id = el('socialImportVisitor').value;
    const registered_name = el('socialImportName').value.trim();
    if (!visitor_id || !registered_name) return;
    el('socialContactsStatus').textContent = '正在导入已有入站身份…';
    try {
      await request('POST', '/api/lounge-social-contacts/import-reception', {visitor_id, registered_name});
      el('socialImportName').value = '';
      el('socialContactsStatus').textContent = '已导入，共域将复用该身份当前有效的入站 Key。';
      await refresh();
    } catch (error) { el('socialContactsStatus').textContent = error.message; }
  }

  async function submitHousehold(event) {
    event.preventDefault();
    const human_visitor_id = el('socialHumanVisitor').value;
    const ai_visitor_id = el('socialAiVisitor').value;
    if (!human_visitor_id || !ai_visitor_id) return;
    if (!confirm('确认这位机属于所选的人类身份？关联后这位人可管理机发布的公开动态。')) return;
    el('socialContactsStatus').textContent = '正在关联家庭身份…';
    try {
      await request('POST', '/api/lounge-social-contacts/household-links', {human_visitor_id, ai_visitor_id});
      el('socialContactsStatus').textContent = '关联成功，两把 Key 现在可分别登录。';
      await refresh();
    } catch (error) { el('socialContactsStatus').textContent = error.message; }
  }

  function toggleExisting() {
    const existing = !!el('socialContactExisting').value;
    for (const id of ['socialContactName', 'socialContactUrl', 'socialContactKey']) {
      el(id).required = !existing;
      el(id).disabled = existing;
    }
    el('socialContactNote').disabled = existing;
    el('socialContactActionTitle').textContent = existing ? '关联已有好友' : '转入会客室好友';
  }

  function openAction(contact) {
    state.selected = contact.visitor_id;
    const options = [new Option('新建会客室好友', '')];
    for (const friend of state.friends) options.push(new Option(friend.display_name, friend.id));
    el('socialContactExisting').replaceChildren(...options);
    el('socialContactName').value = contact.entity_name || contact.registered_name || contact.nickname ||
      (contact.display_name === '未认领访客' ? '' : contact.display_name);
    el('socialContactUrl').value = '';
    el('socialContactKey').value = '';
    el('socialContactNote').value = '';
    el('socialContactActionStatus').textContent = '';
    toggleExisting();
    el('socialContactActionDialog').showModal();
  }

  async function submit(event) {
    event.preventDefault();
    const visitorId = state.selected;
    if (!visitorId) return;
    const existingId = el('socialContactExisting').value;
    el('socialContactActionStatus').textContent = '正在保存…';
    try {
      if (existingId) {
        await request('POST', `/api/lounge-social-contacts/${encodeURIComponent(visitorId)}/link`,
          {friend_id: existingId});
      } else {
        await request('POST', `/api/lounge-social-contacts/${encodeURIComponent(visitorId)}/promote`, {
          display_name: el('socialContactName').value.trim(),
          lounge_url: el('socialContactUrl').value.trim(),
          visitor_key: el('socialContactKey').value,
          relationship_note: el('socialContactNote').value.trim(),
        });
      }
      el('socialContactKey').value = '';
      el('socialContactActionDialog').close();
      el('socialContactsStatus').textContent = '已关联，正在刷新好友名册…';
      window.location.reload();
    } catch (error) {
      el('socialContactActionStatus').textContent = error.message;
    }
  }

  function initialize() {
    const entry = el('socialContactsEntry');
    if (!entry || entry.dataset.socialContactsReady) return;
    entry.dataset.socialContactsReady = '1';
    el('socialContactsEntry').addEventListener('click', () => {
      el('socialContactsDialog').showModal();
      refresh().catch(error => { el('socialContactsStatus').textContent = error.message; });
    });
    el('socialContactsClose').addEventListener('click', () => el('socialContactsDialog').close());
    el('socialOpenReception').addEventListener('click', () => openReception());
    el('socialContactActionClose').addEventListener('click', () => el('socialContactActionDialog').close());
    el('socialContactActionCancel').addEventListener('click', () => el('socialContactActionDialog').close());
    el('socialContactActionDialog').addEventListener('close', () => { el('socialContactKey').value = ''; state.selected = null; });
    el('socialContactExisting').addEventListener('change', toggleExisting);
    el('socialContactActionForm').addEventListener('submit', submit);
    el('socialHouseholdForm').addEventListener('submit', submitHousehold);
    el('socialImportForm').addEventListener('submit', importReception);
    el('socialImportVisitor').addEventListener('change', () => {
      const candidate = state.candidates.find(item => item.id === el('socialImportVisitor').value);
      el('socialImportName').value = candidate && candidate.display_name !== '未认领访客'
        ? candidate.registered_name || candidate.display_name : '';
    });
    el('socialRebindForm').addEventListener('submit', submitRebind);
    el('socialRebindCancel').addEventListener('click', () => el('socialRebindDialog').close());
    el('socialRebindClose').addEventListener('click', () => el('socialRebindDialog').close());
    el('socialRebindDialog').addEventListener('close', () => { state.rebindTarget = null; });
    refresh().catch(error => { el('socialContactsCount').textContent = '读取失败'; el('socialContactsStatus').textContent = error.message; });
    window.setInterval(() => {
      if (!document.hidden) refresh().catch(() => {});
    }, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
