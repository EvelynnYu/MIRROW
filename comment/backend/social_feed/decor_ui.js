/* Shared by the public wall and MIRROW's desktop/mobile page. */
(() => {
  const node = (tag, cls = '', text) => { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const copy = value => JSON.parse(JSON.stringify(value));
  const fontChoices = [['sans','清爽无衬线'],['serif','书页宋体'],['hand','手写楷体'],
    ['calligraphy','毛笔手写 · 马善政'],['flower','中英花体 · Great Vibes'],
    ['neon','霓虹微光'],['outline','糖纸描边'],['gold','鎏金花体']];
  const errors = {
    profile_verification_pending: '资料来源待验证：可以浏览和收礼。请在个人资料里的「统一网名与头像」完成关联后再保存装饰。',
    design_conflict: '另一处已保存更新，请关闭编辑器后刷新再试。', asset_not_owned: '请重新上传属于当前身份的图片。',
    image_dimensions_exceeded: '图片尺寸或 GIF 帧数过大，请缩小后上传（最多 120 帧）。',
    invalid_decor_image: '请选择有效的 PNG、JPG、WebP 或 GIF 图片。', media_too_large: '请选择 10 MB 以内的文件。',
    media_quota_exceeded: '这个身份的物料空间已满，请联系本家主人。', invalid_decor_audio: '音乐读取失败，请选择 MP3 或 WAV。',
    audio_converter_unavailable: '本家还没有配置音乐转换程序。', k_context_unavailable: 'AI 的聊天上下文还没就绪，请先打开主聊天再试。',
    home_link_required: '请先完成「关联我的共域」。', invalid_link_proof: '两家身份确认未完成，请检查站点配置后重试。',
    social_site_unavailable: '这家暂时未连通或尚未升级装饰模块，请稍后再试。', link_proof_unavailable: '对方无法访问你的共域，请检查隧道是否在线。',
    gift_updates_not_ready:'赠礼更新尚未启用，请由站主完成备份迁移。', preset_catalog_invalid:'素材清单有错误，请核对目录中的 catalog.json。',
    gif_processing_busy:'还有一张 GIF 正在制作，稍后再试。',gif_generation_failed:'制作失败，请检查静态图片、减少图片大小，或关闭平滑过渡重试。',invalid_gif_request:'请选择 2～8 张静态图片。',
    invalid_import_song:'请填写网易云标准歌曲链接或歌曲 ID，不支持歌单与短链接。',
    music_import_unavailable:'这首歌的完整音频暂不可获取，请检查网易云授权或换一首歌；原音乐未替换。',
    music_import_not_configured:'本家尚未接入网易云账号导入接口，请联系部署者。',
    music_import_preview_only:'网易云只提供试听片段，未导入、未替换原音乐。',
    music_import_duration_unsupported:'自动导入支持完整的 10 分钟以内歌曲。',
    music_import_incomplete:'下载音频与整首歌曲时长不符，未保存，请稍后重试。',
    music_import_timeout:'导入等待超时，未替换原音乐；请手动重试。',
    music_import_busy:'已有歌曲正在导入，请等待完成后重试。',
    audio_probe_unavailable:'本家未配置音频校验程序 ffprobe，请联系部署者。',
  };
  function button(text, action, cls = '') {
    const b = node('button', cls, text); b.type = 'button';
    b.onclick = async () => { if (b.disabled) return; b.disabled = true; try { await action(); } finally { b.disabled = false; } };
    return b;
  }
  function field(label, value, change, options = {}) {
    const wrap = node('label', 'md-field'), title = node('span', '', label);
    const input = options.choices ? node('select') : node(options.lines ? 'textarea' : 'input');
    input.setAttribute('aria-label', label);
    if (options.choices) for (const [v, t] of options.choices) { const o = node('option', '', t); o.value = v; input.append(o); }
    else { input.type = options.type || 'text'; input.maxLength = options.max || 500; }
    input.value = value || ''; input.oninput = () => change(input.value);
    wrap.append(title, input); return wrap;
  }
  function modal(title, parent) {
    const d = node('dialog', 'md-dialog'), head = node('div', 'md-toolbar');
    head.append(node('h2', '', title), button('关闭', () => d.close()));
    const body = node('div', 'md-dialog-body'); d.append(head, body); parent.append(d);
    d.addEventListener('close', () => d.remove(), { once: true }); d.showModal(); return [d, body];
  }

  async function mount(container, options = {}) {
    const api = options.api || '/social/v1/decor';
    const root = options.root || document.body;
    const ownerKey=Symbol.for('mirrow.decor.owner'), owner={};root[ownerKey]=owner;
    const ownsRoot=()=>root[ownerKey]===owner;
    const headers = options.headers || {};
    const backgroundTarget = root.querySelector('.social-feed-body,.social-remote-body') || container.closest('main') || root;
    let disposed = false, state, music, musicId = '', musicPanel, observer, schedule, painting = false, paintAgain = false, refreshing = false, renderVersion = 0, backgroundVersion = 0, cancelAutoplay = () => {};
    const urls = new Map(), cleanups = [], dialogs = new Set(), uploadNames = new WeakMap();
    container.classList.add('md-panel'); root.classList.add('md-domain');
    backgroundTarget.classList.add('md-background-surface');
    const message = node('p', 'md-message'); message.setAttribute('role', 'status');
    const shelf = node('section', 'md-shelf'), toolbar = node('div', 'md-toolbar');
    const musicSlot = root.querySelector('[data-social-music-slot]') || root.querySelector('header .row') || toolbar;
    container.replaceChildren(shelf, toolbar, message);
    const say = (text, bad = false) => { message.textContent = text; message.classList.toggle('md-error', bad);
      const active=[...dialogs].at(-1)?.querySelector('.md-dialog-status');if(active){active.textContent=text;active.classList.toggle('md-error',bad);}
    };
    const csrf = () => decodeURIComponent((document.cookie.match(/(?:^|; )mirrow_wall_csrf=([^;]*)/) || [, ''])[1]);
    async function request(path, method = 'GET', data, base = api) {
      const response = await fetch(base + path, { method, credentials: 'same-origin', cache: 'no-store',
        headers: { ...headers, ...(data !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(method === 'GET' ? {} : { 'X-MIRROW-CSRF': csrf() }) },
        body: data !== undefined ? JSON.stringify(data) : undefined });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(errors[result.detail] || result.detail || '暂时无法连接这家的装饰服务');
      return result;
    }
    const safe = fn => async () => { try { await fn(); } catch (e) { say(e.message, true); } };
    let studioLoading;
    async function studio(){
      if(!window.MirrowDecorStudio){
        if(!studioLoading)studioLoading=new Promise((resolve,reject)=>{const s=document.createElement('script');
          const address=new URL(api,location.href);
          s.src=address.origin+(address.pathname.startsWith('/api/')?'/api/social-decor-ui/studio':'/social/v1/decor/studio.js');s.onload=resolve;s.onerror=()=>{studioLoading=null;s.remove();reject(Error('图片工作室暂未加载，请重试。'));};document.head.append(s);});
        await studioLoading;
      }
      return {ui:window.MirrowDecorStudio,ctx:{request,open:openModal,say,asset}};
    }
    async function asset(id, base = api) {
      if (!id) return '';
      const key = base + id;
      if (!urls.has(key)) urls.set(key, (async () => {
        const r = await fetch(base + '/media/' + encodeURIComponent(id), { credentials: 'same-origin', headers });
        if (!r.ok) throw Error('图片暂时无法加载');
        const url = URL.createObjectURL(await r.blob());
        if (disposed) { URL.revokeObjectURL(url); return ''; }
        return url;
      })().catch(error => { urls.delete(key); throw error; }));
      return urls.get(key);
    }
    async function image(id, cls, base = api) {
      const img = node('img', cls); img.alt = ''; img.loading = 'lazy'; img.decoding = 'async';
      if (id) try { img.src = await asset(id, base); } catch (_) { img.hidden = true; }
      else img.hidden = true;
      return img;
    }
    async function theme(value, target = root) {
      if(disposed||!ownsRoot())return;
      const version = target === root ? ++backgroundVersion : 0;
      target.dataset.mdTheme = value.preset; target.dataset.mdFont = value.font; target.dataset.mdSize = value.text_size;
      target.dataset.mdFit = value.background_fit || 'width';
      target.style.setProperty('--md-accent', value.accent);
      for (const [key, css] of [['background', '--md-background'], ['card_background', '--md-card-image']]) {
        let url = '';
        if (value[key]) try { url = await asset(value[key]); } catch (_) { /* Missing decoration never blocks the feed or gifts. */ }
        if (disposed || !ownsRoot() || target === root && version !== backgroundVersion) return;
        let surface = target;
        if (key === 'background' && target === root) {
          let landscape = false;
          if (url) landscape = await new Promise(resolve => {
            const probe = new Image(); probe.onload = () => resolve(probe.naturalWidth >= probe.naturalHeight);
            probe.onerror = () => resolve(false); probe.src = url;
          });
          if (disposed || !ownsRoot() || version !== backgroundVersion) return;
          surface = landscape ? root : backgroundTarget;
          for (const old of new Set([root, backgroundTarget])) {
            old.classList.remove('md-background-surface'); old.style.removeProperty('--md-background'); delete old.dataset.mdFit;
          }
        }
        if (key === 'background') { surface.dataset.mdFit = value.background_fit || 'width'; surface.classList.add('md-background-surface'); }
        surface.style.setProperty(css, url ? `url("${url}")` : 'none');
      }
    }
    async function renderShelf(home = state.home, version = renderVersion) {
      const heading = node('div', 'md-shelf-heading');
      heading.append(node('span', 'md-eyebrow', 'MIRROW · COMMENT / CABINET'), node('h2', '', home.theme.shelf_name));
      const grid = node('div', 'md-exhibits');
      grid.style.setProperty('--md-exhibit-columns',String(Math.min(3,Math.max(1,home.exhibits.length))));
      const exhibits = await Promise.all(home.exhibits.map(async exhibit => {
        const b = button('', safe(async () => {
          const [dialog, content] = openModal(exhibit.name);
          content.append(await image(exhibit.image, 'md-object-large'), node('p', 'md-copy', exhibit.description));
          if (exhibit.human_note) content.append(node('p', 'md-note', '人类主人的留言 · ' + exhibit.human_note));
          if (exhibit.ai_note) content.append(node('p', 'md-note', '机的留言 · ' + exhibit.ai_note));
          if (!state.owner) content.append(button('查看 / 补收本次赠礼', safe(async () => {
            dialog.close(); await receiveGifts(true);
          })));
        }), 'md-exhibit');
        b.append(await image(exhibit.image, 'md-object'), node('span', 'md-object-fallback', exhibit.image ? '' : '✧'), node('strong', '', exhibit.name));
        return b;
      }));
      grid.append(...exhibits);
      if (!home.exhibits.length) {
        grid.classList.add('md-exhibits-empty');
        const empty=node('div','md-empty');
        empty.append(node('span','md-empty-mark','✧'),node('strong','', '故事还没上架'),node('span','',state.owner ? '把那些有故事的小东西，慢慢摆在这里。' : '这里留着空位，等主人添几件小东西。'));
        grid.append(empty);
      }
      if (!disposed && ownsRoot() && version === renderVersion) shelf.replaceChildren(heading, grid);
    }
    function openModal(title) {
      const result = modal(title, container); dialogs.add(result[0]);
      const status=node('p','md-message md-dialog-status');status.setAttribute('role','status');result[0].insertBefore(status,result[1]);
      result[0].addEventListener('close', () => dialogs.delete(result[0])); return result;
    }
    async function uploadField(parent, label, subject, data, key, audio = false, changed = () => {}) {
      const row = node('div', 'md-upload');
      const preview = await image(audio ? '' : data[key], 'md-thumb'); row.append(preview);
      const names = uploadNames.get(data) || {}; uploadNames.set(data, names);
      const saved = node('small', 'md-upload-status', data[key] ? '已上传 · ' + (names[key] || (audio ? '音乐文件' : '图片已保留')) : '尚未上传');
      const l = node('label', 'md-field'), f = node('input'); f.type = 'file';
      f.accept = audio ? 'audio/mpeg,audio/wav' : 'image/png,image/jpeg,image/webp,image/gif';
      l.append(node('span', '', label), f, saved); row.append(l);
      row.append(button('清除', () => { data[key] = ''; delete names[key]; f.value = ''; saved.textContent = '尚未上传'; preview.hidden = true; changed(); })); parent.append(row);
      if(!audio){
        const apply=async id=>{data[key]=id;preview.src=await asset(id);preview.hidden=false;saved.textContent='图片已就绪，保存后生效';changed();};
        const category=key==='frame_asset'?'frame':key==='background'?'background':key==='card_background'||key==='card_asset'?'card':'exhibit';
        row.append(button('从素材库选择',safe(async()=>{const s=await studio();await s.ui.pick(s.ctx,subject,category,apply);})),
          button('制作 GIF',safe(async()=>{const s=await studio();await s.ui.gif(s.ctx,subject,apply);})));
      }
      f.onchange = async () => {
        const file = f.files[0]; if (!file) return;
        if (file.size > 10 * 1024 * 1024) { say('文件需在 10 MB 内。', true); return; }
        f.disabled = true; say('正在处理物料…');
        try {
          const response = await fetch(api + '/media/' + encodeURIComponent(subject) + '?kind=' + (audio ? 'audio' : 'image'), {
            method: 'POST', credentials: 'same-origin', headers: { ...headers, 'Content-Type': 'application/octet-stream', 'X-MIRROW-CSRF': csrf() }, body: file });
          const result = await response.json(); if (!response.ok) throw Error(errors[result.detail] || result.detail);
          data[key] = result.id; names[key] = file.name; saved.textContent = '已上传 · ' + file.name;
          if (!audio) { preview.src = await asset(result.id); preview.hidden = false; }
          say('物料已就绪；保存后展示。'); changed();
        } catch (e) { say(e.message, true); } finally { f.disabled = false; }
      };
      return saved;
    }
    async function previewDesign(target, home, personal = {}, name = '这是一条预览动态') {
      target.classList.add('md-domain', 'md-preview'); await theme(home.theme, target);
      target.replaceChildren(node('h3', '', home.theme.shelf_name));
      const exhibitRow = node('div', 'md-preview-objects');
      for (const exhibit of home.exhibits) {
        const item = node('div'); item.append(await image(exhibit.image, 'md-object'), node('small', '', exhibit.name)); exhibitRow.append(item);
      }
      target.append(exhibitRow);
      const post = node('article', 'md-preview-post'); post.dataset.mdCard = personal.card || 'inherit'; post.dataset.mdFont = !personal.font || personal.font==='inherit'?home.theme.font:personal.font;
      const avatar = node('span', 'md-framed md-preview-avatar', 'AI'); avatar.dataset.mdFrame = personal.frame || 'none';
      if (personal.frame_asset) avatar.style.setProperty('--md-frame-image', `url("${await asset(personal.frame_asset)}")`);
      const bg = personal.card_asset ? await asset(personal.card_asset) : '';
      if (bg) post.style.backgroundImage = `url("${bg}")`;
      post.append(avatar, node('strong', '', name), node('p', 'md-preview-text', '把这一刻，留在这里。和朋友们分享正在发生的事。'), node('small', '', '♡ 点赞　·　写一句回应…'));
      target.append(post);
      post.append(button('♡ 点赞 · 点缀色预览', () => {}, 'md-preview-accent'));
    }
    let musicEditorLoading;
    async function editMusic(parent,draft) {
      if(!window.MirrowDecorMusic){
        if(!musicEditorLoading)musicEditorLoading=new Promise((resolve,reject)=>{
          const script=document.createElement('script'),address=new URL(api,location.href);
          script.src=address.origin+(address.pathname.startsWith('/api/')?'/api/social-decor-ui/music-editor':'/social/v1/decor/music-editor.js');
          script.onload=resolve;script.onerror=()=>{musicEditorLoading=null;script.remove();reject(Error('音乐编辑器暂未加载，请重试。'));};
          document.head.append(script);
        });
        await musicEditorLoading;
      }
      await window.MirrowDecorMusic.editor({parent,draft,localOwner:state.owner&&options.localOwner,
        node,button,field,uploadField,uploadedName:()=>uploadNames.get(draft.theme)?.music,request,openModal,say,safe});
    }
    async function editHome() {
      const presets=await request('/presets'), syncGifts=new Set();
      const draft = copy(state.home), [dialog, body] = openModal('布置我的共域');
      dialog.classList.add('md-home-editor');
      body.classList.add('md-editor-layout');
      const previewPane = node('aside', 'md-preview-pane'), editor = node('div', 'md-editor-fields');
      const toggle = node('label', 'md-preview-toggle'), floating = node('input'); floating.type = 'checkbox';
      floating.checked=true; dialog.classList.add('md-preview-floating');
      toggle.append(floating, document.createTextNode('悬浮预览 · 随编辑保持可见'));
      dialog.insertBefore(toggle, body);
      floating.onchange = () => { dialog.classList.toggle('md-preview-floating', floating.checked); };
      const preview = node('div', 'md-preview');
      const status = node('p', 'md-copy', '图片 / GIF ≤ 10 MB，GIF 最多 120 帧；音乐 ≤ 10 MB，最多保留前 10 分钟。');
      const previews = node('div', 'md-toolbar');
      previews.append(button('手机预览', () => { preview.style.maxWidth = '350px'; }), button('宽屏预览', () => { preview.style.maxWidth = '100%'; }));
      previewPane.append(node('small', '', '未保存预览'), previews, preview); editor.append(status);
      body.append(previewPane, editor);
      const themeTab = node('details', 'md-section'); themeTab.open = true;
      themeTab.append(node('summary', '', '主题与文字'));
      const refresh = safe(async () => { await previewDesign(preview, draft); });
      const themeFields=node('div','md-settings-grid');
      for (const [key, label, choices] of [
        ['preset','底色',presets.themes.map(t=>[t.id,t.name])],
        ['font','文字',fontChoices],
        ['text_size','字号',[['small','小'],['normal','标准'],['large','大']]],
        ['background_fit','背景铺法',[['width','适应动态栏宽度（不拉伸）'],['cover','填满动态栏（可能裁切）'],['tile','平铺图案']]],
      ]) themeFields.append(field(label, draft.theme[key], v => { draft.theme[key] = v; refresh(); }, { choices }));
      themeTab.append(themeFields);
      const themeChoices=node('div','md-preset-grid');
      for(const preset of presets.themes){const b=button(preset.name,()=>{draft.theme.preset=preset.id;draft.theme.accent=preset.accent;
        themeTab.querySelector('select').value=preset.id;themeTab.querySelector('input[type=color]').value=preset.accent;refresh();});
        const swatch=node('span','md-preset-swatch');swatch.style.background=preset.accent;b.prepend(swatch);themeChoices.append(b);}
      const presetDetails=node('details','md-preset-details');presetDetails.append(node('summary','','浏览预制主题'),themeChoices);themeTab.append(presetDetails);
      themeTab.append(field('点缀色', draft.theme.accent, v => { draft.theme.accent = v; refresh(); }, { type: 'color' }));
      themeTab.append(node('small','md-copy','点缀色用于点赞、按钮、链接和选中强调，不改变背景图。'));
      themeTab.append(field('展架名字', draft.theme.shelf_name, v => { draft.theme.shelf_name = v; refresh(); }, { max: 40 }));
      await uploadField(themeTab,'动态栏背景','aning',draft.theme,'background',false,refresh);
      themeTab.append(node('small','md-copy','竖版背景仅铺中间动态栏；横版／方图铺整个共域。内站与网站一致。'));
      await uploadField(themeTab,'动态背景卡','aning',draft.theme,'card_background',false,refresh);
      editor.append(themeTab);
      await editMusic(editor,draft);
      const items = node('div'), controls = node('div', 'md-toolbar');
      async function renderEditors() {
        items.replaceChildren();
        for (const [index, entry] of draft.exhibits.entries()) {
          const box = node('details', 'md-section'); box.append(node('summary', '', `${index + 1}. ${entry.name || '新展品'}`));
          box.append(field('物品名', entry.name, v => { entry.name = v; refresh(); }, { max: 40 }));
          box.append(field('物品描述', entry.description, v => entry.description = v, { lines: true, max: 500 }));
          await uploadField(box,'架上展示图','aning',entry,'image',false,refresh);
          await uploadField(box,'发放物料图（留空沿用展示图）','aning',entry,'gift_image');
          box.append(field('你给访客的留言', entry.human_note, v => entry.human_note = v, { lines: true, max: 200 }));
          box.append(field('AI 给访客的留言', entry.ai_note, v => entry.ai_note = v, { lines: true, max: 200 }));
          const updateLabel=node('label'),update=node('input');update.type='checkbox';update.checked=syncGifts.has(entry.id);
          update.onchange=()=>update.checked?syncGifts.add(entry.id):syncGifts.delete(entry.id);
          updateLabel.append(update,document.createTextNode('同步本次修改到已发放礼物的收藏夹（含图片与留言）'));box.append(updateLabel,
            node('small','md-copy','未勾选则只修改展架；勾选后访客下次来或检查更新时刷新收藏，不重新送礼。'));
          let facts = '';
          box.append(field('告诉 AI 这件东西是什么、想起哪段经历', '', v => facts = v, { lines: true, max: 1500 }));
          box.append(button('请 AI 帮我拟稿', safe(async () => {
            if (!facts.trim()) throw Error('先给 AI 一点物品事实或回忆线索。');
            say('AI 正在结合人格和记忆拟稿，完成后会填在这里…');
            const result = await request('/draft', 'POST', { facts, current: entry });
            // A concurrent upload belongs to the editor; the model returns text only.
            for (const key of ['name','description','ai_note']) entry[key] = result[key];
            await renderEditors(); items.children[index].open = true; say('草稿已填好，检查后再保存。');
          })));
          const actions = node('div', 'md-toolbar');
          if (index) actions.append(button('向前摆', safe(async () => { [draft.exhibits[index-1],draft.exhibits[index]]=[draft.exhibits[index],draft.exhibits[index-1]]; await renderEditors(); })));
          actions.append(button('撤下', safe(async () => { draft.exhibits.splice(index, 1); await renderEditors(); })));
          box.append(actions); items.append(box);
        }
        await refresh();
      }
      controls.append(button('＋ 添一件（最多 5 件）', safe(async () => {
        if (draft.exhibits.length >= 5) throw Error('架上最多摆 5 件。');
        draft.exhibits.push({ id: crypto.randomUUID().replaceAll('-',''), name:'新展品',description:'',image:'',gift_image:'',human_note:'',ai_note:'' });
        await renderEditors(); items.lastElementChild.open = true;
      })), button('查看更新预览', safe(async () => { await previewDesign(preview, draft); previewPane.scrollIntoView({block:'nearest',behavior:'smooth'}); say('预览已更新；布置尚未保存。'); })), button('放大预览', safe(async () => {
        const [, panel] = openModal('布置预览 · 尚未保存'); const full = node('div'); panel.append(full); await previewDesign(full, draft);
      })));
      editor.append(items, controls); await renderEditors(); await refresh();
      const footer=node('footer','md-editor-footer');
      footer.append(node('small','','预览不会改变已发布的布置'),button('保存这次布置', safe(async () => { state.home = await request('/home','PUT',{...draft,sync_gift_exhibits:[...syncGifts].filter(id=>draft.exhibits.some(e=>e.id===id))}); say('共域布置已保存。'); dialog.close(); await render(); }), 'md-primary'));
      dialog.append(footer);
    }
    async function giftSettings() {
      const [, body] = openModal('给来访的人和机备礼');
      body.append(node('p','md-copy','人和机各备一份，可选同一件或不同展品。每位来访身份自动收一份；再次发放会建立新的一版，朋友下次来会再收到。已收礼物永久留在收藏里。'));
      for (const [kind, label] of [['human','给人类朋友'],['ai','给机朋友']]) {
        const active = state.gifts.find(g => g.kind === kind);
        let selected = active?.snapshot.id || '';
        const box = node('section','md-section');
        box.append(field(label,selected,v => selected=v,{choices:[['','暂不发放'],...state.home.exhibits.map(e=>[e.id,e.name])]}));
        box.append(button('应用并发放这一版', safe(async () => { const result = await request('/gifts','POST',{kind,exhibit_id:selected,request_id:crypto.randomUUID()}); state.gifts=result.gifts; say(selected?'新一版礼物已备好。':'已停止这类身份的新礼物发放。'); })));
        body.append(box);
      }
    }
    async function editPerson() {
      const presets=await request('/presets');
      const [, body] = openModal('身份装饰与跨域同步');
      let selected = state.profiles[0]; const form = node('div');
      body.append(field('管理谁的装饰',selected.actor,v=>{selected=state.profiles.find(p=>p.actor===v); draw();},{choices:state.profiles.map(p=>[p.actor,p.name])}),form);
      async function draw() {
        form.replaceChildren(); const design=copy(selected.design), subject=selected.actor;
        const preview = node('div'); form.append(preview);
        const refresh = safe(() => previewDesign(preview, state.home, design, selected.name)); await refresh();
        const controls = node('fieldset'); controls.className = 'md-personal-controls';
        controls.disabled = selected.can_edit_decor === false;
        if (controls.disabled) form.append(node('p','md-copy',errors.profile_verification_pending));
        form.append(controls);
        controls.append(field('头像框预设',design.frame,v=>{design.frame=v;design.frame_asset='';refresh();},{choices:presets.frames.map(f=>[f.id,f.name])}));
        await uploadField(controls,'自定义透明头像框 / GIF',subject,design,'frame_asset',false,refresh);
        await uploadField(controls,'我发布的动态背景图 / GIF',subject,design,'card_asset',false,refresh);
        controls.append(field('我发布的动态背景卡',design.card,v=>{design.card=v;refresh();},{choices:[['inherit','跟随共域'],['paper','奶油纸'],['rose','玫瑰纸'],['ink','深墨纸']]}));
        controls.append(field('我的动态文字',design.font,v=>{design.font=v;refresh();},{choices:[['inherit','跟随共域'],...fontChoices]}));
        controls.append(button('保存身份装饰',safe(async()=>{const result=await request('/people/'+encodeURIComponent(subject),'PUT',design); selected.design=result.design; await paint(); say(result.sync_results?.some(r=>r.status!=='success')?'本家已保存；部分朋友家未同步，恢复后点重试。':'身份装饰已保存。');})));
        if (state.owner) {
          const sync=copy(selected.sync);
          form.append(node('h3','','让这套装饰出现在哪些共域'));
          form.append(field('展示范围',sync.scope,v=>sync.scope=v,{choices:[['local','只在本家'],['all','所有已注册共域'],['selected','我选择的共域']]}));
          for (const site of state.sites) {
            const row=node('div','md-toolbar'),label=node('label'),check=node('input');check.type='checkbox'; check.checked=sync.site_ids.includes(site.id);
            check.onchange=()=>sync.site_ids=check.checked?[...new Set([...sync.site_ids,site.id])]:sync.site_ids.filter(id=>id!==site.id);
            label.append(check,document.createTextNode(site.name)); row.append(label,button('关联 / 验证',safe(async()=>{await request('/sites/'+site.id+'/link/'+subject,'POST',{});say('已与'+site.name+'确认这位身份。');}))); form.append(row);
          }
          form.append(node('p','md-copy','未列出的朋友家，请先到「点赞之交 → 已注册的共域」填写共域网址与对方给这位身份的 Key。头像框与动态样式可同步，整站背景、音乐和奇物架留在自家。'));
          form.append(button('保存范围并同步 / 重试',safe(async()=>{const result=await request('/people/'+subject+'/sync','PUT',sync);selected.sync=copy(sync);say(result.results.some(r=>r.status!=='success')?'部分共域未连通；本家已保存，稍后可以重试。':'展示范围已保存，同步完成。');})));
        } else {
          const guide=node('div','md-section'); guide.append(node('h3','','关联我的共域'));
          guide.append(node('p','md-copy','已有自家共域：打开自家页面，以主人身份进入「身份装饰」，找到这家并点“关联 / 验证”。两家确认后即可选择同步范围。还没有自家共域也可以保存本站的个人装饰。'));
          let origin=selected.link?.origin || '';
          guide.append(field('自家共域网址',origin,v=>origin=v,{type:'url'}));
          guide.append(button('打开自家共域 ↗',safe(async()=>{const u=new URL(origin);if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash)throw Error('请填写 HTTPS 共域首页地址。');u.hash='link-source='+encodeURIComponent(location.origin);window.open(u.href,'_blank','noopener,noreferrer');})));
          if(selected.link)guide.append(button('解除本站身份关联',safe(async()=>{await request('/link/'+encodeURIComponent(subject),'DELETE');selected.link=null;say('关联已解除。已显示的装饰仍可在本站修改。');})));
          form.append(guide);
        }
      }
      await draw();
    }
    async function showGifts(gifts, local = false, collection = false, subject = '') {
      const [dialog, body] = openModal(collection ? '我的收藏' : '主人给你留了一份礼物');
      const source = local ? (options.localApi || '/api/social-decor') : api;
      for (const gift of gifts) {
        const item=gift.snapshot,card=node('article','md-gift');
        card.append(await image(item.gift_image||item.image,'md-gift-image',source),node('h3','',item.name),node('p','md-copy',item.description));
        if(item.human_note)card.append(node('p','md-note','人类主人的留言 · '+item.human_note));
        if(item.ai_note)card.append(node('p','md-note','机的留言 · '+item.ai_note));
        card.append(node('small','',new Date(gift.received*1000).toLocaleString()));body.append(card);
      }
      if(!gifts.length)body.append(node('p','md-copy','收藏里还没有礼物。去朋友家坐坐吧。'));
      if(!collection) {
        body.append(node('p','md-copy','已经自动收进你的收藏。'));
        const acknowledge=safe(async()=>{for(const g of gifts)await request('/seen','POST',{id:g.id},source);});
        dialog.addEventListener('close',acknowledge,{once:true});
        body.append(button('收到啦',()=>dialog.close(),'md-primary'));
      } else {
        body.append(button('检查赠礼更新',safe(async()=>{const data=await request('/collection/refresh','POST',{subject});dialog.close();await showGifts(data.items,false,true,subject);
          say(data.results?.some(r=>r.status!=='success')?'部分赠礼方暂时离线，保留已有收藏。':'已检查赠礼的最新展示。');})));
        if(gifts.length===50)body.append(button('看更早的收藏',safe(async()=>{const next=await request('/collection?before='+gifts.at(-1).received+'&subject='+encodeURIComponent(subject));dialog.close();await showGifts(next.items,false,true,subject);}))); 
      }
    }
    async function render() {
      if(disposed||!ownsRoot())return;
      const version=++renderVersion,home=state.home;
      // Controls and audio appear immediately; slow media is presentation, not
      // a prerequisite for receipts, profile onboarding or cleanup ownership.
      const surfaces=Promise.all([theme(home.theme),renderShelf(home,version)]);
      toolbar.replaceChildren();
      if(state.owner)toolbar.append(button('布置奇物架与主题',safe(editHome)),button('给访客备礼',safe(giftSettings)));
      if(state.owner)toolbar.append(button('赠礼发放记录',safe(async()=>{
        const [,body]=openModal('赠礼发放记录');
        const summary=node('p','md-copy');body.append(summary);
        async function page(cursor) {
          const data=await request('/deliveries'+(cursor?'?before_time='+cursor.time+'&before_id='+encodeURIComponent(cursor.id):''));
          summary.textContent='本家共域累计发放 '+data.total+' 份；人和机分别记账。';
          for(const row of data.items)body.append(node('p','md-note',`${row.name}（${row.kind==='human'?'人':'机'}） · ${row.gift_name}\n${new Date(row.received*1000).toLocaleString()}`));
          if(!data.total)body.append(node('p','md-copy','还没有访客赠礼收据。'));
          if(data.has_more){const more=button('看更早的发放记录',safe(async()=>{await page(data.next_cursor);more.remove();}));body.append(more);}
        }
        await page();
      })));
      if(state.human)toolbar.append(button('身份装饰',safe(editPerson)));
      toolbar.append(button('收藏',safe(async()=>{
        const show=async subject=>{const data=await request('/collection?subject='+encodeURIComponent(subject));await showGifts(data.items,false,true,subject);};
        if(state.profiles.length===1){await show(state.actor);return;}
        const [picker,body]=openModal('看看谁的收藏');
        for(const profile of state.profiles)body.append(button(profile.name,safe(async()=>{picker.close();await show(profile.actor);}))); 
      })));
      toolbar.append(button('刷新布置', safe(() => refreshState(true))));
      if (!state.owner) toolbar.append(button('补收 / 查看赠礼', safe(() => receiveGifts(true))));
      void renderMusic().catch(()=>{});
      await surfaces;
    }
    async function renderMusic() {
      const current = state.home.theme;
      // Refreshing decoration must preserve a visitor's intentional pause.
      if (musicId === current.music && musicPanel && music) {
        const name=musicPanel.querySelector('.md-music-name');
        name.textContent = current.music_title || '共域背景音乐';name.title=name.textContent;
        musicSlot.append(musicPanel); return;
      }
      cancelAutoplay(); music?.pause(); music = null; musicPanel?.remove(); musicPanel = null; musicId = current.music || '';
      const track = current.music_track;
      if (!current.music && !track) return;
      musicPanel = node('span','md-music'); musicPanel.setAttribute('aria-label','共域背景音乐');
      const name = node('span','md-music-name', current.music ? (current.music_title || '共域背景音乐') : track.title);
      name.title = name.textContent; musicPanel.append(name); musicSlot.append(musicPanel);
      if (!current.music) {
        const pending=button('待导入',()=>{});pending.disabled=true;
        pending.title='仅保存了选曲；主人导入音频并保存布置后可播放。';pending.setAttribute('aria-label','背景音乐待导入');
        musicPanel.append(pending);return;
      }
      const audio = node('audio'); music = audio; audio.loop = true; audio.preload = 'metadata'; audio.volume = .3;
      const play = button('正在加载…', async () => {
        cancelAutoplay(); if (audio.paused) await attempt(); else audio.pause();
      });
      play.setAttribute('aria-label','播放背景音乐'); musicPanel.append(play, audio);
      function pausedLabel(label) { play.textContent=label;play.title=label==='▶'?'播放背景音乐':label;play.setAttribute('aria-label','播放背景音乐');play.setAttribute('aria-pressed','false'); }
      audio.addEventListener('play', () => { play.textContent='Ⅱ';play.title='暂停背景音乐';play.setAttribute('aria-label','暂停背景音乐');play.setAttribute('aria-pressed','true'); });
      audio.addEventListener('pause', () => pausedLabel('▶'));
      audio.addEventListener('error', () => pausedLabel('音频不可用 · 重试'));
      async function attempt() {
        if(disposed||!ownsRoot()||music!==audio)return;
        try { await audio.play(); if (music === audio) cancelAutoplay(); }
        catch (e) {
          pausedLabel(e.name === 'NotAllowedError' ? '▶' : '播放失败 · 重试');
          if(e.name==='NotAllowedError')play.title='浏览器限制自动播放，点此播放背景音乐';
          if (e.name === 'NotAllowedError' && music === audio && !disposed) {
            cancelAutoplay();
            const retry = event => { if (!musicPanel.contains(event.target) && music === audio && !disposed) void attempt(); };
            document.addEventListener('pointerdown',retry); document.addEventListener('keydown',retry);
            cancelAutoplay = () => { document.removeEventListener('pointerdown',retry); document.removeEventListener('keydown',retry); };
          }
        }
      }
      try {
        audio.src = await asset(current.music);
        if (!disposed && ownsRoot() && music === audio) await attempt();
      } catch (_) { pausedLabel('音频不可用 · 重试'); }
    }
    function remoteSiteId() {
      if (!options.localApi) return '';
      const match = String(api).match(/\/api\/social-sites\/([^/]+)\/decor$/);
      return match ? decodeURIComponent(match[1]) : '';
    }
    async function showHumanGiftNotices() {
      if (disposed || !ownsRoot() || !state.human || dialogs.size) return;
      const source = options.localApi || api;
      const siteId = remoteSiteId();
      const data = await request('/human-gift-notices' + (siteId ? '?site_id=' + encodeURIComponent(siteId) : ''), 'GET', undefined, source);
      const gifts = data.items || [];
      if (!gifts.length || disposed || !ownsRoot() || dialogs.size) return;
      const first = gifts[0];
      const [dialog, body] = openModal('你家小机 ' + (first.machine_name || 'AI') + ' 收到了一份礼物');
      for (const gift of gifts) {
        const item = gift.snapshot, card = node('article', 'md-gift');
        card.append(node('small', '', '收礼的小机 · ' + (gift.machine_name || 'AI')));
        card.append(await image(item.gift_image || item.image, 'md-gift-image', source), node('h3', '', item.name), node('p', 'md-copy', item.description));
        if (disposed || !dialog.open) return;
        if (item.human_note) card.append(node('p', 'md-note', '人类主人的留言 · ' + item.human_note));
        if (item.ai_note) card.append(node('p', 'md-note', '机的留言 · ' + item.ai_note));
        card.append(node('small', '', new Date(gift.received * 1000).toLocaleString())); body.append(card);
      }
      body.append(node('p', 'md-copy', '这是小机的收藏；这里只记录你已经看过这份收据。'));
      const acknowledge = safe(async () => { if (!disposed) await request('/human-gift-notices/seen', 'POST', { receipt_ids: gifts.map(g => g.id) }, source); });
      dialog.addEventListener('close', acknowledge, { once: true });
      body.append(button('知道啦', () => dialog.close(), 'md-primary'));
    }
    async function receiveGifts(explicit = false) {
      const arrived = await request('/visit','POST',{});
      if (arrived.delivery_pending) say('赠礼暂未同步完成，下次进入会重试；仍可正常浏览共域。');
      if (arrived.gifts?.length) await showGifts(arrived.gifts,arrived.local_receipts);
      else if (explicit) say('本轮赠礼已收妥或主人尚未给这类身份备礼，可在收藏里查看。');
    }
    async function refreshState(explicit = false) {
      if (disposed || refreshing || document.hidden || dialogs.size) return;
      refreshing = true;
      try {
        const fresh = await request('/state'); if (disposed) return;
        const homeChanged = JSON.stringify(fresh.home) !== JSON.stringify(state.home);
        state = fresh; if (homeChanged || explicit) await render(); await paint();
        await receiveGifts(); if (explicit) say('已读取这家的最新布置。');
        await showHumanGiftNotices();
      } finally { refreshing = false; }
    }
    async function paint() {
      if(disposed||!ownsRoot())return;if(painting){paintAgain=true;return;}painting=true;
      try {
        const elements=[...root.querySelectorAll('[data-social-actor],[data-social-post-author]')];
        const ids=[...new Set(elements.map(e=>e.dataset.socialActor||e.dataset.socialPostAuthor))].filter(Boolean);
        if(!ids.length)return;
        const data={people:{}};
        for(let offset=0;offset<ids.length;offset+=50)Object.assign(data.people,(await request('/people?actors='+encodeURIComponent(ids.slice(offset,offset+50).join(',')))).people);
        await Promise.all(elements.map(async element=>{
          const design=data.people[element.dataset.socialActor||element.dataset.socialPostAuthor];if(!design)return;
          if(element.dataset.socialPostAuthor){element.dataset.mdCard=design.card;element.dataset.mdFont=design.font==='inherit'?state.home.theme.font:design.font;
            const bg=design.card_asset?await asset(design.card_asset):'';
            if(disposed||!ownsRoot())return;
            if(bg)element.style.setProperty('--md-personal-bg',`url("${bg}")`);else element.style.removeProperty('--md-personal-bg');
          }
          else {element.dataset.mdFrame=design.frame;element.classList.add('md-framed');
            const frame=design.frame_asset?await asset(design.frame_asset):'';
            if(disposed||!ownsRoot())return;
            if(frame)element.style.setProperty('--md-frame-image',`url("${frame}")`);else element.style.removeProperty('--md-frame-image');
          }
        }));
      }catch(_){/* Feed remains readable while decoration service is offline. */}finally{painting=false;if(paintAgain&&!disposed){paintAgain=false;clearTimeout(schedule);schedule=setTimeout(paint,200);}}
    }
    try {
      state=await request('/state'); if(disposed)return()=>{};
      // Receive first: a broken theme asset/audio must not swallow a gift.
      try { await receiveGifts(); } catch(e) { say('赠礼暂未收妥：' + e.message,true); }
      void render().then(()=>paint()).catch(e=>{if(!disposed)say('部分装饰暂未加载：'+e.message,true);});
      await showHumanGiftNotices();
      void paint();observer=new MutationObserver(records=>{if(!records.some(r=>[...r.addedNodes].some(n=>n.nodeType===1&&(n.matches?.('[data-social-actor],[data-social-post-author]')||n.querySelector?.('[data-social-actor],[data-social-post-author]')))))return;clearTimeout(schedule);schedule=setTimeout(paint,200);});observer.observe(root,{childList:true,subtree:true});
      const resume = () => { if (!document.hidden) void safe(refreshState)(); };
      for (const type of ['focus','pageshow','mirrow-app-resume']) { window.addEventListener(type,resume); cleanups.push(() => window.removeEventListener(type,resume)); }
      document.addEventListener('visibilitychange',resume); cleanups.push(() => document.removeEventListener('visibilitychange',resume));
      const timer = setInterval(resume,45000); cleanups.push(() => clearInterval(timer));
    } catch(e) { say('奇物架暂时不可用：'+e.message,true); }
    return () => { disposed=true;cancelAutoplay();observer?.disconnect();clearTimeout(schedule);cleanups.forEach(fn=>fn());music?.pause();musicPanel?.remove();dialogs.forEach(d=>d.close());
      urls.forEach(p=>p.then(u=>URL.revokeObjectURL(u)).catch(()=>{}));container.replaceChildren();
      if(!ownsRoot())return;delete root[ownerKey];root.classList.remove('md-domain');
      ['--md-background','--md-card-image','--md-accent'].forEach(p=>root.style.removeProperty(p));
      ['mdTheme','mdFont','mdSize','mdFit'].forEach(p=>delete root.dataset[p]);
      for (const surface of new Set([root,backgroundTarget])) { surface.classList.remove('md-background-surface');surface.style.removeProperty('--md-background');delete surface.dataset.mdFit; }
    };
  }
  window.MirrowDecor={mount};
})();
