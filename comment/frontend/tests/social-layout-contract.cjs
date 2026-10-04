// Structure-only regression checks; no fetch, credentials, databases or model calls.
const {readFileSync,existsSync}=require('node:fs');
const {resolve}=require('node:path');
const assert=require('node:assert/strict');
const frontend=resolve(__dirname,'..');
const backend=resolve(frontend,existsSync(resolve(frontend,'../backend/social_feed'))?'../backend':'../ai-chat-backend');
const read=path=>readFileSync(path.startsWith('ai-chat-frontend/')
  ?resolve(frontend,path.slice('ai-chat-frontend/'.length))
  :resolve(backend,path.slice('ai-chat-backend/'.length)),'utf8');
for(const path of ['components/SocialTimelineCard.tsx','pages/SocialFeedPage.tsx','pages/SocialRemoteFeed.tsx']){
  const source=read('ai-chat-frontend/src/'+path);
  assert.match(source,/<div className="social-feed-comment-box"><SocialMentions compact /,path);
  assert.ok(!source.includes('</div><SocialMentions endpoint='),'mention must not occupy a separate comment row');
  assert.ok(source.includes('social-feed-action-forward'),'quiet forwarding action');
  assert.ok(source.includes('mention_actor_ids'),'stable identity contract preserved');
}
const picker=read('ai-chat-frontend/src/components/SocialMentions.tsx');
assert.match(picker,/<dialog ref={dialog}/);
assert.match(picker,/showModal\(\)/);
assert.match(picker,/aria-pressed={selected.includes\(person.actor_id\)}/);
const filters=read('ai-chat-frontend/src/components/SocialFeedFilters.css');
assert.ok(!filters.includes('grid-row:2'),'music must not force a second header row');
const decor=read('ai-chat-backend/social_feed/decor_ui.css');
assert.match(decor,/\.md-panel:not\(\.social-decor-compact\)>\.md-toolbar\{display:grid/);
assert.match(decor,/\.md-empty\{grid-column:1\/-1/);
const runtime=read('ai-chat-backend/social_feed/decor_ui.js');
assert.ok(runtime.includes("floating.checked=true"));
assert.ok(runtime.includes("dialog.append(footer)"));
const wall=read('ai-chat-backend/social_feed/public_wall.js');
assert.ok(wall.includes('reply.append(mentionSlot, field, sendButton)'));
assert.ok(wall.includes("panel.showModal()"));
assert.ok(wall.includes("!panel.open"),'closed picker cannot accept late responses');
console.log('social UI layout contract: home/remote/timeline/public OK');
