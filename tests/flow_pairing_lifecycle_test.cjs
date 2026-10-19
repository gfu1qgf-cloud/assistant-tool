const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const flush = () => new Promise(resolve => setImmediate(resolve));
const file = name => fs.readFileSync(path.join(__dirname, '../browser_extensions/', name), 'utf8');
const quiet = {log() {}, warn() {}, error() {}};

(async () => {
  const id = 'pnjppjjfgiccpcfajkpooklgohgihdom', ticket = 'a'.repeat(32), project = 'https://flow.google.com/project/our-project';
  const version = JSON.parse(file('flow_bridge/manifest.json')).version;
  const data = {}, hooks = {}, timers = [], sockets = [], writes = [], created = [], reloads = [], focused = [];
  const tabs = new Map([[7, {id: 7, windowId: 42, active: true, url: `${project}#lzx_setup=${ticket}&lzx_port=18189`}]]);
  const event = name => ({addListener(fn) {hooks[name] = fn;}});
  class Socket {
    static OPEN = 1;
    constructor() {this.readyState = 0; this.sent = []; sockets.push(this);}
    send(text) {this.sent.push(JSON.parse(text));} close() {this.readyState = 3; this.onclose?.();}
    open() {this.readyState = 1; this.onopen?.();}
  }
  const chrome = {runtime: {id, getManifest: () => ({version}), onMessage: event('message'),
    onStartup: event('startup'), onInstalled: event('installed')},
    storage: {onChanged: event('storage'), local: {
      async get(keys) {keys = Array.isArray(keys) ? keys : [keys]; return Object.fromEntries(keys.filter(k => k in data).map(k => [k, structuredClone(data[k])]));},
      async set(value) {writes.push(structuredClone(value)); const changes = Object.fromEntries(Object.entries(value).map(([k,v]) => [k,
        {oldValue: structuredClone(data[k]), newValue: structuredClone(v)}])); Object.assign(data, structuredClone(value)); hooks.storage?.(changes, 'local');},
      async remove(key) {delete data[key];}}},
    tabs: {async get(id) {if (!tabs.has(id)) throw Error('gone'); return tabs.get(id);}, async query() {return [...tabs.values()];},
      async create(value) {created.push(value); return {id: 8, windowId: 42, ...value};}, async update(id, value) {Object.assign(tabs.get(id), value); return tabs.get(id);},
      async reload(id) {reloads.push(id);}},
    windows: {async update(id, value) {focused.push({id, ...value});}},
    downloads: {onChanged: event('download'), async search() {return [];}}, alarms: {onAlarm: event('alarm')}, action: {onClicked: event('action')}};
  const context = {chrome, WebSocket: Socket, crypto, URL, URLSearchParams, Date, console: quiet,
    setTimeout(fn, ms) {timers.push({fn, ms}); return timers.length;}, clearTimeout() {}, setInterval() {return 1;}, clearInterval() {}};
  vm.createContext(context);
  vm.runInContext(file('flow_adapter/background.js').replace("import './background.js';", '') +
    '\nglobalThis.api = {ensureTab, receive, connect, resumeReloadSetup};', context); await flush();
  assert.equal(sockets.length, 0, 'unpaired startup does not guess credentials or open tabs');
  let result;
  assert.equal(hooks.message({action: 'lzxSetupFromProject'}, {id, frameId: 0, tab: tabs.get(7), url: tabs.get(7).url}, value => {result = value;}), true);
  await flush(); const claim = sockets[0]; claim.open();
  assert.deepEqual(claim.sent[0], {type: 'setup_claim', ticket});
  await claim.onmessage({data: JSON.stringify({type: 'setup_bundle', pairing: {token: 'x'.repeat(40), port: 18189,
    profile: 'Profile 23', project, extension_id: id, setup_id: ticket}})}); await flush();
  assert.equal(result.success, true); assert.equal(JSON.stringify(result).includes('x'.repeat(40)), false);
  assert.equal(data.lzxConfig.profile, 'Profile 23'); assert.equal(data.lzxOwnedTab, 7);
  for (const timer of timers.filter(t => t.ms <= 50)) await timer.fn(); await flush();
  const host = sockets.at(-1); host.open(); await context.api.receive({type: 'welcome'});
  const socketCount = sockets.length;
  hooks.storage({lzxConfig: {oldValue: structuredClone(data.lzxConfig), newValue: structuredClone(data.lzxConfig)}}, 'local');
  await context.api.connect(); assert.equal(sockets.length, socketCount, 'equal config writes cannot cause reconnect loops');
  assert.equal(await context.api.ensureTab(), 7); assert.equal(created.length, 0, 'reuse the existing same-project tab');
  assert.equal(writes.filter(w => w.lzxConfig).length, 1, 'opening a project does not rewrite connection config');
  data.lzxReloadSetup = {ticket, port: 18189, project, tabId: 7, version};
  await context.api.resumeReloadSetup();
  assert.equal(data.lzxReloadSetup, undefined); assert.deepEqual(reloads, [7],
    'extension update must actually reload the original tab even if its URL did not change');
  assert.equal(created.length, 0);
  await context.api.receive({action: 'open_project', params: {}, request_id: 'open'});
  assert.equal(created.length, 0, 'repeated open focuses the project instead of spawning tabs');
  assert.deepEqual(focused, [{id: 42, focused: true}], 'activate the owning Chrome window, not only its tab');
  const stored = structuredClone(data.lzxConfig);
  let wrongResult;
  hooks.message({action: 'lzxSetupFromProject'}, {id, frameId: 0, tab: {id: 9}, url: `https://evil.test/#lzx_setup=${ticket}&lzx_port=18189`}, value => {wrongResult = value;});
  await flush(); assert.equal(wrongResult.success, false); assert.deepEqual(data.lzxConfig, stored);

  // Execute the entire built proxy: bridge mode must not alter Object.prototype,
  // install a Webpack loader, or eagerly execute site module factories.
  const window = {__lzxFlowTransport: {}, fetch: async () => {throw Error('no network');}}; window.top = window;
  const proxyContext = {window, location: {hostname: 'flow.google.com'}, console: quiet, URL,
    XMLHttpRequest: class {open() {} send() {}}, setTimeout() {}, clearTimeout() {}, Request: class {}};
  vm.createContext(proxyContext); vm.runInContext('globalThis.originalHasOwn = Object.prototype.hasOwnProperty;', proxyContext);
  vm.runInContext(file('flow_bridge/injects/flow-proxy.js'), proxyContext);
  assert.equal(vm.runInContext('Object.prototype.hasOwnProperty === originalHasOwn', proxyContext), true);
  assert.equal(window.moduleManager, undefined); assert.equal(window.require, undefined);
  assert.equal(typeof window.fetch, 'function', 'network response capture remains enabled');
  console.log('Flow lifecycle: project-ticket auto pairing, no credential leak, no config/open-tab loop, and no global/module hooks passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
