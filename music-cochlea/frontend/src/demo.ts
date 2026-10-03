/** Fully fictitious fixtures; demo requests never reach NetEase or a real host. */
const cover = 'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="#d8c2bc"/><circle cx="100" cy="100" r="62" fill="none" stroke="#fff5e9"/><path d="M65 120 Q100 55 140 100" fill="none" stroke="#906b72" stroke-width="3"/><text x="100" y="170" text-anchor="middle" font-family="serif" font-size="14" fill="#695250">EVENING LETTER</text></svg>');
export const demoSong={id:'900001',name:'晚风写了一封信',artist:'纸月乐队',duration:203000,cover,
  lyrics_available:true,lyrics_excerpt:'把一天的声音，慢慢放回晚风里。\n留一盏灯，也留一点时间给你。',
  melody_summary:'约 86 BPM；示例段落能量平稳，后段略微抬升。',material_status:'ready',shared_by:'k' as const,share_number:2};
export function setupDemo() {
  const songs=[demoSong,{...demoSong,id:'900002',name:'雨停以前',artist:'小岛电台'},{...demoSong,id:'900003',name:'慢慢靠近',artist:'纸月乐队'}];
  const playlists=[{id:'900010',name:'我们的晚风',subject:'shared',track_count:12,cover},
    {id:'900011',name:'K 的旧唱片',subject:'k',track_count:8,cover},{id:'900012',name:'我的雨天循环',subject:'owner',track_count:16,cover}];
  const state:any={session:{id:'demo-session',device:'mobile',status:'playing',song:demoSong,mode:'single',quiet:true,
    follow_external:true,queue:songs,queue_index:0,position_ms:72000,pause_timeout_seconds:1800},playlists,capabilities:{}};
  let messages:any[]=[{id:'demo1',role:'user',body:'今晚想安静一点，陪我一起听吧。',tool_calls:'[]'},
    {id:'demo2',role:'assistant',body:'好。把晚风分你一半，我在这里陪你。',tool_calls:JSON.stringify([{extra_data:{attachment_kind:'music',attachment_status:'ready',music_card:demoSong}}])}];
  window.fetch=async (input,init)=>{
    const url=typeof input==='string'?input:input instanceof URL?input.href:input.url;
    const path=new URL(url,location.origin).pathname;
    const body=init?.body?JSON.parse(String(init.body)):{};
    let result:any;
    if(path==='/api/music/v2/status') result=state;
    else if(path==='/api/music/v2/material'||path.startsWith('/api/music/v2/material/')) result={song:demoSong,analysis_pending:false};
    else if(path==='/api/music/v2/session') {if(state.session)Object.assign(state.session,body);result={session:state.session};}
    else if(path==='/api/music/v2/control'){
      if(body.action==='end')state.session=null;
      else if(state.session){state.session.status=body.action==='pause'?'paused':'playing';if(body.action==='next'){state.session.queue_index=(state.session.queue_index+1)%songs.length;state.session.song=songs[state.session.queue_index];}}
      result={session:state.session};
    }
    else if(path==='/api/music/v2/play'){state.session={...state.session,id:'demo-session',song:demoSong,status:'playing',device:body.device,mode:body.mode||'single',queue:songs,queue_index:0,position_ms:72000};result={session:state.session};}
    else if(path==='/api/music/v2/history')result={days:[{date:'2026-10-02',status:'completed',summary:'示例手记：一起听过三首歌。重复播放是观察事实，不直接等于偏好。'}]};
    else if(path==='/api/music/v2/shared-songs')result={songs:songs.map(s=>({...s,share_count:2,cached_share:true}))};
    else if(path==='/api/music/v2/resolve')result={kind:'song',song:demoSong};
    else if(path.startsWith('/api/music/v2/search'))result={songs};
    else if(path==='/api/music/v2/playlists')result={playlists,bindings:playlists};
    else if(path.startsWith('/api/music/v2/playlists/'))result={playlist:playlists[0],songs};
    else if(path==='/api/music-host/messages'){
      if(init?.method==='POST'){messages.push({id:'demo'+Date.now(),role:'user',body:body.body,tool_calls:JSON.stringify(body.card?[{extra_data:{long_text_card:body.card}}]:[])});result={message_id:messages.at(-1).id};}
      else result={messages};
    } else if(path.startsWith('/api/music-host/messages/')&&init?.method==='DELETE'){
      messages=messages.filter(message=>message.id!==path.split('/').at(-1));result={deleted:true};
    } else if(path==='/api/cognition/music')result={records:[],dimensions:[]};
    else return new Response(JSON.stringify({detail:'此演示仅使用虚构本地资料，不执行实际账号操作'}),{status:503,headers:{'Content-Type':'application/json'}});
    return new Response(JSON.stringify(result),{headers:{'Content-Type':'application/json'}});
  };
}
