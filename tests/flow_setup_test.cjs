const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const id = 'pnjppjjfgiccpcfajkpooklgohgihdom';
const project = 'https://labs.google/fx/tools/flow/project/selected-project';
const nextProject = 'https://flow.google.com/project/another-project';
const flush = () => new Promise(resolve => setImmediate(resolve));

function options(saved = {}, ticket = '', live = true, version = '') {
  const fields = Object.fromEntries(['pair', 'profile', 'project', 'save', 'status'].map(name => [name,
    {value: '', textContent: '', disabled: false, events: {}, addEventListener(name, callback) {this.events[name] = callback;}}]));
  const data = {lzxConfig: structuredClone(saved)}, writes = [], commands = [], hooks = [];
  let reloads = 0;
  const chrome = {tabs: {async query() {return [];}}, runtime: {getManifest() {return {version};}, reload() {reloads++;}, async sendMessage(message) {commands.push(message); return {connected: live, profile: data.lzxConfig.profile,
    project: data.lzxConfig.project, setupId: data.lzxConfig.setup_id || ''};}}, storage: {local: {
    async get() {return structuredClone(data);}, async set(value) {writes.push(structuredClone(value)); Object.assign(data, structuredClone(value));}
  }}};
  const sockets = [], replacements = [];
  class Socket {
    constructor(url) {this.url = url; this.sent = []; sockets.push(this);}
    send(value) {this.sent.push(JSON.parse(value));} close() {this.closed = true;}
  }
  const context = {chrome, URL, URLSearchParams, WebSocket: Socket, addEventListener(name, fn) {hooks[name] = fn;},
    location: {hash: ticket ? '#setup='+ticket+'&port=18189' : '', pathname: '/options.html'},
    history: {replaceState(...args) {replacements.push(args);}},
    setTimeout(callback, ms) {if (ms === 500) queueMicrotask(callback); return 1;}, clearTimeout() {}, document: {getElementById(name) {return fields[name];}}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/options.js'), 'utf8'), context);
  return {fields, data, writes, sockets, replacements, context, hooks, commands, reloads: () => reloads};
}

async function background(config, ownedUrl) {
  const data = {lzxConfig: config, lzxOwnedTab: ownedUrl ? 7 : null};
  const hooks = {}, sockets = [], created = [], sent = [];
  const event = name => ({addListener(callback) {hooks[name] = callback;}});
  class Socket {
    static OPEN = 1;
    constructor() {this.readyState = 1; sockets.push(this);}
    send(value) {sent.push(JSON.parse(value));}
    close() {this.readyState = 3;}
  }
  const chrome = {
    storage: {local: {async get(keys) {return Object.fromEntries(keys.filter(k => k in data).map(k => [k, structuredClone(data[k])]));},
      async set(value) {Object.assign(data, structuredClone(value));}}, onChanged: event('storage')},
    tabs: {async get() {return {id: 7, url: ownedUrl};}, async query() {return ownedUrl ? [{id: 7, url: ownedUrl}] : [];},
      async create(value) {created.push(value); return {id: 8, url: value.url};}, async sendMessage() {return {accepted: true};}},
    downloads: {onChanged: event('download')}, runtime: {onMessage: event('message'), onStartup: event('startup'),
      onInstalled: event('installed'), getManifest() {return {version: '1.1.2'};}, async openOptionsPage() {}},
    alarms: {create() {}, onAlarm: event('alarm')}, action: {onClicked: event('action')}
  };
  const context = {chrome, WebSocket: Socket, crypto, URL, console,
    setInterval() {return 1;}, clearInterval() {}, setTimeout() {return 1;}, Date};
  const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/background.js'), 'utf8')
    .replace("import './background.js';", '') + '\nglobalThis.testApi = {ensureTab};';
  vm.createContext(context); vm.runInContext(source, context); await flush(); sockets[0].onopen();
  return {api: context.testApi, data, created, sent};
}

(async () => {
  const ui = options(); await flush();
  const bundle = {token: 't'.repeat(40), port: 18189, profile: 'Profile 23', project, extension_id: id, setup_id: 'a'.repeat(32)};
  ui.fields.pair.value = JSON.stringify(bundle); ui.fields.pair.events.input();
  assert.equal(ui.fields.profile.value, 'Profile 23'); assert.equal(ui.fields.project.value, project);
  assert.equal(ui.writes.length, 0, 'pasting fills form but requires explicit save');
  await ui.fields.save.onclick();
  assert.deepEqual(ui.data.lzxConfig, {token: bundle.token, port: 18189, profile: 'Profile 23', project, setup_id: bundle.setup_id});
  assert.equal(ui.fields.pair.value, '', 'long-term token should leave the visible text field after save');
  assert.equal(ui.fields.save.disabled, false);
  assert.match(ui.fields.status.textContent, /已连接主程序/);
  const offline = options({}, '', false); await flush();
  assert.match(offline.fields.status.textContent, /无需粘贴任何内容/);
  offline.fields.pair.value = JSON.stringify(bundle); offline.fields.pair.events.input();
  await offline.fields.save.onclick();
  assert.match(offline.fields.status.textContent, /后台尚未连上/,
    'saving config is not proof that the host accepted the background connection');
  const savedReconnect = options({...bundle}, '', false); await flush();
  assert(savedReconnect.commands.some(c => c.action === 'lzxReconnect'), 'bare page must automatically reuse saved config');
  assert.equal(savedReconnect.writes.length, 0, 'reconnect does not require pasting or rewriting credentials');
  const reused = options({}, '', false); await flush();
  reused.context.location.hash = '#setup='+bundle.setup_id+'&port=18189';
  await reused.hooks.hashchange();
  assert.equal(reused.sockets.length, 1, 'an already-open options page must notice a later setup hash');
  reused.sockets[0].onopen();
  await reused.sockets[0].onmessage({data: JSON.stringify({type: 'setup_bundle', pairing: bundle})});
  assert.equal(reused.data.lzxConfig.profile, bundle.profile);
  const update = options({...bundle}, bundle.setup_id, false, '1.1.4'); await flush();
  update.sockets[0].onopen();
  assert.equal(update.sockets[0].sent[0].type, 'setup_reload_check');
  update.sockets[0].onmessage({data: JSON.stringify({type: 'setup_reload_allowed', ticket: bundle.setup_id,
    profile: bundle.profile, project})}); await flush();
  assert.equal(update.reloads(), 1, 'only an idle update approved by the host may reload the extension');
  assert.equal(update.data.lzxReloadSetup.version,
    JSON.parse(fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/manifest.json'), 'utf8')).version);
  assert.equal(update.data.lzxConfig.token, bundle.token, 'updating preserves existing config');
  const busyUpdate = options({...bundle}, bundle.setup_id, false, '1.1.4'); await flush();
  busyUpdate.sockets[0].onopen();
  busyUpdate.sockets[0].onmessage({data: JSON.stringify({type: 'setup_reload_denied', error: 'busy'})}); await flush();
  assert.equal(busyUpdate.reloads(), 0); assert.equal(busyUpdate.writes.length, 0);

  const other = options({token: 'x'.repeat(40), port: 18189, profile: 'Profile 24', project: nextProject}); await flush();
  assert.equal(other.fields.project.value, nextProject, 'separate Chrome storage retains separate project');
  other.fields.project.value = '';
  await other.fields.save.onclick();
  assert.equal(other.writes.length, 0, 'missing project must not borrow or infer another URL');
  assert.match(other.fields.status.textContent, /手动填写/);
  assert.equal(other.fields.save.disabled, false);

  for (const link of ['https://labs.google/', 'https://labs.google.evil.test/project/one',
      'https://user:pass@labs.google/project/one', 'http://labs.google/project/one', 'https://labs.google:8080/project/one']) {
    ui.fields.pair.value = JSON.stringify(bundle); ui.fields.profile.value = bundle.profile; ui.fields.project.value = link;
    const count = ui.writes.length; await ui.fields.save.onclick();
    assert.equal(ui.writes.length, count, `invalid project must not persist: ${link}`);
  }
  ui.fields.pair.value = JSON.stringify({...bundle, extension_id: 'wrong'});
  ui.fields.project.value = project; const count = ui.writes.length;
  await ui.fields.save.onclick(); assert.equal(ui.writes.length, count);

  const automatic = options({}, bundle.setup_id); await flush();
  assert.equal(automatic.sockets.length, 1);
  assert.equal(automatic.sockets[0].url, 'ws://127.0.0.1:18189');
  automatic.sockets[0].onopen();
  assert.deepEqual(automatic.sockets[0].sent[0], {type: 'setup_claim', ticket: bundle.setup_id});
  await automatic.sockets[0].onmessage({data: JSON.stringify({type: 'setup_bundle', pairing: bundle})});
  assert.equal(automatic.data.lzxConfig.token, bundle.token);
  assert.equal(automatic.data.lzxConfig.profile, 'Profile 23');
  assert.equal(automatic.data.lzxConfig.project, project);
  assert.equal(automatic.writes.length, 1, 'automatic connection requires no paste or save click');
  assert.equal(automatic.fields.pair.value, '');
  assert.equal(automatic.replacements.length, 1, 'remove the one-time ticket from history after success');
  assert.match(automatic.fields.status.textContent, /已连接主程序/);
  const expired = options({}, bundle.setup_id); await flush();
  await expired.sockets[0].onmessage({data: JSON.stringify({type: 'setup_error', error: '已过期'})});
  assert.equal(expired.writes.length, 0); assert.match(expired.fields.status.textContent, /已过期/);
  const mismatch = options({}, bundle.setup_id); await flush();
  await mismatch.sockets[0].onmessage({data: JSON.stringify({type: 'setup_bundle', pairing: {...bundle, setup_id: 'b'.repeat(32)}})});
  assert.equal(mismatch.writes.length, 0, 'wrong setup ticket must not modify the profile');

  const empty = await background({token: bundle.token, port: 18189, profile: bundle.profile}, project);
  await assert.rejects(empty.api.ensureTab(), /手动添加/);
  assert.equal(empty.created.length, 0, 'even an old owned tab must not be reused when saved project is absent');
  const correct = await background(bundle, project);
  assert.equal(await correct.api.ensureTab(), 7); assert.equal(correct.created.length, 0);
  assert.equal(correct.sent[0].extension_version, '1.1.2');
  assert.equal(correct.sent[0].setup_id, bundle.setup_id);
  const changed = await background({...bundle, project: nextProject}, project);
  await assert.rejects(changed.api.ensureTab(), /等待主程序/);
  assert.equal(changed.created.length, 0, 'a changed project must be opened by the desktop, never a hidden extension tab');
  console.log('Flow setup: per-profile autofill, explicit project, save validation, no tab guessing and saved-project routing passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
