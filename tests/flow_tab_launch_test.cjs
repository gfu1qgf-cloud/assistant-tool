const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const flush = () => new Promise(resolve => setImmediate(resolve));
const root = path.join(__dirname, '../browser_extensions');
const version = JSON.parse(fs.readFileSync(path.join(root, 'flow_bridge/manifest.json'))).version;
const project = 'https://flow.google.com/project/our-project';
const extension = 'pnjppjjfgiccpcfajkpooklgohgihdom';
const ticket = 'a'.repeat(32);

async function fixture(rows, saved = {}, health = {accepted:true}) {
  const tabs = new Map(rows.map(tab => [tab.id, {...tab}]));
  const hooks = {}, created = [], updates = [], focused = [], messages = [], sockets = [], reloads = [];
  const data = {lzxConfig: {token: 'x'.repeat(40), port: 18189, profile: 'Profile 23', project}, ...saved};
  const event = name => ({addListener(fn) {hooks[name] = fn;}});
  class Socket {
    static OPEN = 1;
    constructor() {this.readyState = 1; this.sent = []; sockets.push(this);}
    send(text) {this.sent.push(JSON.parse(text)); messages.push(JSON.parse(text));}
    close() {this.readyState = 3;}
  }
  const chrome = {runtime: {id: extension, getManifest: () => ({version}),
    onMessage: event('message'), onStartup: event('startup'), onInstalled: event('installed')},
    storage: {onChanged: event('storage'), local: {
      async get(keys) {return Object.fromEntries(keys.filter(key => key in data).map(key => [key, structuredClone(data[key])]));},
      async set(value) {Object.assign(data, structuredClone(value));}, async remove(key) {delete data[key];}}},
    tabs: {async query() {return [...tabs.values()];}, async get(id) {if (!tabs.has(id)) throw Error('gone'); return tabs.get(id);},
      async create(value) {created.push(structuredClone(value)); const tab = {id: 100+created.length, windowId: 42, ...value}; tabs.set(tab.id, tab); return tab;},
      async update(id, value) {if (!tabs.has(id)) throw Error('gone'); updates.push({id, ...value}); Object.assign(tabs.get(id), value); return tabs.get(id);},
      async reload(id) {reloads.push(id);}, async sendMessage() {return health;}},
    windows: {async update(id, value) {focused.push({id, ...value});}},
    downloads: {onChanged: event('download'), async search() {return [];}},
    alarms: {onAlarm: event('alarm')}, action: {onClicked: event('action')}};
  const context = {chrome, WebSocket: Socket, crypto, URL, URLSearchParams, Date,
    console: {log() {}, error() {}, warn() {}}, setTimeout() {return 1;}, clearTimeout() {},
    setInterval() {return 1;}, clearInterval() {}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'flow_adapter/background.js'), 'utf8')
    .replace("import './background.js';", '') + '\nglobalThis.api = {ensureTab, receive, resumeReloadSetup};', context);
  await flush(); sockets[0]?.onopen?.();
  return {api: context.api, tabs, data, created, updates, focused, messages, sockets, hooks, reloads};
}

