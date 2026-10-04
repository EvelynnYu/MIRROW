/* Reusable image studio; uploaded bytes only, never local filesystem paths. */
(() => {
  const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  function button(text,action){const b=el('button',text);b.type='button';b.onclick=async()=>{b.disabled=true;try{await action();}finally{b.disabled=false;}};return b;}
  async function pick(ctx,subject,category,apply){
    const data=await ctx.request('/presets'); const [dialog,body]=ctx.open('素材库');
    const grid=el('div');grid.className='md-preset-grid';body.append(grid);
    for(const item of data.assets.filter(a=>a.category===category)){
      const choice=button(item.name,async()=>{
      try{const result=await ctx.request('/presets/'+encodeURIComponent(subject),'POST',{id:item.id});await apply(result.id);dialog.close();}
      catch(e){ctx.say(e.message,true);}
      });
      if(item.preview?.startsWith('data:image/png;base64,')){const img=el('img');img.src=item.preview;img.alt='';img.className='md-preset-image';choice.prepend(img);}
      grid.append(choice);
    }
    if(!grid.children.length)body.append(el('p','这类素材还没有收录。可以上传自己的图片；内置主题和头像框在各自的预设列表中选择。'));
  }
  async function gif(ctx,subject,apply){
    const [dialog,body]=ctx.open('一键制作 GIF');
    body.append(el('p','选 2～8 张静态 PNG/JPG/WebP，按顺序播放；原图总计不超过 10 MB。已有透明背景会保留，这里不会自动抠图。'));
    const input=el('input');input.type='file';input.multiple=true;input.accept='image/png,image/jpeg,image/webp';body.append(input);
    let files=[],result='',urls=[];
    const frames=el('div');frames.className='md-gif-frames';body.append(frames);
    const status=el('p');status.role='status';const preview=el('img');preview.className='md-gift-image';preview.hidden=true;
    function reset(){result='';preview.hidden=true;use.disabled=true;}
    function draw(){urls.forEach(URL.revokeObjectURL);urls=[];frames.replaceChildren();reset();files.forEach((file,i)=>{
      const row=el('div');row.className='md-gif-frame';const img=el('img');img.alt=file.name;img.src=URL.createObjectURL(file);urls.push(img.src);
      row.append(img,el('small',(i+1)+'. '+file.name));
      if(i)row.append(button('前移',()=>{[files[i-1],files[i]]=[files[i],files[i-1]];draw();}));
      row.append(button('移除',()=>{files.splice(i,1);draw();}));frames.append(row);
    });}
    input.onchange=()=>{const next=Array.from(input.files||[]);if(next.length<2||next.length>8||next.reduce((n,f)=>n+f.size,0)>10*1024*1024){status.textContent='请选择 2～8 张图片，总大小在 10 MB 内。';return;}files=next;draw();status.textContent='按需要调整顺序，然后点生成。';};
    const speed=el('select');for(const [value,text] of [['normal','标准速度'],['slow','慢一点'],['fast','快一点']]){const o=el('option',text);o.value=value;speed.append(o);}speed.onchange=reset;body.append(speed);
    const checks={};for(const [key,text] of [['ping_pong','来回播放'],['smooth','平滑过渡（不自然时可关闭）']]){const l=el('label'),c=el('input');c.type='checkbox';c.checked=true;c.onchange=reset;checks[key]=c;l.append(c,document.createTextNode(text));body.append(l);}
    const use=button('使用这张 GIF',async()=>{if(result){await apply(result);dialog.close();}});use.disabled=true;
    const encode=file=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=()=>reject(Error('图片读取失败'));r.readAsDataURL(file);});
    const generate=button('生成并预览',async()=>{
      if(files.length<2){status.textContent='先选至少两张图片。';return;}
      input.disabled=true;speed.disabled=true;Object.values(checks).forEach(c=>c.disabled=true);frames.inert=true;reset();
      try{status.textContent='正在制作，稍等一下…';const data=await ctx.request('/gif/'+encodeURIComponent(subject),'POST',{frames:await Promise.all(files.map(encode)),speed:speed.value,ping_pong:checks.ping_pong.checked,smooth:checks.smooth.checked});
        if(!dialog.open)return;result=data.id;preview.src=await ctx.asset(result);preview.hidden=false;use.disabled=false;
        status.textContent='已生成 '+(data.size/1024/1024).toFixed(2)+' MB · '+(data.mode==='smooth'?'平滑动画':'逐帧动画')+'。检查预览后再使用。';
      }catch(e){status.textContent=e.message;}finally{input.disabled=false;speed.disabled=false;Object.values(checks).forEach(c=>c.disabled=false);frames.inert=false;}
    });
    body.append(generate,status,preview,use);dialog.addEventListener('close',()=>urls.forEach(URL.revokeObjectURL),{once:true});
  }
  window.MirrowDecorStudio={pick,gif};
})();
