'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../sanwufan/static/app.js'), 'utf8');

async function client(pathname, firstView) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      textContent: '', value: '', checked: false, hidden: false, open: false,
      classList: {toggle() {}, add() {}, remove() {}}, dataset: {},
      addEventListener() {}, close() {},
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    location: {pathname, origin: 'http://localhost', href: `http://localhost${pathname}`}, URL, console,
    performance: {now: () => 0}, localStorage: {getItem: () => null},
    ResizeObserver: class {observe() {}},
    document: {body: node('body'), getElementById: node,
      querySelector: node, querySelectorAll: () => [], addEventListener() {}},
    setTimeout() {}, setInterval() {},
    nextView: firstView,
  });
  context.fetch = async () => ({ok: true, json: async () => context.nextView});
  vm.runInContext(source, context);
  // Keep the actual poll/accept/connection path. Rendering is exercised in the browser.
  vm.runInContext('render = renderSocial = renderChat = renderSession = () => {};', context);
  await new Promise(resolve => setImmediate(resolve));
  return {context, node,
    state: () => JSON.parse(vm.runInContext('JSON.stringify(state)', context)),
    async poll(view) {context.nextView = view; await vm.runInContext('poll()', context);},
  };
}

async function main() {
  const practiceView = {table_id: 'practice', version: 0, player_seat: 0, hand: [], phase: 'waiting'};
  const practice = await client('/', practiceView);
  assert.equal(practice.node('connection').textContent, '已连接', 'First practice snapshot must connect');
  assert.equal(practice.state().table_id, 'practice');
  await practice.poll({...practiceView, version: 1, hand: ['H:2']});
  assert.equal(practice.state().hand[0], 'H:2');
  assert.equal(practice.node('connection').textContent, '已连接');

  const friends = await client('/friends', {mode: 'lobby', name: ''});
  assert.equal(friends.state(), null);
  assert.equal(friends.node('connection').textContent, '已连接');
  const friendView = {...practiceView, table_id: 'friends', mode: 'friends', room_code: '123456',
    players: [0, 1, 2, 3].map(seat => ({seat, name: `玩家${seat}`, occupied: true})),
    chat_sequence: 2, chat_messages: [{id: 2, text: '一起打牌'}]};
  await friends.poll(friendView);
  assert.equal(friends.state().room_code, '123456');
  await friends.poll({...friendView, version: 1, chat_sequence: 1, chat_messages: []});
  assert.equal(friends.state().chat_sequence, 2, 'Older chat response must not erase new messages');
  assert.equal(friends.state().chat_messages[0].id, 2);
  console.log('Frontend first-load and chat snapshot checks passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