(async () => {
  const idleRefresh=await fixture([{id:7,windowId:42,url:project,active:true}],{},
    {healthy:true,running:false,native_busy:false});
  await idleRefresh.api.receive({action:'repair_project',request_id:'f5',params:{idle:true,url:project,force_reload:true}});
  assert.deepEqual(idleRefresh.reloads,[7],'explicit F5 reloads the same healthy idle tab');
  assert.equal(idleRefresh.created.length,0);
  await idleRefresh.api.receive({action:'repair_project',request_id:'double-f5',params:{idle:true,url:project,force_reload:true}});
  assert.equal(idleRefresh.reloads.length,1,'repeated F5 respects the existing page recovery gap');
  const runningRefresh=await fixture([{id:7,windowId:42,url:project,active:true}],{},
    {healthy:true,running:true,native_busy:false});
  await runningRefresh.api.receive({action:'repair_project',request_id:'busy-f5',params:{idle:true,url:project,force_reload:true}});
  assert.equal(runningRefresh.reloads.length,0,'even manual F5 must preserve an active generation');
  const starting = await fixture([{id: 7, windowId: 42, active: true,
    url: 'chrome://newtab/', pendingUrl: project+'#lzx_setup='+ticket+'&lzx_port=18189'}]);
  assert.equal(await starting.api.ensureTab(), 7, 'a pending same-project navigation is already our tab');
  assert.equal(starting.created.length, 0, 'startup must not duplicate a project while Chrome is navigating');
  await starting.api.receive({action: 'open_project', request_id: 'open', params: {}});
  assert.deepEqual(starting.focused, [{id: 42, focused: true}]);

  const cold = await fixture([]);
  const opened = await Promise.allSettled([cold.api.ensureTab(), cold.api.ensureTab(), cold.api.ensureTab()]);
  assert(opened.every(value => value.status === 'rejected'));
  assert.equal(cold.created.length, 0, 'only the desktop may open a Flow project, even with concurrent commands');

  const leaving = await fixture([{id: 7, windowId: 42, active: true, url: project,
    pendingUrl: 'https://flow.google.com/project/someone-else'}], {lzxOwnedTab: 7});
  await assert.rejects(leaving.api.ensureTab(), /等待主程序/);
  assert.equal(leaving.created.length, 0, 'do not create a replacement tab if the user navigates away');
  assert.equal(leaving.updates.length, 0, 'never overwrite a tab the user is navigating away');
  const busy = await fixture([{id: 7, windowId: 42, url: project, active: false},
    {id: 8, windowId: 43, url: project, active: true}], {lzxOwnedTab: 7, lzxBatch: 'b'.repeat(32)});
  assert.equal(await busy.api.ensureTab(), 7, 'opening another foreground project cannot steal the running batch');
  assert.equal(busy.created.length, 0);
  await busy.api.receive({action: 'refresh_project', request_id: 'unsafe-refresh', params: {batch_id: 'b'.repeat(32)}});
  assert.equal(busy.reloads.length, 0, 'even a matching active paid batch can never trigger an automatic page reload');
  assert.equal(busy.messages.find(message => message.request_id === 'unsafe-refresh').success, false);

  const restored = await fixture([{id: 9, windowId: 42, active: true, url: 'chrome://newtab/', pendingUrl: project}],
    {lzxReloadSetup: {version, project, mode: 'reload_only', tabId: 99}});
  assert.equal(restored.created.length, 0, 'reload recovery reuses the pending project instead of creating another tab');
  assert.equal(restored.updates[0].id, 9);

  const options = await fixture([{id: 7, windowId: 42, url: project, active: true}]);
  let reply;
  const entry = project+'#lzx_setup='+ticket+'&lzx_port=18189&lzx_action=options';
  options.hooks.message({action: 'lzxSetupFromProject'}, {id: extension, frameId: 0,
    tab: {id: 7}, url: entry}, value => {reply = value;});
  await flush(); const claim = options.sockets.at(-1); claim.onopen();
  assert.equal(claim.sent[0].type, 'setup_reload_check', 'opening a privileged setup page requires a host-approved ticket');
  assert.equal(options.created.length, 0);
  await claim.onmessage({data: JSON.stringify({type: 'setup_reload_allowed', ticket,
    profile: 'Profile 23', project, mode: 'pair'})}); await flush();
  assert.equal(reply.success, true);
  assert.equal(options.created[0].url, `chrome-extension://${extension}/options.html#setup=${ticket}&port=18189`);
  assert.equal(options.created[0].active, true);
  assert.equal(options.created[0].windowId, 42);
  assert.equal(options.data.lzxConfig.token, 'x'.repeat(40), 'showing settings cannot rewrite credentials');
  const fresh = await fixture([{id: 7, windowId: 42, url: project, active: true}]);
  let freshReply;
  fresh.hooks.message({action: 'lzxSetupFromProject'}, {id: extension, frameId: 0,
    tab: {id: 7}, url: entry.replace('lzx_action=options', 'lzx_action=update')}, value => {freshReply = value;});
  await flush(); const approval = fresh.sockets.at(-1); approval.onopen();
  await approval.onmessage({data: JSON.stringify({type: 'setup_reload_allowed', ticket,
    profile: 'Profile 23', project, mode: 'pair'})}); await flush();
  const pairing = fresh.sockets.at(-1); assert.notEqual(pairing, approval); pairing.onopen();
  assert.equal(pairing.sent[0].type, 'setup_claim', 'an already-current extension pairs without a second hash navigation');
  await pairing.onmessage({data: JSON.stringify({type: 'setup_bundle', pairing: {
    setup_id: ticket, port: 18189, extension_id: extension, token: 'y'.repeat(40), profile: 'Profile 23', project}})});
  await flush(); assert.equal(freshReply.success, true);
  assert.equal(fresh.updates.length, 0); assert.equal(fresh.created.length, 0);
  const denied = await fixture([{id: 7, windowId: 42, url: project, active: true}]);
  let deniedReply;
  denied.hooks.message({action: 'lzxSetupFromProject'}, {id: extension, frameId: 0,
    tab: {id: 7}, url: entry}, value => {deniedReply = value;});
  await flush(); const deniedClaim = denied.sockets.at(-1); deniedClaim.onopen();
  await deniedClaim.onmessage({data: JSON.stringify({type: 'setup_reload_denied', error: 'busy'})});
  await flush(); assert.equal(deniedReply.success, false);
  assert.equal(denied.created.length, 0, 'an unapproved/busy setup cannot open privileged tabs');
  const missing = await fixture([], {lzxReloadSetup: {version, project, mode: 'reload_only', tabId: 99}});
  assert.equal(missing.created.length, 0, 'extension reload cannot reopen a closed project');
  assert(missing.data.lzxReloadSetup, 'keep the handoff until the desktop opens its project');
  console.log('Flow tabs: desktop-only navigation, pending tab adoption, foreground activation, safe reload recovery and host-approved options handoff passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
