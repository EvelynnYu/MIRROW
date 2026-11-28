import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const js = ts.transpile(source('../src/lib/wanderDisplay.ts').replace(
  "import activityCatalog from '../../shared/presence/activity-visuals.json';",
  () => 'const activityCatalog = ' + source('../shared/presence/activity-visuals.json') + ';').replace("import shelfCatalog from '../../shared/presence/shelf-visuals.json';",
    () => 'const shelfCatalog = ' + source('../shared/presence/shelf-visuals.json') + ';'), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext });
const { activityVisual, wanderPresence, plainActivity } = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

test('each runtime activity has a distinct visual, including older decorated titles', () => {
  const enums = source('../../ai-chat-backend/wander_manager/event_types.py').split('class WanderEvent:')[0];
  const ids = Array.from(enums.matchAll(/^    [A-Z_]+\s*=\s*['"](\w+)['"]/gm), match => match[1]);
  assert.ok(ids.length > 0);
  assert.deepEqual(Object.keys(JSON.parse(source('../shared/presence/activity-visuals.json'))).sort(), [...ids].sort());
  const icons = ids.map(id => activityVisual(id).icon);
  assert.equal(new Set(icons).size, ids.length);
  assert.ok(!icons.includes('💭'));
  assert.equal(activityVisual(undefined, '💭 自己听歌').icon, activityVisual('listen_music').icon);
  assert.equal(plainActivity('🎮 玩游戏 · 完成了两步'), '玩游戏 · 完成了两步');
});

test('presence distinguishes execution, waits, rest, review', () => {
  const status = {runtime_state:'active', mode:'idle', current_activity_state:'node_running', current_event_type:'listen_music'};
  assert.equal(wanderPresence(status).label,'正在听歌');
  assert.equal(wanderPresence({...status,current_activity_state:'node_review'}).label,'在回顾刚才的活动');
  const wait = {...status,current_activity_state:'active_wait',next_node_at:'2026-11-18T15:20:00'};
  assert.match(wanderPresence(wait).detail,/听歌.*15:20/);
  assert.equal(wanderPresence({...wait,current_event_type:'sleep'}).label,'休息中');
  assert.equal(wanderPresence({...status,runtime_state:'scheduled'}).label,'漫想间隔');
  assert.equal(wanderPresence(null).label, '状态待同步');
});

test('bookshelf uses observed sources and a mixed visit keeps the shelf icon', () => {
  assert.equal(activityVisual('browse_bookmarks', '', ['bookmarks','bookmarks']).icon, '🔖');
  assert.equal(activityVisual('browse_bookmarks', '', ['essays','bookmarks']).icon, '📚');
  assert.equal(activityVisual(undefined,'翻收藏夹').icon,'🔖');
});
