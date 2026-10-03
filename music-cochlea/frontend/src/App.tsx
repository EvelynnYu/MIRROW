import { useEffect, useState } from 'react';
import MusicHost from './music/MusicHost';
import MusicCard from './music/MusicCard';
import AsyncMusicCard from './music/AsyncMusicCard';
import ConversationQuickTools from './music/ConversationQuickTools';
import MusicExperiences from './music/MusicExperiences';
import { openMusic, composeMusic } from './music/api';
import { setMusicDraft, useMusicDraft } from './music/draft';
import { getApiBase, getAccessHeaders } from './shared/config';

async function host(path:string,data?:unknown,method?:string){
  const response=await fetch(getApiBase()+'/api/music-host'+path,{method:method||(data?'POST':'GET'),headers:{'Content-Type':'application/json',...getAccessHeaders()},body:data?JSON.stringify(data):undefined});
  const result=await response.json();if(!response.ok)throw new Error(result.detail||'这次操作未完成');return result;
}
export default function App(){
  const [messages,setMessages]=useState<any[]>([]),[body,setBody]=useState(''),[error,setError]=useState('');
  const [settings,setSettings]=useState(false),[notes,setNotes]=useState(false);
  const [api,setApi]=useState(getApiBase()),[token,setToken]=useState(localStorage.getItem('music_token')||'');
  const draft=useMusicDraft();
  const demo=new URLSearchParams(location.search).get('demo')==='1';
  async function load(){try{setMessages((await host('/messages')).messages);setError('');}catch(e){setError((e as Error).message);}}
  useEffect(()=>{void load();const timer=setInterval(()=>void load(),3000);const view=new URLSearchParams(location.search).get('view');if(view==='player'||view==='playlists')setTimeout(()=>openMusic(view),150);return()=>clearInterval(timer);},[]);
  async function send(){try{await host('/messages',{body,card:draft});setBody('');setMusicDraft(null);await load();}catch(e){setError((e as Error).message);}}
  return <div className="app-shell"><header className="app-top"><span className="brand">MIRROW</span><span className="top-title">共享耳蜗</span><button onClick={()=>setSettings(!settings)}>设置</button></header>
    {settings&&<section className="connection"><label>后端地址<input value={api} onChange={e=>setApi(e.target.value)}/></label><label>访问令牌<input type="password" value={token} onChange={e=>setToken(e.target.value)}/></label><button onClick={()=>{localStorage.setItem('music_api',api.replace(/\/$/,''));localStorage.setItem('music_token',token);location.reload();}}>保存连接</button></section>}
    <div className="app-body"><aside className="app-sidebar"><small>我们的日常</small><button className="selected" onClick={()=>openMusic()}>一起听歌</button><button onClick={()=>openMusic('playlists')}>我们的歌单</button><button onClick={()=>openMusic('shared')}>共享歌库</button><button onClick={()=>setNotes(!notes)}>音乐体验</button><p>留一点时间，<br/>给音乐和彼此。</p></aside>
    <main className="conversation" data-conversation-surface><ConversationQuickTools onSearch={()=>setError('宿主接入口：在你的聊天应用中绑定搜索函数。')}/>
      <div className="chat-heading"><span>日常</span><small>{demo?'虚构资料演示':'音乐模块 · 本地部署'}</small></div>
      {error&&<p className="app-error">{error}</p>}
      {notes&&<MusicExperiences/>}
      <div className="messages">{messages.map(message=>{let calls:any[]=[];try{calls=JSON.parse(message.tool_calls||'[]');}catch{}const mine=message.role==='user';return <article className={'message '+(mine?'mine':'partner')} key={message.id}><small>{mine?'你':demo?'K':'音乐工具'} · 此刻</small>{message.body&&<div className="bubble">{message.body}</div>}{calls.map((call,i)=>{const a=call.extra_data||{};return a.long_text_card?<MusicCard key={i} card={a.long_text_card}/>:a.music_card?<MusicCard key={i} song={a.music_card}/>:a.generation_id?<AsyncMusicCard key={i} attachment={a}/>:null;})}<button className="delete-message" onClick={async()=>{await host('/messages/'+message.id,undefined,'DELETE');await load();}}>删除</button></article>;})}</div>
      <footer className="composer"><div className="composer-tools"><button onClick={()=>composeMusic()}>＋ 歌曲卡片</button><button onClick={()=>openMusic()}>♫ 一起听</button></div>{draft&&<div className="attached">♪ {draft.title}<button onClick={()=>setMusicDraft(null)}>取消添加</button></div>}<textarea placeholder="说点什么，或分享一首歌…" value={body} onChange={e=>setBody(e.target.value)}/><button className="send" disabled={!body.trim()&&!draft} onClick={send}>发送 ↑</button></footer>
    </main></div><MusicHost onAttach={()=>{}}/></div>;
}
