/* Bind local knowledge entities, not credentials or remote authorization. */
(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('cognitionDialog');
  let busy = false;
  let entities = [];
  let entityLabels = new Map();
  let automaticNote = true;
  let connectionKind = '';
  let registeredName = '';
  function nameOf(id) { return entities.find(entity => entity.id === id)?.name || ''; }
  function labelOf(entity) { return entityLabels.get(entity.id) || entity.name; }
  function defaultNote() {
    const primary = $('cognitionPrimary').value;
    const related = [...$('cognitionRelated').querySelectorAll('input:checked')].filter(el => el.value !== primary).map(el => nameOf(el.value));
    return `本次会客对象：${nameOf(primary) || '尚未绑定'}；关联人物：${related.join('、') || '尚未选择'}。具体关系尚未填写。`;
  }
  function syncPrimary() {
    const primary = $('cognitionPrimary').value;
    $('cognitionRelated').querySelectorAll('input').forEach(el => {
      const isPrimary = el.value === primary;
      if (isPrimary) el.checked = false;
      el.disabled = busy || isPrimary;
      el.closest('label').classList.toggle('is-primary', isPrimary);
      el.closest('label').title = isPrimary ? '已是当前交谈对象，无需再次关联' : '';
    });
  }
  function selectionChanged() {
    syncPrimary();
    if (automaticNote) $('cognitionNote').value = defaultNote();
  }
  async function api(path='', method='GET', body) {
    const response = await fetch('/api/lounge-cognition' + path, {method, cache:'no-store',
      headers:{'X-MIRROW-Lounge-Admin':'1','Content-Type':'application/json'},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '认知绑定未完成');
    return value;
  }
  async function run(task) {
    if (busy) return;
    busy = true;
    const controls = [...$('cognitionForm').querySelectorAll('input,select,textarea,button')];
    controls.forEach(el => el.disabled = true);
    try { await task(); } catch(error) { $('cognitionStatus').textContent = error.message; }
    finally { busy = false; controls.forEach(el => el.disabled = false); syncPrimary(); }
  }
  function option(value, name) { const el = document.createElement('option'); el.value = value; el.textContent = name; return el; }
  function showEntities(data) {
    entities = data.entities;
    const counts = new Map();
    entityLabels = new Map(entities.map(entity => {
      const count = counts.get(entity.name) || 0;
      counts.set(entity.name, count + 1);
      return [entity.id, entity.name + '*'.repeat(count)];
    }));
    $('cognitionPrimary').replaceChildren(option('', '暂不绑定'), ...entities.filter(e => !['aning','k'].includes(e.id)).map(e => option(e.id, labelOf(e))));
    $('cognitionRelated').replaceChildren();
    for (const entity of entities) {
      const label = document.createElement('label'), checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = entity.id;
      checkbox.onchange = selectionChanged;
      label.append(checkbox, document.createTextNode(labelOf(entity))); $('cognitionRelated').append(label);
    }
  }
  async function load() {
    const target = $('cognitionConnection').value;
    if (!target) { $('cognitionStatus').textContent = '请先在好友名册添加好友，或在接待设置生成访客身份。'; return; }
    const value = await api('/' + encodeURIComponent(target));
    $('cognitionPrimary').value = value.primary_entity_id;
    $('cognitionNote').value = value.relationship_context;
    $('cognitionRelated').querySelectorAll('input').forEach(el => el.checked = value.related_entity_ids.includes(el.value));
    syncPrimary();
    automaticNote = !value.relationship_context || value.relationship_context === defaultNote();
    if (automaticNote) $('cognitionNote').value = defaultNote();
    $('cognitionStatus').textContent = '这里只建立本地认知关联，不改变对方身份或权限。';
  }
  function showRegistrationMatches(matches, currentBinding) {
    const panel = $('cognitionCandidates');
    panel.replaceChildren();
    panel.hidden = !registeredName || !!currentBinding;
    if (panel.hidden) return;
    const intro = document.createElement('p'); intro.className = 'section-description';
    intro.textContent = matches.length
      ? `登记身份名「${registeredName}」命中 ${matches.length} 个他者书主名／别名。请核对人物后再保存认知绑定。`
      : `登记身份名「${registeredName}」没有命中他者书主名／别名。确认是新朋友后，可用下方「一键登记并绑定」；也可手动选择已有实体。`;
    panel.append(intro);
    for (const match of matches) {
      const select = document.createElement('button');
      select.type = 'button'; select.className = 'quiet-button';
      select.textContent = `${match.name} · ${match.type === 'human' ? '人' : match.type === 'silicon' ? '机' : '其他'} · ${match.matched_by === 'alias' ? '别名命中' : '主名命中'}`;
      select.onclick = () => { $('cognitionPrimary').value = match.id; selectionChanged(); };
      panel.append(select);
    }
    if (matches.length === 1) { $('cognitionPrimary').value = matches[0].id; selectionChanged(); }
  }
  document.addEventListener('mirrow-cognition-open', event => { if (busy || dialog.open) return; dialog.showModal(); run(async () => {
    const data = await api();
    const connection = data.connections.find(c => c.id === event.detail?.connectionId);
    if (!connection) { $('cognitionConnection').value = ''; throw new Error('这位好友或访客已不存在，请刷新名册'); }
    $('cognitionConnection').value = connection.id;
    connectionKind = connection.visitor_kind || '';
    registeredName = event.detail?.registeredName || '';
    $('cognitionConnectionLabel').textContent = (connection.direction === 'inbound' ? '来访身份 · ' : '好友身份 · ') + connection.name;
    // Stars only distinguish duplicate display names; IDs remain stable.
    showEntities(data);
    await load();
    showRegistrationMatches(event.detail?.matches || [], $('cognitionPrimary').value);
  }); });
  $('cognitionClose').onclick = () => dialog.close();
  $('cognitionPrimary').onchange = selectionChanged;
  $('cognitionNote').oninput = () => { automaticNote = false; };
  $('cognitionResetNote').onclick = () => { automaticNote = true; $('cognitionNote').value = defaultNote(); };
  $('cognitionCreate').onclick = () => run(async () => {
    const target = $('cognitionConnection').value;
    if (!target) throw new Error('请先选择会客连接');
    const suggested = registeredName || $('cognitionConnectionLabel').textContent.replace(/^.*?·\s*/, '').trim();
    const proposed = prompt('确认要登记的新朋友主名称：', suggested);
    if (proposed === null) return;
    const name = proposed.trim();
    if (!name) throw new Error('请填写人物主名称');
    const related = [...$('cognitionRelated').querySelectorAll('input:checked')].map(el => el.value);
    const note = $('cognitionNote').value;
    const response = await fetch('/api/cognition/other/entities', {method:'POST',
      headers:{'X-MIRROW-Lounge-Admin':'1','Content-Type':'application/json'},
      body:JSON.stringify({name, type:connectionKind === 'human' ? 'human' : connectionKind === 'external_ai' ? 'silicon' : 'other', aliases:[]})});
    const entity = await response.json();
    if (!response.ok) throw new Error(typeof entity.detail === 'string' ? entity.detail : '人物登记未完成');
    showEntities(await api());
    $('cognitionPrimary').value = entity.id;
    $('cognitionRelated').querySelectorAll('input').forEach(el => el.checked = related.includes(el.value));
    selectionChanged();
    if (!automaticNote) $('cognitionNote').value = note;
    try {
      await api('/' + encodeURIComponent(target), 'PUT', {primary_entity_id:entity.id,
        related_entity_ids:related, relationship_context:$('cognitionNote').value});
    } catch (error) {
      throw new Error('人物已登记，但绑定未完成；请点击“保存认知绑定”重试。' + error.message);
    }
    await load();
    $('cognitionStatus').textContent = '已登记并绑定这位新朋友；他者书内容仍可日后逐渐补充。';
  });
  $('cognitionForm').onsubmit = event => { event.preventDefault(); run(async () => {
    const target = $('cognitionConnection').value;
    if (!target) throw new Error('请先选择会客连接');
    await api('/' + encodeURIComponent(target), 'PUT', {primary_entity_id:$('cognitionPrimary').value,
      related_entity_ids:[...$('cognitionRelated').querySelectorAll('input:checked')].map(el => el.value),
      relationship_context:$('cognitionNote').value});
    await load(); $('cognitionStatus').textContent = '已保存，下次构建会客上下文时生效。';
  }); };
})();
