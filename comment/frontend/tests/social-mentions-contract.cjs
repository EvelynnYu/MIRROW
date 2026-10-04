// Isolated contract check: no network, keys, browser storage, or MIRROW data.
const assert = require('node:assert/strict');
const select = (ids, actor) => ids.includes(actor) ? ids.filter(id => id !== actor) : ids.length < 8 ? [...ids, actor] : ids;
let ids = [];
ids = select(ids, 'actor-a');
assert.deepEqual(ids, ['actor-a']);
ids = select(ids, 'actor-a');
assert.deepEqual(ids, []);
ids = Array.from({length: 8}, (_, index) => `actor-${index}`);
assert.deepEqual(select(ids, 'actor-9'), ids);
assert.equal(JSON.stringify({content:'hello', ...(ids.length ? {mention_actor_ids:ids} : {})}).includes('actor-9'), false);
console.log('social mentions isolated contract: ok');
