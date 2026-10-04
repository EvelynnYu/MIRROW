import React from 'react';
import { createRoot } from 'react-dom/client';
import SocialFeedPage from '../src/pages/SocialFeedPage';

// Isolated fixtures: all requests are handled in memory, never sent to MIRROW.
const fixturePeople:Record<string,{nickname:string;avatar:string;name:string}>={aning:{nickname:'演示人',avatar:'',name:'演示人'},k:{nickname:'演示机',avatar:'',name:'演示机'}};
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
  const now = Date.now() / 1000;
  if(url.pathname==='/api/social-sites')return Response.json({sites:[]});
  const personMatch=url.pathname.match(/^\/api\/social-feed\/people\/(aning|k)(\/profile)?$/);
  if(personMatch){
    const person=fixturePeople[personMatch[1]];
    if(init?.method==='PUT'){const value=JSON.parse(String(init.body));person.nickname=value.nickname;person.name=value.nickname;}
    return Response.json({person});
  }
  if (url.pathname === '/api/settings') return Response.json(init?.method === 'POST' ? { success: true } : { userAvatar: '主', aiPersona: { avatar: 'AI' } });
  if (url.pathname === '/api/social-feed/moments') return Response.json({ items: [
    { id: 'fixture-k', author: 'k', content: '演示私密动态：用于测试评论显示。', visibility: 'private', created_at: now, reactions: [], comments: [{ id: 'fixture-comment', author: 'aning', content: '演示评论 A。', created_at: now }] },
    { id: 'fixture-aning', author: 'aning', content: '演示公开动态：用于测试装饰。', visibility: 'public', created_at: now - 500, reactions: [{ author: 'k', reaction: 'like', created_at: now }], comments: [] },
  ], has_more: false });
  if (url.pathname === '/api/social-feed/notifications') return Response.json({ items: [] });
  return Response.json({ detail: '隔离验收未实现该操作' }, { status: 400 });
};
createRoot(document.getElementById('root')!).render(<React.StrictMode><SocialFeedPage onBack={() => {}} /></React.StrictMode>);
