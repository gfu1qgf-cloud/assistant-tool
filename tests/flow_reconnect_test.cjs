const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const id = 'pnjppjjfgiccpcfajkpooklgohgihdom', hooks = {}, sockets = [], timers = [];
  const data = {lzxConfig: {token: 'x'.repeat(40), port: 18189, profile: 'Profile 23',
    project: 'https://flow.google.com/project/project-1', setup_id: 'a'.repeat(32)}};
  const event = name => ({addListener(fn) {hooks[name] = fn;}});
  let delayedRead = null;
  class Socket {
    static OPEN = 1;
    constructor() {this.readyState = 0; this.sent = []; sockets.push(this);}
    send(text) {this.sent.push(JSON.parse(text));}
    close() {this.readyState = 3;} // old close notification deliberately arrives later
    open() {this.readyState = 1; this.onopen();}
  }
  const chrome = {storage: {local: {
    async get(keys) {if (delayedRead) await delayedRead;
      return Object.fromEntries(keys.filter(k => k in data).map(k => [k, structuredClone(data[k])]));},
    async set(value) {Object.assign(data, structuredClone(value));}}, onChanged: event('storage')},
    runtime: {id, getManifest() {return {version: '1.1.4'};}, onMessage: event('message'),
      onStartup: event('startup'), onInstalled: event('installed')},
    downloads: {onChanged: event('download')}, alarms: {onAlarm: event('alarm')}, action: {onClicked: event('action')}};
  const context = {chrome, WebSocket: Socket, crypto, URL, Date, console,
    setTimeout(fn, ms) {timers.push({fn, ms}); return timers.length;},
    setInterval() {return 1;}, clearInterval() {}};
  const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/background.js'), 'utf8')
    .replace("import './background.js';", '') + '\nglobalThis.api = {connect, receive};';
  vm.createContext(context); vm.runInContext(source, context); await flush();
  const status = () => {let value; hooks.message({action: 'lzxConnectionStatus'},
    {id, url: `chrome-extension://${id}/options.html`}, v => value = v); return value;};
  assert.equal(sockets.length, 1);
  await context.api.connect(); assert.equal(sockets.length, 1, 'CONNECTING socket cannot be duplicated');
  const old = sockets[0]; old.open();
  assert.equal(status().connected, false, 'an open socket is not authenticated before welcome');
  await context.api.receive({type: 'welcome'}); assert.equal(status().connected, true);
  assert.equal(status().setupId, 'a'.repeat(32));
  assert.equal(JSON.stringify(status()).includes(data.lzxConfig.token), false, 'status never returns pairing secrets');
  data.lzxConfig.setup_id = 'b'.repeat(32);
  hooks.storage({lzxConfig: {newValue: data.lzxConfig}}, 'local');
  assert.equal(status().connected, false);
  await context.api.connect(); const current = sockets[1]; current.open();
  await context.api.receive({type: 'welcome'}); assert.equal(status().connected, true);
  old.onclose();
  assert.equal(status().connected, true, 'late old close must not erase the new socket or heartbeat');
  old.onmessage({data: JSON.stringify({type: 'welcome'})});
  assert.equal(status().setupId, 'b'.repeat(32));
  current.close(); current.onclose();
  data.lzxConfig.setup_id = 'c'.repeat(32); hooks.storage({lzxConfig: {oldValue: {setup_id: 'b'}, newValue: data.lzxConfig}}, 'local');
  let release;
  delayedRead = new Promise(resolve => {release = resolve;});
  const connecting = context.api.connect();
  data.lzxConfig.setup_id = 'd'.repeat(32); hooks.storage({lzxConfig: {oldValue: {setup_id: 'c'}, newValue: data.lzxConfig}}, 'local');
  delayedRead = null; release(); await connecting;
  assert.equal(sockets.length, 2, 'obsolete async config read must not create a socket');
  await context.api.connect(); assert.equal(sockets.length, 3); sockets[2].open();
  assert.equal(sockets[2].sent[0].setup_id, 'd'.repeat(32));
  await context.api.receive({type: 'welcome'}); assert.equal(status().connected, true);
  let leaked = false; hooks.message({action: 'lzxConnectionStatus'}, {id, url: 'https://flow.google.com/'}, () => {leaked = true;});
  assert.equal(leaked, false, 'website content cannot query privileged extension status');
  console.log('Flow connection: authenticated status, fresh config, CONNECTING deduplication and stale-socket races passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
