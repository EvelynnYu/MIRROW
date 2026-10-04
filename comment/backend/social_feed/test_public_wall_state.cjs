// Pure transient state tests; no browser, service, credentials or production data.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function fresh() {
  let number = 0;
  const sandbox = { window: {}, crypto: { randomUUID: () => 'fixture-request-' + (++number) } };
  vm.runInNewContext(fs.readFileSync(__dirname + '/public_wall_state.js', 'utf8'), sandbox);
  return sandbox.window.MirrowWallState;
}
test('same in-flight operation is locked; uncertain manual retry reuses receipt', () => {
  const state = fresh(), payload = { content: '一次发布' };
  const first = state.begin('fixture-human', 'publish', payload);
  assert.equal(state.begin('fixture-human', 'publish', payload), null);
  state.finish(first, false);
  const retry = state.begin('fixture-human', 'publish', payload);
  assert.equal(retry.requestId, first.requestId);
  state.finish(retry, true);
  assert.notEqual(state.begin('fixture-human', 'publish', payload).requestId, first.requestId);
});
test('changed content and a different actor never reuse request identity', () => {
  const state = fresh(), first = state.begin('human', 'comment:post', { content: '原文' });
  state.finish(first, false);
  assert.notEqual(state.begin('human', 'comment:post', { content: '新文' }).requestId, first.requestId);
  assert.notEqual(state.begin('ai', 'comment:post', { content: '原文' }).requestId, first.requestId);
});
test('card refresh keeps draft, reply and expansion; login switch clears them', () => {
  const state = fresh(), view = state.view('human', 'post', false);
  view.text = '未发送'; view.replyId = 'comment'; view.expanded = true;
  assert.equal(state.view('human', 'post', false), view);
  assert.equal(state.view('human', 'post', true).text, '');
  assert.equal(state.view('ai', 'post', false).text, '');
  const old = state.begin('human', 'publish', { content: '旧请求' });
  state.clear(); const current = state.begin('human', 'publish', { content: '新请求' });
  state.finish(old, true);
  assert.equal(state.begin('human', 'publish', { content: '新请求' }), null);
  assert.equal(state.view('human', 'post', false).text, '');
  state.finish(current, true);
});
