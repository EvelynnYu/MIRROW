/* Shared music settings: choose one source, prepare audio, then save the home. */
(() => {
  async function editor({parent,draft,localOwner,node,button,field,uploadField,uploadedName,request,openModal,say,safe}) {
    const box=node('section','md-music-editor');
    box.append(node('h3','','背景音乐'),node('p','md-copy','选一种音频来源，准备好后保存布置。进入共域会尝试循环播放；浏览器拦截时点播放即可。'));
    const status=node('p','md-music-current');
    const updateStatus=()=>{
      status.textContent=draft.theme.music
        ? '音频已就绪 · '+(draft.theme.music_title||'共域背景音乐')+'（保存后生效）'
        : draft.theme.music_track ? '仅已选曲 · '+draft.theme.music_track.title+'；还需导入音频才能播放。' : '未设置背景音乐';
    };
    box.append(status);
    const tabs=node('div','md-music-tabs');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','背景音乐来源');
    const filePane=node('div','md-music-source'),cloudPane=node('div','md-music-source');
    filePane.setAttribute('role','tabpanel');cloudPane.setAttribute('role','tabpanel');
    const advanced=node('details','md-music-advanced');advanced.append(node('summary','','显示名（可选）'));
    const title=field('音乐显示名',draft.theme.music_title,v=>{draft.theme.music_title=v;updateStatus();},{max:200});
    const titleInput=title.querySelector('input');advanced.append(title,node('small','md-copy','留空使用默认名称；这只改变顶栏显示，不决定是否有音频。'));
    const fileStatus=await uploadField(filePane,'上传 MP3 / WAV','aning',draft.theme,'music',true,()=>{
      // A local file becomes the sole selected source; old cloud metadata is not its authority.
      draft.theme.music_track=null;
      const name=uploadedName();
      if(draft.theme.music&&name)draft.theme.music_title=name.slice(0,200);
      titleInput.value=draft.theme.music_title;updateStatus();
    });
    filePane.append(node('small','md-copy','文件不超过 10 MB，最长 10 分钟。上传后仍需保存布置。'));
    let cloudButton,fileButton;
    function selectSource(cloud){
      cloudPane.hidden=!cloud;filePane.hidden=cloud;
      cloudButton?.setAttribute('aria-selected',String(cloud));fileButton.setAttribute('aria-selected',String(!cloud));
    }
    if(localOwner){
      cloudButton=button('网易云导入',()=>selectSource(true));cloudButton.setAttribute('role','tab');tabs.append(cloudButton);
      const selection=node('p','md-music-selection',draft.theme.music_track?'当前选曲：'+draft.theme.music_track.title:'先选曲，再导入完整音频。');
      cloudPane.append(button('从共享耳蜗选歌',safe(async()=>{
        const data=await request('/music-choices'),[picker,list]=openModal('共享耳蜗 · 选择背景音乐');
        list.append(node('p','md-copy','选歌只是选择来源；下一步导入完整音频，不控制网易云播放器。'));
        if(!data.songs.length)list.append(node('p','md-copy','暂无缓存歌曲，可返回填写网易云歌曲链接。'));
        for(const song of data.songs)list.append(button(song.title+' · '+song.artist,()=>{
          draft.theme.music_track=song;selection.textContent='当前选曲：'+song.title;updateStatus();picker.close();
        }));
      })),selection);
      const links=node('details','md-music-advanced');links.append(node('summary','','或者粘贴歌曲链接 / ID'));
      let source='';links.append(field('网易云歌曲链接 / ID','',v=>source=v,{max:2048}),node('small','md-copy','留空使用上方选曲；不支持歌单或分享短链接。'));cloudPane.append(links);
      const rightsLabel=node('label','md-music-rights'),rights=node('input');rights.type='checkbox';
      rightsLabel.append(rights,node('span','','我确认有权将此音乐用于共域，供获准访问的朋友播放。'));
      const importer=button('导入完整音频',safe(async()=>{
        if(!rights.checked)throw Error('请先确认音乐的分享权限。');
        const selected=source.trim()||draft.theme.music_track?.id;if(!selected)throw Error('先选曲或填写网易云歌曲链接。');
        rights.disabled=true;const oldLabel=importer.textContent;importer.textContent='正在导入…';
        say('正在获取、校验与压缩完整音频，原音乐保持不变。');
        try{
          const result=await request('/music-import','POST',{source:selected,sharing_rights_confirmed:true});
          if(!box.isConnected)return;
          draft.theme.music=result.id;draft.theme.music_title=result.track.title;draft.theme.music_track=result.track;
          titleInput.value=result.track.title;fileStatus.textContent='已导入 · '+result.track.title;
          selection.textContent='已导入：'+result.track.title;updateStatus();
          say('完整音频已就绪，点击“保存这次布置”后启用。');
        }finally{rights.disabled=false;importer.textContent=oldLabel;}
      }),'md-primary');
      importer.disabled=true;rights.onchange=()=>importer.disabled=!rights.checked;
      cloudPane.append(rightsLabel,importer,node('small','md-copy','试听片段不能导入，失败不替换旧音乐。'));
    }
    fileButton=button('上传本地音频',()=>selectSource(false));fileButton.setAttribute('role','tab');tabs.append(fileButton);
    box.append(tabs,cloudPane,filePane,advanced);
    box.append(button('移除背景音乐',()=>{
      draft.theme.music='';draft.theme.music_title='';draft.theme.music_track=null;titleInput.value='';
      filePane.querySelector('input[type=file]').value='';fileStatus.textContent='尚未上传';updateStatus();
      say('背景音乐已从草稿移除，保存后生效。');
    },'md-music-remove'));
    parent.append(box);selectSource(localOwner&&Boolean(draft.theme.music_track||!draft.theme.music));updateStatus();
  }
  window.MirrowDecorMusic={editor};
})();
