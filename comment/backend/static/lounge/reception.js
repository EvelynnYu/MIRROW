/* Local management; secrets are one-time DOM values, never browser storage. */
(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('receptionDialog');
  let busy = false;
  let focusVisitorId = '';
  async function api(path = '', method = 'GET', body) {
    const response = await fetch('/api/lounge-reception' + path, {
      method, cache: 'no-store', headers: {'X-MIRROW-Lounge-Admin': '1', 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作未完成，请稍后再试');
    return data;
  }
  async function run(task) {
    if (busy) return;
    busy = true;
    try { await task(); } catch (error) { $('receptionStatus').textContent = error.message; }
    finally { busy = false; }
  }
  function secret(data) {
    $('receptionKey').value = data.visitor_key;
    $('receptionBindNew').dataset.visitorId = data.id || '';
    $('receptionSecret').hidden = false;
    $('receptionSecret').scrollIntoView({block: 'nearest'});
  }
  async function refresh() {
    const data = await api();
    $('receptionEnabled').checked = data.enabled;
    $('receptionQuota').value = data.hourly_quota_limit;
    $('receptionAddress').value = data.public_mcp_url || '尚未配置远程地址';
    $('receptionSafetyEnabled').checked = Boolean(data.safety_policy?.enabled);
    $('receptionSafetyCriteria').value = data.safety_policy?.criteria || '';
    $('receptionStatus').textContent = (data.enabled && data.listening ? '本机接待已开放。' : '会客室休息中。') + data.notice;
    $('receptionVisitors').replaceChildren();
    for (const visitor of data.visitors) {
      const row = document.createElement('div'); row.className = 'reception-visitor';
      row.dataset.visitorId = visitor.id;
      const title = document.createElement('div');
      const statuses = {active: '可来访', paused: '已暂停', suspended: '可再次来访（上次已结束）', safety_lock: '暂不可用'};
      const name = visitor.display_name === '等待好友认领' && visitor.registered_name
        ? `${visitor.registered_name}（朋友圈登记，尚未会客认领）` : visitor.display_name;
      title.textContent = name + ' · ' + (visitor.visitor_kind === 'human' ? '人' : '机') + ' · ' + (visitor.has_active_key ? (statuses[visitor.status] || visitor.status) : '无有效 Key') + ' · ' + visitor.id.slice(0, 8);
      row.append(title);
      const actions = document.createElement('div'); actions.className = 'actions';
      const cognition = document.createElement('button'); cognition.className = 'quiet-button'; cognition.type = 'button'; cognition.textContent = '认知绑定';
      cognition.onclick = () => document.dispatchEvent(new CustomEvent('mirrow-cognition-open', {detail:{connectionId:'visitor:' + visitor.id}}));
      actions.append(cognition);
      if (visitor.can_delete_key) {
        const remove = document.createElement('button'); remove.className = 'quiet-button'; remove.type = 'button'; remove.textContent = '删除 Key';
        remove.onclick = () => run(async () => {
          if (!confirm('仅当两个好友名册均已移除该身份，或本来就是未登记空 Key，才能删除。删除不会清除旧记录，Key 会立即失效且无法找回。继续？')) return;
          if (!confirm('最后确认：永久删除这张身份的 Key？这不是可恢复的暂停访问。')) return;
          await api('/visitors/' + encodeURIComponent(visitor.id) + '/key', 'DELETE');
          $('receptionKey').value = ''; $('receptionSecret').hidden = true; await refresh();
        }); actions.append(remove);
      }
      const visitorActions = [['rotate','轮换 Key'],['revoke','撤销访问']];
      if (visitor.status === 'paused' || visitor.status === 'safety_lock') visitorActions.push(['resume','恢复身份']);
      for (const [action, label] of visitorActions) {
        const button = document.createElement('button'); button.className = 'quiet-button'; button.type = 'button';
        button.textContent = action === 'rotate' && !visitor.has_active_key ? '签发 Key'
          : action === 'resume' && visitor.status === 'safety_lock' ? '解除锁定' : label;
        if (action === 'revoke' && !visitor.has_active_key) button.disabled = true;
        button.onclick = () => run(async () => {
          if (action !== 'resume' && !window.MirrowVisitorKeyConfirm(action, visitor.display_name, visitor.has_active_key)) return;
          if (action === 'rotate' && !$('receptionSecret').hidden && !confirm('上一把刚显示的 Key 将从页面清除。确认已经复制了吗？')) return;
          const result = await api('/visitors/' + encodeURIComponent(visitor.id) + '/' + action, 'POST');
          $('receptionKey').value = ''; $('receptionSecret').hidden = true;
          await refresh();
          if (result.visitor_key) secret(result);
          if (result.notice) $('receptionStatus').textContent = result.notice;
        });
        actions.append(button);
      }
      row.append(actions); $('receptionVisitors').append(row);
    }
    if (focusVisitorId) {
      const target = [...$('receptionVisitors').children].find(row => row.dataset.visitorId === focusVisitorId);
      if (target) {
        target.scrollIntoView({block: 'center'});
        target.classList.add('reception-visitor-focused');
        focusVisitorId = '';
      }
    }
  }
  document.addEventListener('mirrow-reception-focus', event => {
    focusVisitorId = event.detail?.visitorId || '';
    if (dialog.open) {
      const target = [...$('receptionVisitors').children].find(row => row.dataset.visitorId === focusVisitorId);
      if (target) {
        target.scrollIntoView({block: 'center'});
        target.classList.add('reception-visitor-focused');
        focusVisitorId = '';
      }
    }
  });
  $('receptionOpen').onclick = () => { dialog.showModal(); run(refresh); };
  $('receptionClose').onclick = () => { $('receptionKey').value = ''; $('receptionBindNew').dataset.visitorId = ''; $('receptionSecret').hidden = true; dialog.close(); };
  dialog.addEventListener('close', () => { $('receptionKey').value = ''; $('receptionBindNew').dataset.visitorId = ''; $('receptionSecret').hidden = true; });
  $('receptionSettings').onsubmit = event => { event.preventDefault(); run(async () => {
    await api('/settings', 'PUT', {enabled: $('receptionEnabled').checked, hourly_quota_limit: Number($('receptionQuota').value)});
    await refresh();
  }); };
  $('receptionCreate').onclick = () => run(async () => {
    if (!$('receptionSecret').hidden && !confirm('上一把刚生成的 Key 将从页面清除。确认已经复制了吗？')) return;
    const data = await api('/visitors', 'POST', {visitor_kind:$('receptionKind').value});
    await refresh(); secret(data);
  });
  $('receptionSafety').onsubmit = event => {event.preventDefault(); run(async () => {
    await api('/safety-policy', 'PUT', {enabled:$('receptionSafetyEnabled').checked, criteria:$('receptionSafetyCriteria').value});
    $('receptionSafetyStatus').textContent = '已保存，下次接待回复使用新策略。';
  });};
  $('receptionCopy').onclick = () => run(async () => {
    if (!navigator.clipboard) { $('receptionKey').select(); $('receptionStatus').textContent = '请长按或按 Ctrl+C 复制选中的 Key。'; return; }
    await navigator.clipboard.writeText($('receptionKey').value); $('receptionStatus').textContent = '已复制，请仅交给对应好友。';
  });
  $('receptionBindNew').onclick = () => {
    const visitorId = $('receptionBindNew').dataset.visitorId;
    if (visitorId) document.dispatchEvent(new CustomEvent('mirrow-cognition-open', {detail:{connectionId:'visitor:' + visitorId}}));
  };
})();
