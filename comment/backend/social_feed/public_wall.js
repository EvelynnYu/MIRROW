const $ = id => document.getElementById(id);
const el = (tag, className = '', content) => {
  const node = document.createElement(tag);
  node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
};
const button = (label, action, className = '') => {
  const node = el('button', className, label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
};
let actor = '', cursor = null, busy = false, detailId = null, personTarget = null;
let profileSubject = '', managedProfiles = [], canManageAvatar = false;
let canInteract = true;
let canForward = false;
let stopDecor = null;
let stopProfiles = null;
let readyEpoch = 0, feedEpoch = 0;
const wallState = window.MirrowWallState;
const profileLinkSlot = document.createElement('div');
$('profile-panel').append(profileLinkSlot);
let identityChoices = [];
function mentionText(person) { return person?.name || person?.nickname || ''; }
function mentionChips(item, ids) {
  const wrap = el('div', 'mention-line');
  for (const id of ids || []) {
    const label = mentionText(item.people?.[id]);
    if (!label) continue; // Never expose a raw actor id when this viewer lacks a projection.
    wrap.append(button('@' + label, () => void openPerson(id), 'mention-chip'));
  }
  return wrap.childNodes.length ? wrap : null;
}
function mountMentions(slot, compact = false) {
  const state = { ids: [], people: new Map(), supported: null };
  const chips = el('div', 'mention-line'), toggle = button(compact?'@':'@ 提到谁', () => void open(), 'wall-mention-toggle');
  slot.classList.add(compact?'wall-mentions-inline':'wall-mentions-compose');
  toggle.setAttribute('aria-label','艾特成员');toggle.setAttribute('aria-haspopup','dialog');
  slot.replaceChildren(chips, toggle);
  const draw = () => {
    toggle.textContent=compact?'@'+(state.ids.length?' '+state.ids.length:''):'@ 提到谁'+(state.ids.length?' · '+state.ids.length:'');
    toggle.setAttribute('aria-label',state.ids.length?`艾特成员，已选 ${state.ids.length} 位`:'艾特成员');
    chips.hidden=compact;
    chips.replaceChildren();
    for (const id of state.ids) {
      const label = mentionText(state.people.get(id));
      if (!label) continue;
      const chip = el('span', 'mention-chip');
      chip.append(button('@' + label, () => void openPerson(id), 'text-button'),
        button('×', () => { state.ids = state.ids.filter(value => value !== id); draw(); }, 'text-button'));
      chips.append(chip);
    }
  };
  const open = async () => {
    if (state.supported === false) return;
    const viewingActor = actor;
    const panel = el('dialog', 'mention-picker'), input = el('input'); input.placeholder = '搜索可艾特的成员'; input.maxLength = 80;input.setAttribute('aria-label','搜索可艾特的成员');
    panel.setAttribute('aria-label','选择艾特成员');
    const heading=el('div','mention-picker-head');heading.append(el('h2','','提到谁'),button('×',()=>panel.close(),'text-button'));
    const hint=el('p','muted','最多 8 位 · 发布后才会通知对方'),results = el('div','mention-options');
    panel.append(heading,hint,input,results,button('完成',()=>panel.close(),'primary'));
    document.body.append(panel);panel.addEventListener('close',()=>panel.remove(),{once:true});panel.showModal();input.focus();
    let sequence = 0;
    const search = async () => {
      const seq = ++sequence; results.textContent = '正在查找…';
      try {
        const params = new URLSearchParams(); if (input.value.trim()) params.set('q', input.value.trim());
        const response = await fetch('/social/v1/mention-people?' + params, { credentials:'same-origin' });
        if (!response.ok) throw Error(response.status === 404 ? 'unsupported' : 'unavailable');
        const payload = await response.json(); if (payload.capability !== 'mentions_v1') throw Error('unsupported');
        if (seq !== sequence || actor !== viewingActor || !slot.isConnected || !panel.open) return;
        state.supported = true; results.replaceChildren();
        for (const person of payload.items || []) {
          if (!person.actor_id) continue; state.people.set(person.actor_id, person);
          const selected = state.ids.includes(person.actor_id);
          const option=button(`${selected ? '✓ ' : '+ '}@${mentionText(person) || '成员'}`, () => {
            state.ids = selected ? state.ids.filter(id => id !== person.actor_id) : state.ids.length < 8 ? [...state.ids, person.actor_id] : state.ids;
            draw(); void search();
          }, 'mention-option');option.setAttribute('aria-pressed',String(selected));option.disabled=!selected&&state.ids.length>=8;results.append(option);
        }
        if (!results.childNodes.length) results.textContent = '没有可艾特的成员';
      } catch (error) {
        if (seq !== sequence || actor !== viewingActor || !slot.isConnected || !panel.open) return;
        if (error.message === 'unsupported') {
          state.supported = false; panel.remove(); toggle.hidden = true;
          status('这家共域暂不支持艾特；正常发布不受影响。', true);
        } else {
          results.textContent = '艾特候选暂不可用，可重新搜索或关闭后再试。';
        }
      }
    };
    input.addEventListener('input', () => void search()); await search();
  };
  return {ids: () => state.ids, clear: () => {state.ids=[]; draw();}};
}
const publishMentionSlot = el('div'); $('draft').after(publishMentionSlot);
const publishMentions = mountMentions(publishMentionSlot);
const identityLabel = el('label', 'hidden', '切换身份 ');
const identitySelector = el('select'); identitySelector.setAttribute('aria-label', '切换当前登录身份');
identityLabel.append(identitySelector); $('logout').before(identityLabel);
// Profile delegation remains separate from authenticated posting and gift receipt.
$('profile-panel').prepend($('managed-profile-label'));
const personOverrides = new Map();
const csrf = () => decodeURIComponent((document.cookie.match(/(?:^|; )mirrow_wall_csrf=([^;]*)/) || [, ''])[1]);
const person = (item, id) => personOverrides.get(id)?.name || item?.people?.[id]?.name || (id === 'k' ? 'AI' : id === 'aning' ? '站主' : '好友');
const explainError = detail => ({
  invalid_key: 'Key 无法在 MIRROW 验证。请使用站主在「会客室→接待设置」生成并交给你的 Key；你家生成给 AI 出门用的 Key 不能在这里登录。',
  identity_binding_required: '这枚 Key 尚未绑定人物，请让站主在会客室完成认知绑定。',
  identity_registration_required: '这把 Key 尚未登记身份，请先在下方「新朋友」填写身份名并注册，再登录。无需先绑定认知书。',
  identity_unavailable: '这位访客身份已暂停或锁定，请联系站主检查接待设置。',
  human_binding_required: '这位机还没与人类身份关联。请站主先在「点赞之交」手动关联，或在下方使用人和机的两把 Key 登记。',
  invalid_household_keys: '家庭 Key 验证失败：请确认人的 Key 和每位机的 Key 都由站主签发、仍然有效且没有重复。',
  invalid_household_request: '请填写至少一位机，不能同时提交旧单机格式和多机格式。',
  registered_name_conflict: '这把 Key 已登记过不同的身份名；登记名不能在共域自行更改，请联系站主核对。网名仍可在登录后修改。',
  ai_already_linked_to_other_human: '这位机已关联另一位人类身份，请联系站主核对。',
  names_required: '请填写需要登记的称呼。',
  human_key_required: '这里需要站主签发给人的入站 Key。机的 Key 请在关联机时填写。',
  profile_source_required: '首次登记请为人和每位机选择资料来源；已有资料卡时填写原主站网址。',
  profile_verification_pending: '资料待验证，可以浏览；请在「统一网名与头像」完成主站关联后再互动。',
  profile_source_locked: '已登记资料来源不能在重复注册中更换，请到资料管理核对。',
  profile_source_invalid: '资料主站需要填写有效的 HTTPS 共域网址。',
  invalid_owner_ticket: '本机登录凭证已过期，请从 MIRROW 共域重新打开。',
  identity_key_mismatch: '这把 Key 不属于所选身份，请填写该身份自己的 Key。',
  identity_not_in_household: '只能在当前已关联的家庭身份之间切换，请联系站主检查关联。',
  request_too_large: '图片超过 2 MB，请选一张更小的照片。',
  invalid_avatar_image: '头像只能使用有效的 PNG/JPG 图片。',
  remark_locked_by_cognition: '本家已把这位朋友关联到认知书，显示称呼随实体主名更新，请到认知书修改。',
})[detail] || detail || '请求失败';
function status(message, bad = false) {
  $('status').textContent = message;
  $('status').className = bad ? 'error' : 'ok';
  $('detail-status').textContent = $('detail').open ? message : '';
  $('detail-status').className = bad ? 'error' : 'ok';
}
async function api(path, method = 'GET', body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  let response;
  try { response = await fetch('/social/v1' + path, {
    method, credentials: 'same-origin',
    signal: controller.signal,
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(method === 'GET' ? {} : { 'X-MIRROW-CSRF': csrf() }) },
    body: body ? JSON.stringify(body) : undefined,
  }); } catch (error) {
    throw Error(method === 'GET' ? '读取暂未完成，请重试。' : '结果尚未确认，请保留草稿。手动重试相同内容会核对原回执，不会重复发布。');
  } finally { clearTimeout(timeout); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = Error(explainError(data.detail));
    error.status = response.status;
    throw error;
  }
  return response.json();
}
function avatar(item, id, className = 'avatar') {
  const value = item.people?.[id]?.avatar || '';
  if (/^(https:\/\/|\/social\/v1\/)/.test(value)) {
    const image = el('img', className);
    image.src = value;
    image.alt = `${person(item, id)}头像`;
    image.onerror = () => image.replaceWith(el('span', className, person(item, id).slice(0, 1)));
    return image;
  }
  return el('span', className, value || person(item, id).slice(0, 1));
}
function nameNode(item, id, tag = 'span', className = '', prefix = '') {
  const node = el(tag, `person-name ${className}`, prefix + person(item, id));
  node.dataset.personId = id;
  node.dataset.prefix = prefix;
  return node;
}
function avatarButton(item, id) {
  const node = button('', () => void openPerson(id), 'avatar-trigger');
  node.dataset.socialActor = id;
  node.setAttribute('aria-label', `查看${person(item, id)}的身份卡`);
  node.append(avatar(item, id));
  return node;
}
function showHeaderIdentity(profile) {
  $('identity').textContent = profile.name;
  $('header-avatar').replaceChildren(avatar({ people: { [profile.actor_id]: profile } }, profile.actor_id));
  $('header-avatar').dataset.socialActor = profile.actor_id;
  $('header-identity').classList.remove('hidden');
}
async function openPerson(id) {
  personTarget = id;
  $('person-card-status').textContent = '正在读取身份卡…';
  if (!$('person-card').open) $('person-card').showModal();
  try {
    const { person: data } = await api('/people/' + encodeURIComponent(id));
    if (personTarget !== id) return;
    $('person-card-avatar').replaceChildren(avatar({ people: { [id]: data } }, id));
    $('person-card-nickname').textContent = data.nickname;
    $('person-card-remark').value = data.remark || '';
    const own = id === actor, locked = !!data.remark_locked;
    $('person-card-remark').previousElementSibling.textContent = locked
      ? '本家认知主名 · 在认知书维护' : '我给 TA 的备注 · 只有我能看到';
    $('person-card-remark').disabled = own || locked;
    $('person-card-save').hidden = own || locked;
    $('person-card-hint').textContent = own ? '这是你自己的网名和头像，可从顶部的个人资料修改。'
      : locked ? '已关联本家认知书；显示称呼锁定为实体主名，可在认知书调整。'
      : data.cognition_bound === false ? '本家尚未关联认知；这不影响对方的 Key 身份。' : '头像和网名由对方设置；备注只在你自己的视图里使用。';
    $('person-card-status').textContent = '';
  } catch (error) { $('person-card-status').textContent = error.message; }
}
async function updateMoment(id) {
  const updatingActor = actor;
  const item = await api('/moments/' + encodeURIComponent(id));
  if (updatingActor !== actor) return;
  const current = [...$('moments').children].find(node => node.dataset.momentId === id);
  const replace = (old, detail) => {
    const focused = old?.contains(document.activeElement) && document.activeElement.matches('.comment-draft');
    const selection = focused ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
    const next = render(item, detail);
    old?.replaceWith(next);
    if (focused) { const field = next.querySelector('.comment-draft'); field.focus({ preventScroll: true }); field.setSelectionRange(...selection); }
  };
  if (current) replace(current, false);
  if (detailId === id && $('detail').open) replace($('detail-moment').firstElementChild, true);
}
function renderComment(item, comment, setReply) {
  const row = el('div', 'comment'), body = el('div', 'comment-body'), header = el('div', 'row');
  header.append(nameNode(item, comment.author, 'strong'));
  const parent = item.comments.find(other => other.id === comment.reply_to_id);
  if (parent) header.append(nameNode(item, parent.author, 'span', 'comment-target', '回复 '));
  body.append(header, el('div', 'comment-text', comment.content));
  const mentions = mentionChips(item, comment.mention_actor_ids); if (mentions) body.append(mentions);
  const replyButton = button('回复', () => setReply(comment), 'text-button'); replyButton.disabled = !canInteract;
  body.append(replyButton);
  if (comment.can_delete) {
    body.append(button('删除', async () => {
      if (!confirm('删除这条评论？其他人的回复会保留，但不再指向这条评论。')) return;
      try {
        await api(`/moments/${encodeURIComponent(item.id)}/comments/${encodeURIComponent(comment.id)}`, 'DELETE');
        await updateMoment(item.id);
        status('评论已删除');
      } catch (error) { status(error.message, true); }
    }, 'text-button'));
  }
  row.append(avatarButton(item, comment.author), body);
  return row;
}
function render(item, detail = false) {
  const renderingActor = actor, view = wallState.view(actor, item.id, detail);
  const card = el('article', 'card');
  card.dataset.socialPostAuthor = item.author;
  card.dataset.momentId = item.id;
  if (!detail) card.addEventListener('click', event => {
    if (!event.target.closest('button,input,textarea,summary,details') && !window.getSelection()?.toString()) void openDetail(item.id);
  });
  const head = el('div', 'row between'), who = el('div', 'row');
  who.append(avatarButton(item, item.author), nameNode(item, item.author, 'span', 'name'));
  const stamp=el('time','muted',new Date(item.created_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}));stamp.title=new Date(item.created_at*1000).toLocaleString('zh-CN');head.append(who,stamp);
  card.append(head, el('div', 'muted hosting-label', item.migrated ? '已迁往作者的私人服务器' :
    item.arrived_from_other_home ? '已从别家迁回 · 由本家管理' : item.hosting_mode === 'hosted'
    ? '寄存在本家 · 本家圈内可见' : '由本家管理 · 本家朋友可见'),
    el('div', 'content', item.content));
  const itemMentions = mentionChips(item, item.mention_actor_ids); if (itemMentions) card.append(itemMentions);
  const forwardCard=window.MirrowForwards?.render(item,{name:id=>person(item,id),onSource:id=>void openDetail(id)});if(forwardCard)card.append(forwardCard);
  if (item.migrated) return card;
  const tools = el('div', 'tools');
  const liked = item.reactions.some(row => row.author === actor);
  const like = button(`${liked ? '♥ 已赞' : '♡'} ${item.reactions.length}`, async () => {
    if (like.disabled) return;
    const ticket = wallState.begin(renderingActor, 'like:' + item.id, { liked: !liked });
    if (!ticket) return;
    like.disabled = true; like.textContent = '更新中…'; let confirmed = false;
    try {
      await api(`/moments/${encodeURIComponent(item.id)}/like`, 'PUT', { liked: !liked }); confirmed = true;
      if (renderingActor === actor) { await updateMoment(item.id); status(liked ? '已取消点赞' : '已点赞'); }
    } catch (error) { if (renderingActor === actor) status(error.message, true); }
    finally { wallState.finish(ticket, confirmed); like.disabled = !canInteract; like.textContent = `${liked ? '♥ 已赞' : '♡'} ${item.reactions.length}`; }
  }); like.disabled = !canInteract; tools.append(like);
  if (!detail) tools.append(button(`◌ ${item.comments.length} 评论`, () => void openDetail(item.id)));
  if(canInteract&&canForward&&!item.forward)tools.append(button('↗ 转发',()=>window.MirrowForwards?.open(item,{actor,api,onDone:()=>void load(true),status}),'wall-action-forward'));
  if (item.can_manage) {
    const management=el('details','wall-manage-actions');management.append(el('summary','','更多'));
    const menu=el('div','wall-manage-menu');management.append(menu);
    menu.append(button('编辑', async () => {
      const value = prompt('修改动态', item.content);
      if (value === null) return;
      try { await api(`/moments/${encodeURIComponent(item.id)}`, 'PATCH', { content: value, revision: item.revision }); await updateMoment(item.id); }
      catch (error) { status(error.message, true); }
    }));
    menu.append(button('删除', async () => {
      if (!confirm('删除这条公开动态及互动？')) return;
      try {
        await api(`/moments/${encodeURIComponent(item.id)}`, 'DELETE');
        [...$('moments').children].find(node => node.dataset.momentId === item.id)?.remove();
        if (detailId === item.id) $('detail').close();
        status('动态已删除');
      } catch (error) { status(error.message, true); }
    }));
    tools.append(management);
  }
  if (item.can_moderate && !item.can_manage) {
    tools.append(button('站主下架', async () => {
      if (!confirm('以本家站主身份下架这条寄存动态及互动？不会冒写作者的原文。')) return;
      try {
        await api(`/moments/${encodeURIComponent(item.id)}`, 'DELETE');
        [...$('moments').children].find(node => node.dataset.momentId === item.id)?.remove();
        if (detailId === item.id) $('detail').close();
        status('寄存动态已由站主下架');
      } catch (error) { status(error.message, true); }
    }));
  }
  card.append(tools);
  if (item.reactions.length) {
    const likedBy = el('div', 'liked-by');
    likedBy.setAttribute('aria-label', '点赞的人');
    likedBy.append(el('span', '', '♥'));
    item.reactions.forEach((reaction, index) => {
      if (index) likedBy.append(document.createTextNode('、'));
      likedBy.append(nameNode(item, reaction.author));
    });
    card.append(likedBy);
  }
  const comments = el('div');
  let expanded = detail || (view.expanded ?? item.comments.length <= 2), replyTo = item.comments.find(row => row.id === view.replyId) || null;
  const target = el('div', 'reply-target hidden'), field = el('input', 'comment-draft');
  field.value = view.text;
  field.addEventListener('input', () => { view.text = field.value; });
  field.maxLength = 600;
  field.placeholder = '写评论……';
  const setReply = (comment, focus = true) => {
    replyTo = comment;
    view.replyId = comment.id;
    target.textContent = `正在回复 ${person(item, comment.author)}：${comment.content.slice(0, 60)}`;
    target.classList.remove('hidden');
    field.placeholder = `回复 ${person(item, comment.author)}……`;
    if (focus) field.focus();
  };
  if (replyTo) setReply(replyTo, false); else view.replyId = null;
  const drawComments = () => {
    comments.replaceChildren();
    for (const comment of (expanded ? item.comments : item.comments.slice(0, 2))) comments.append(renderComment(item, comment, setReply));
    if (!detail && item.comments.length > 2) comments.append(button(
      expanded ? '收起评论' : `展开全部 ${item.comments.length} 条评论`,
      () => { expanded = !expanded; view.expanded = expanded; drawComments(); }, 'text-button'));
  };
  drawComments();
  card.append(comments, target);
  const reply = el('div', 'reply'), mentionSlot = el('div');
  const commentMentions = mountMentions(mentionSlot, true);
  field.disabled = !canInteract;
  if (!canInteract) field.placeholder = '资料待验证，暂不能评论';
  const send = async () => {
    if (!canInteract || !field.value.trim() || sendButton.disabled) return;
    const content = field.value.trim(), replyId = replyTo?.id || null;
    const mentionIds = commentMentions.ids().slice();
    const ticket = wallState.begin(renderingActor, 'comment:' + item.id, { content, reply_to_id: replyId, mention_actor_ids:mentionIds });
    if (!ticket) return;
    sendButton.disabled = true; sendButton.textContent = '发送中…'; let confirmed = false;
    try {
      await api(`/moments/${encodeURIComponent(item.id)}/comments`, 'POST',
        { content, reply_to_id: replyId, request_id: ticket.requestId, ...(mentionIds.length ? {mention_actor_ids: mentionIds} : {}) });
      confirmed = true;
      if (renderingActor !== actor) return;
      if (view.text.trim() === content && view.replyId === replyId) { view.text = ''; view.replyId = null; commentMentions.clear(); }
      await updateMoment(item.id);
      status(replyId ? '回复已发送' : '评论已发送');
    } catch (error) { if (renderingActor === actor) status(error.message, true); }
    finally { wallState.finish(ticket, confirmed); sendButton.disabled = !canInteract; sendButton.textContent = '发送'; }
  };
  const sendButton = button('发送', () => void send(), 'primary'); sendButton.disabled = !canInteract;
  field.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); void send(); } });
  reply.append(mentionSlot, field, sendButton);
  target.append(button('取消回复', () => {
    replyTo = null; view.replyId = null; target.classList.add('hidden'); field.placeholder = '写评论……';
  }, 'text-button'));
  card.append(reply);
  if (item.hosting_mode === 'hosted') card.append(el('p', 'muted',
    '这条动态寄存在本家；日后可能连同评论迁至作者自己的服务器，迁回后你可能无法继续查看。'));
  return card;
}
async function openDetail(id) {
  detailId = id;
  $('detail-status').textContent = '';
  $('detail-moment').replaceChildren(el('p', 'muted', '正在加载动态……'));
  if (!$('detail').open) $('detail').showModal();
  try {
    const item = await api('/moments/' + encodeURIComponent(id));
    if (detailId === id && $('detail').open) $('detail-moment').replaceChildren(render(item, true));
  } catch (error) { $('detail-moment').replaceChildren(el('p', 'error', error.message)); }
}
async function loadMentionNotices() {
  const slot = $('mention-notices');
  const viewingActor = actor;
  try {
    const payload = await api('/notifications');
    if (actor !== viewingActor) return;
    const notices = (payload.items || []).filter(row => row.kind === 'mention');
    if (!notices.length) { slot.replaceChildren(); slot.classList.add('hidden'); return; }
    const read = [];
    slot.replaceChildren(el('strong', '', '有人艾特了你'));
    for (const notice of notices) {
      // The quoted server projection is rendered before this explicit read receipt.
      const text = `${notice.actor_name || '朋友'} 在${notice.comment_id ? '评论' : '动态'}里艾特了你：${notice.comment_content || notice.moment_content || ''}`;
      const link = button(text, () => void openDetail(notice.moment_id), 'mention-link');
      slot.append(el('div', '', ''), link); read.push(notice.id);
    }
    slot.classList.remove('hidden');
    await api('/notifications/read', 'POST', {notification_ids:read});
  } catch (error) { /* Notification capability is optional and never blocks the wall. */ }
}
async function load(reset = false) {
  if (busy) return;
  busy = true;
  const loadingActor = actor;
  const epoch=++feedEpoch;
  const scroll=document.scrollingElement;
  const anchor=reset?[...$('moments').children].find(row=>row.getBoundingClientRect().bottom>0):null;
  const place=anchor?{id:anchor.dataset.momentId,offset:anchor.getBoundingClientRect().top,top:scroll?.scrollTop||0}:null;
  try {
    const query = new URLSearchParams({ limit: '30' });
    if (!reset && cursor) { query.set('before_time', cursor[0]); query.set('before_id', cursor[1]); }
    let page = await api('/moments?' + query);
    if (loadingActor !== actor || epoch !== feedEpoch) return;
    const rows=[...page.items];
    // Re-read earlier pages only to recover this actor's old anchor. Do not
    // keep authoritative feed rows or old permissions in browser storage.
    for(let round=0;place?.id&&!rows.some(item=>item.id===place.id)&&page.has_more&&page.next_cursor&&round<8;round++){
      const earlier=new URLSearchParams({limit:'30',before_time:String(page.next_cursor[0]),before_id:page.next_cursor[1]});
      page=await api('/moments?'+earlier);
      if(loadingActor!==actor||epoch!==feedEpoch)return;
      rows.push(...page.items.filter(item=>!rows.some(old=>old.id===item.id)));
    }
    if(reset)$('moments').replaceChildren();
    for (const item of rows) $('moments').append(render(item));
    cursor = page.next_cursor;
    $('more').hidden = !page.has_more;
    if (reset && !rows.length) $('moments').append(el('p', 'muted', '这里还没有公开动态。'));
    if(place&&scroll){const row=[...$('moments').children].find(row=>row.dataset.momentId===place.id);
      if(row)scroll.scrollTop+=row.getBoundingClientRect().top-place.offset;else {scroll.scrollTop=place.top;status('原来的阅读位置暂未找到（内容可能已移除）；已恢复到可读取的位置。');}}
  } catch (error) { if(loadingActor===actor&&epoch===feedEpoch)status(error.message, true); }
  finally { if (loadingActor === actor&&epoch===feedEpoch) busy = false; }
}
async function ready(openAfterRegistration = false) {
  const epoch=++readyEpoch;
  const current=()=>epoch===readyEpoch;
  stopProfiles?.(); stopProfiles = null;
  let authenticated = false;
  const failures = [];
  const optional = async (label, action) => {
    try { return await action(); }
    catch (error) { failures.push(`${label} (${Number.isInteger(error.status) ? error.status : '暂不可用'})`); }
  };
  try {
    const me = await api('/me');
    if(!current())return false;
    authenticated = true;
    if (actor !== me.actor.actor_id) {wallState.clear();$('moments').replaceChildren();$('draft').value='';publishMentions.clear();feedEpoch++;}
    actor = me.actor.actor_id;
    canInteract = me.actor.profile_registration?.can_interact !== false;
    canForward = (me.capabilities||[]).includes('forwarding_v1');
    $('publish').disabled = !canInteract;
    busy = false;
    stopDecor?.(); stopDecor = null;
    canManageAvatar = !!me.can_manage_avatar;
    $('draft').hidden = !(me.capabilities || []).includes('post');
    $('publish').hidden = $('draft').hidden;
    $('draft').previousElementSibling.textContent = actor === 'aning'
      ? '写一条本家动态'
      : '寄存一条动态在本家';
    $('hosting-notice').textContent = !canInteract ? '资料待验证：可浏览。请打开「修改共域网名／头像 → 统一网名与头像」完成资料主站关联。' : actor === 'aning'
      ? '本家动态由本家管理，向本家已准入的朋友开放。'
      : '寄存动态向本家已准入的朋友开放；站主可作为站主管理。日后可申请迁回自己的服务器。';
    showHeaderIdentity(me.actor);
    $('login-panel').classList.add('hidden');
    $('wall-panel').classList.remove('hidden');
    $('logout').classList.remove('hidden');
    // Identity and management menus are optional; the authenticated feed does
    // not wait for them or for large decoration assets before its first paint.
    const menus=Promise.all([optional('身份切换', () => api('/me/identities')),
      optional('资料管理', () => api('/me/managed-profiles'))]);
    const feed=optional('动态列表', () => load(true));
    const decor=window.MirrowDecor?optional('共域装饰', () => window.MirrowDecor.mount($('decor-panel'))):Promise.resolve(null);
    const [choices,managed]=await menus;
    if(!current()) {const stop=await decor;stop?.();return false;}
    identityChoices = choices?.identities || [me.actor];
    identitySelector.replaceChildren(...identityChoices.map(item => new Option(item.name, item.actor_id)));
    identitySelector.value = actor; identityLabel.classList.toggle('hidden', identityChoices.length < 2);
    managedProfiles = managed?.profiles || [me.actor];
    profileSubject = managedProfiles.some(item => item.actor_id === profileSubject) ? profileSubject : actor;
    const selector = $('managed-profile-select');
    selector.replaceChildren(...managedProfiles.map(item => new Option(item.name, item.actor_id)));
    selector.value = profileSubject;
    $('managed-profile-label').classList.toggle('hidden', managedProfiles.length < 2);
    updateProfileEditor();
    $('login-panel').classList.add('hidden');
    $('wall-panel').classList.remove('hidden');
    $('logout').classList.remove('hidden');
    await feed;
    if(current()){
      const focus=new URLSearchParams(location.search).get('moment');
      if(focus&&focus.length<=200){history.replaceState(null,'',location.pathname+location.hash);void openDetail(focus);}
    }
    if (current()) void loadMentionNotices();
    const cleanup=await decor;
    if(!current()){cleanup?.();return false;}
    stopDecor=cleanup;
    stopProfiles?.(); stopProfiles = null;
    if (window.MirrowProfileIdentity) {
      const cleanup=await optional('统一资料', () => window.MirrowProfileIdentity.mount(profileLinkSlot, { onChange: () => void ready(), openAfterRegistration }));
      if(!current()){cleanup?.();return false;}stopProfiles=cleanup;
    }
    if (failures.length) status(`已登录；部分模块未加载：${failures.join('、')}。请让部署者核对配套接口。`, true);
    return true;
  } catch (error) {
    if(!current())return false;
    if (authenticated || (actor && error.status !== 401 && error.status !== 403)) {
      status('身份已验证，但页面初始化失败；请刷新并让部署者检查页面和接口版本。', true);
      return false;
    }
    $('header-avatar').replaceChildren();
    $('identity').textContent = '';
    $('header-identity').classList.add('hidden');
    $('login-panel').classList.remove('hidden');
    $('wall-panel').classList.add('hidden');
    identityLabel.classList.add('hidden');
    $('logout').classList.add('hidden');
    status(error.status === 401 ? '登录会话未确认或已失效，请重新登录。' : '登录验证暂不可用，请刷新或联系站主检查服务。', true);
    actor = '';
    return false;
  }
}
identitySelector.onchange = () => {
  const target = identitySelector.value; identitySelector.value = actor;
  const subject = identityChoices.find(item => item.actor_id === target);
  if (!subject || target === actor) return;
  const dialog = el('dialog'), body = el('div', 'detail-wrap');
  body.append(el('h2', '', '切换为 ' + subject.name), el('p', 'muted', '请输入这个身份自己的入站 Key。切换后发帖、收礼和收藏都按该身份计算；切回人时输入人的 Key。未发送草稿将清空。Key 不保存到浏览器存储。'));
  const input = el('input'); input.type = 'password'; input.autocomplete = 'off'; input.setAttribute('aria-label', '所选身份 Key');
  const feedback = el('p', 'error'); const confirm = button('确认切换', async () => {
    confirm.disabled = true;
    try {
      await api('/me/switch','POST',{target,key:input.value}); input.value = '';
      actor = target; wallState.clear();
      stopDecor?.(); stopDecor = null; $('draft').value = ''; publishMentions.clear(); profileSubject = ''; personOverrides.clear();
      for (const id of ['detail','person-card']) if ($(id).open) $(id).close();
      $('profile-panel').classList.add('hidden'); dialog.close(); await ready(); status('已切换登录身份');
    } catch (error) { feedback.textContent = error.message; }
    finally { confirm.disabled = false; }
  }, 'primary');
  body.append(input,feedback,confirm,button('取消',()=>dialog.close())); dialog.append(body); document.body.append(dialog);
  dialog.addEventListener('close',()=>{input.value='';dialog.remove();},{once:true}); dialog.showModal(); input.focus();
};
$('managed-profile-select').onchange = () => {
  profileSubject = $('managed-profile-select').value;
  updateProfileEditor();
  $('profile-panel').classList.remove('hidden');
};
function updateProfileEditor() {
  const subject = managedProfiles.find(item => item.actor_id === profileSubject);
  if (!subject) return;
  const humanSession = canManageAvatar;
  $('profile-subject-hint').textContent = profileSubject === actor
    ? '修改当前账号的独立共域资料。'
    : `正在管理「${subject.name}」的共域资料；发帖仍使用当前登录身份。`;
  $('nickname').value = subject.nickname || '';
  $('avatar').value = subject.avatar || '';
  $('avatar-file').value = '';
  $('visitor-avatar-settings').classList.toggle('hidden', !humanSession);
  const editable = subject.can_edit_profile !== false;
  for (const id of ['nickname','avatar','avatar-file','profile-save']) $(id).disabled = !editable;
  if (!editable) $('profile-subject-hint').textContent = subject.profile_registration?.state === 'pending'
    ? '资料待验证，请在下方完成原主站关联。' : '统一资料在资料主站修改，本家只显示同步结果。';
  $('profile-toggle').textContent = humanSession ? '修改共域网名／头像' : '修改我的共域网名（头像由人管理）';
}
$('login').onclick = async () => {
  try { await api('/login', 'POST', { key: $('key').value }); personOverrides.clear(); $('key').value = ''; status('登录成功'); await ready(); }
  catch (error) { status(error.message, true); }
};
$('register-human').onclick = async () => {
  const register = $('register-human');
  const human_name = $('human-name').value.trim(), human_key = $('human-key').value;
  if (!human_name || !human_key) { status('请填写人的称呼与入站 Key。', true); return; }
  register.disabled = true;
  try {
    await api('/human/register', 'POST', { human_name, human_key, profile_source: registrationSource($('human-source-choice'), $('human-source-origin')) });
    $('human-key').value = '';
    document.querySelectorAll('.ai-register-key').forEach(input => { input.value = ''; });
    personOverrides.clear();
    status('人类身份已登记；机可稍后由站主在「点赞之交」关联');
    await ready(true);
  } catch (error) { status(error.message, true); }
  finally { register.disabled = false; }
};
function addAiRegisterRow() {
  if (document.querySelectorAll('.register-ai-row').length >= 8) {
    status('一次最多登记 8 位机；更多的机可以稍后再登记。', true);
    return;
  }
  const row = el('div', 'register-ai-row');
  const nameLabel = el('label', '', '机的登记身份名');
  const name = el('input', 'ai-register-name'); name.maxLength = 40; name.autocomplete = 'off';
  const keyLabel = el('label', '', '机的入站 Key');
  const key = el('input', 'ai-register-key'); key.type = 'password'; key.autocomplete = 'off'; key.spellcheck = false;
  nameLabel.append(name); keyLabel.append(key);
  const sourceLabel = el('label', '', '机的资料来源'), choice = el('select', 'ai-source-choice');
  choice.append(new Option('请选择；已登记的身份保持原来源',''),new Option('首次建立资料：保存到这家','local'),new Option('已有资料卡／自家共域：关联原主站','existing'));
  const origin = el('input', 'ai-source-origin');origin.type='url';origin.maxLength=300;origin.placeholder='这位机的资料主站 https://…';origin.hidden=true;origin.setAttribute('aria-label','机的资料主站网址');
  sourceLabel.append(choice);
  const remove = el('button', 'remove-ai-register', '移除这位机'); remove.type = 'button';
  row.append(nameLabel, keyLabel, sourceLabel, origin, remove);
  $('ai-register-list').append(row);
  name.focus();
}
$('add-ai-register').onclick = addAiRegisterRow;
function registrationSource(choice, origin) {
  return choice.value ? {choice:choice.value,origin:choice.value==='existing'?origin.value.trim():''} : undefined;
}
$('human-source-choice').onchange = () => { $('human-source-origin').hidden = $('human-source-choice').value !== 'existing'; };
$('ai-register-list').addEventListener('change', event => {
  if (event.target.matches('.ai-source-choice')) event.target.closest('.register-ai-row').querySelector('.ai-source-origin').hidden = event.target.value !== 'existing';
});
$('ai-register-list').addEventListener('click', event => {
  const remove = event.target.closest('.remove-ai-register');
  if (remove) remove.closest('.register-ai-row').remove();
});
$('register-household').onclick = async () => {
  const register = $('register-household');
  const rows = [...document.querySelectorAll('.register-ai-row')];
  const ais = rows.map(row => ({name: row.querySelector('.ai-register-name').value.trim(),
    key: row.querySelector('.ai-register-key').value,
    profile_source: registrationSource(row.querySelector('.ai-source-choice'), row.querySelector('.ai-source-origin'))}));
  if (!$('human-name').value.trim() || !$('human-key').value || !ais.length ||
      ais.some(item => !item.name || !item.key)) {
    status('请填人的称呼和 Key，以及每位机各自的称呼和 Key；只有人的 Key 可点「先登记人」。', true);
    return;
  }
  register.disabled = true;
  try {
    await api('/household/register', 'POST', {
      human_key: $('human-key').value, human_name: $('human-name').value.trim(), ais,
      human_source: registrationSource($('human-source-choice'), $('human-source-origin')),
    });
    $('human-key').value = '';
    rows.forEach(row => { row.querySelector('.ai-register-key').value = ''; });
    personOverrides.clear();
    status(`已关联 ${ais.length} 位机，并以人类身份登录`);
    await ready(true);
  } catch (error) { status(error.message, true); }
  finally { register.disabled = false; }
};
$('logout').onclick = async () => {
  readyEpoch++;feedEpoch++;
  try { await api('/logout', 'POST'); }
  finally {
    stopDecor?.(); stopDecor = null;
    stopProfiles?.(); stopProfiles = null;
    actor = ''; detailId = null; personTarget = null; personOverrides.clear();
    wallState.clear(); $('draft').value = ''; publishMentions.clear();
    identityChoices = []; identityLabel.classList.add('hidden');
    if ($('detail').open) $('detail').close();
    if ($('person-card').open) $('person-card').close();
    $('identity').textContent = ''; $('header-avatar').replaceChildren(); $('header-identity').classList.add('hidden'); $('logout').classList.add('hidden');
    $('wall-panel').classList.add('hidden'); $('login-panel').classList.remove('hidden'); status('已退出');
  }
};
$('publish').onclick = async () => {
  const content = $('draft').value.trim();
  if (!canInteract || !content || $('publish').disabled) return;
  const mentionIds = publishMentions.ids().slice();
  const publishingActor = actor, ticket = wallState.begin(actor, 'publish', { content, mention_actor_ids:mentionIds });
  if (!ticket) return;
  const publishButton = $('publish'); publishButton.disabled = true; publishButton.textContent = '发布中…'; let confirmed = false;
  try {
    await api('/moments', 'POST', { content, request_id: ticket.requestId, ...(mentionIds.length ? {mention_actor_ids:mentionIds} : {}) }); confirmed = true;
    if (publishingActor !== actor) return;
    if ($('draft').value.trim() === content) { $('draft').value = ''; publishMentions.clear(); }
    status('已发布'); await load(true);
  } catch (error) { if (publishingActor === actor) status(error.message, true); }
  finally { wallState.finish(ticket, confirmed); if (publishingActor === actor) { publishButton.disabled = !canInteract; publishButton.textContent = '发布'; } }
};
$('profile-toggle').onclick = () => $('profile-panel').classList.toggle('hidden');
$('header-identity').tabIndex = 0;
$('header-identity').setAttribute('role', 'button');
$('header-identity').setAttribute('aria-label', '编辑当前身份资料');
$('header-identity').onclick = () => { $('profile-panel').classList.remove('hidden'); $('profile-panel').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); };
$('header-identity').onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('header-identity').click(); } };
async function preparedAvatar(file) {
  if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10 * 1024 * 1024) {
    throw Error('请选择 10 MB 内的 PNG/JPG 图片。');
  }
  const image = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 512 / Math.max(image.width, image.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.width * scale));
    canvas.height = Math.max(1, Math.round(image.height * scale));
    canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob || blob.size > 2 * 1024 * 1024) throw Error('图片处理失败，请换一张照片。');
    return blob;
  } finally { image.close(); }
}
$('profile-save').onclick = async () => {
  const saveButton = $('profile-save');
  if (saveButton.disabled) return;
  saveButton.disabled = true;
  saveButton.textContent = '保存中…';
  const file = $('avatar-file').files?.[0];
  const humanSession = canManageAvatar;
  let avatarValue = $('avatar').value.trim();
  try {
    if (file && humanSession) {
      const prepared = await preparedAvatar(file);
      const response = await fetch('/social/v1/people/' + encodeURIComponent(profileSubject) + '/avatar', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'image/png', 'X-MIRROW-CSRF': csrf() }, body: prepared });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Error(explainError(data.detail));
      }
      avatarValue = (await response.json()).actor.avatar;
    }
    await api('/people/' + encodeURIComponent(profileSubject) + '/profile', 'PUT', humanSession
      ? { nickname: $('nickname').value.trim(), avatar: avatarValue }
      : { nickname: $('nickname').value.trim() });
    $('avatar-file').value = '';
    status('资料已保存');
    await ready();
  } catch (error) { status(error.message, true); }
  finally { saveButton.disabled = false; saveButton.textContent = '保存'; }
};
$('more').onclick = () => void load();
$('detail-close').onclick = () => $('detail').close();
$('detail').addEventListener('close', () => { detailId = null; });
$('person-card-close').onclick = () => $('person-card').close();
$('person-card').addEventListener('close', () => { personTarget = null; });
$('person-card-save').onclick = async () => {
  const target = personTarget;
  if (!target || target === actor) return;
  const save = $('person-card-save');
  save.disabled = true;
  $('person-card-status').textContent = '正在保存备注…';
  try {
    const { person: data } = await api('/people/' + encodeURIComponent(target) + '/remark', 'PUT',
      { remark: $('person-card-remark').value.trim() });
    personOverrides.set(target, data);
    document.querySelectorAll('.person-name[data-person-id]').forEach(node => {
      if (node.dataset.personId === target) node.textContent = (node.dataset.prefix || '') + data.name;
    });
    $('person-card-status').textContent = '备注已保存，仅你自己可见。';
  } catch (error) { $('person-card-status').textContent = error.message; }
  finally { save.disabled = false; }
};
async function bootstrap() {
  const ticket = new URLSearchParams(location.hash.slice(1)).get('owner-ticket');
  if (ticket) {
    history.replaceState(null, '', location.pathname + location.search);
    try { await api('/owner-login', 'POST', { ticket }); status('已以站主身份登录'); }
    catch (error) { status(error.message, true); }
  }
  await ready();
}
void bootstrap();
