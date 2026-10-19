const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

(async () => {
  const data = {lzxConfig: {token: 'x'.repeat(40), port: 18189, profile: 'Profile 23',
    project: 'https://labs.google/fx/tools/flow/project/project-1'}};
  // The desktop, not the extension, has opened this URL in Chrome.
  const hooks = {}, tabs = new Map([[7, {id: 7, url: data.lzxConfig.project, active: true}]]), forwarded = [], sent = [], sockets = [];
  let created = 0;
  const event = name => ({addListener(callback) { hooks[name] = callback; }});
  class FakeSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); }
    send(text) { sent.push(JSON.parse(text)); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  const chrome = {
    storage: {local: {async get(keys) { return Object.fromEntries(keys.filter(k => k in data).map(k => [k, structuredClone(data[k])])); },
      async set(values) { Object.assign(data, structuredClone(values)); }}, onChanged: event('storage')},
    tabs: {async get(id) { if (!tabs.has(id)) throw Error('gone'); return tabs.get(id); }, async query() { return [...tabs.values()]; },
      async create(value) { const tab = {id: ++created, url: value.url}; tabs.set(tab.id, tab); return tab; },
      async sendMessage(id, value) { forwarded.push({id, value}); return {accepted: true}; }},
    downloads: {async search({id}) { return [{id, filename: `C:/Downloads/FlowBridge/${'a'.repeat(32)}/result (1).mp4`, state: 'complete'}]; }, onChanged: event('download')},
    runtime: {onMessage: event('message'), onStartup: event('startup'), onInstalled: event('installed'), async openOptionsPage() {}},
    alarms: {create() {}, onAlarm: event('alarm')}, action: {onClicked: event('action')}
  };
  const context = {chrome, WebSocket: FakeSocket, crypto, URL, console, structuredClone,
    setInterval() { return 1; }, clearInterval() {}, setTimeout() { return 1; }, Date};
  const filename = path.join(__dirname, '../browser_extensions/flow_adapter/background.js');
  const source = fs.readFileSync(filename, 'utf8').replace("import './background.js';", '') +
    '\nglobalThis.testApi = {receive, ensureTab, reportDownload};';
  vm.createContext(context); vm.runInContext(source, context, {filename});
  await new Promise(resolve => setImmediate(resolve));
  sockets[0].onopen();
  assert.equal(sent[0].type, 'hello');
  assert.equal(sent[0].profile, 'Profile 23');
  const api = context.testApi;
  await Promise.all([api.ensureTab(), api.ensureTab()]);
  assert.equal(created, 0, 'concurrent startup requests must adopt the desktop-opened tab without creating one');
  const request = {action: 'run_tasks', request_id: 'one', params: {batch_id: 'a'.repeat(32), tasks: [{}]}};
  await Promise.all([api.receive(request), api.receive({...request, request_id: 'two'})]);
  assert.equal(forwarded.filter(item => item.value.message.action === 'run_tasks').length, 1,
    'replayed batch must never generate twice');
  await api.receive({action: 'retry_download', request_id: 'old', params: {batch_id: 'b'.repeat(32)}});
  assert.equal(sent.find(message => message.request_id === 'old').success, false);
  const task = {download_path: `FlowBridge/${'a'.repeat(32)}/result.mp4`, taskId: 'upstream-task', submissionId: 'submission', mediaId: 'media'};
  await context.__lzxDownloadBound(4, task);
  assert.equal(Object.keys(data.lzxOutbox).length, 1);
  const complete = Object.values(data.lzxOutbox)[0];
  assert.equal(complete.data.mediaId, 'media');
  assert.equal(complete.batch_id, 'a'.repeat(32));
  assert.equal(complete.data.filename, `C:/Downloads/FlowBridge/${'a'.repeat(32)}/result (1).mp4`);
  assert.equal(complete.data.download_path, task.download_path, 'renaming must not lose the requested media binding');
  await api.receive({type: 'ack', event_id: complete.event_id});
  assert.equal(Object.keys(data.lzxOutbox).length, 0);
  const saveLocal = chrome.storage.local.set;
  chrome.storage.local.set = async values => {
    if ('lzxBatches' in values) throw Error('synthetic batch journal failure');
    return saveLocal(values);
  };
  const rejected = {...request, request_id: 'failed-claim', params: {...request.params, batch_id: 'c'.repeat(32)}};
  await api.receive(rejected);
  assert.equal(sent.find(message => message.request_id === 'failed-claim').success, false);
  assert.equal(forwarded.filter(item => item.value.message.action === 'run_tasks').length, 1,
    'a failed batch claim must not forward a paid command');
  assert(!data.lzxBatches['c'.repeat(32)]);
  chrome.storage.local.set = async values => {
    if ('lzxDownloads' in values) throw Error('synthetic download journal failure');
    return saveLocal(values);
  };
  await assert.rejects(context.__lzxDownloadBound(5, task), /synthetic download journal failure/,
    'a failed durable download binding must reach the downloader instead of appearing successful');
  chrome.storage.local.set = saveLocal;
  await context.__lzxDownloadBound(6, task);
  assert(data.lzxDownloads[6], 'a failed write must not poison the next serialized operation');
  const before = sent.length;
  hooks.message({action: 'lzxPageEvent', data: {type: 'snapshot', data: {}}}, {tab: {id: 999}, frameId: 0});
  assert.equal(sent.length, before, 'unowned page cannot publish a task result');
  const upstream = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8');
  const pathFunction = upstream.slice(upstream.indexOf('  function resolveDownloadPath('), upstream.indexOf('  function isStaticShowcaseAsset('));
  const pathContext = {window: {__lzxFlowTransport: {}}, sanitizePathName: value => value, getProjectName: () => 'project'};
  vm.createContext(pathContext); vm.runInContext(pathFunction, pathContext);
  assert.equal(pathContext.resolveDownloadPath(task.download_path), task.download_path, 'upstream must preserve host download binding');
  let panelEmail = 'current@example.test', settingsText = 'Omni 1.1 Flash 720p 9:16 x1', costText = '7 积分';
  const pageMessages = [], pageHooks = {};
  const pageWindow = {postMessage(value) { pageMessages.push(value.data); }, addEventListener(name, callback) { pageHooks[name] = callback; }};
  pageWindow.top = pageWindow;
  const pageContext = {window: pageWindow, location: {origin: 'https://labs.google', href: configProject()},
    document: {documentElement: {}, querySelector(selector) {
      if (selector === 'flow-account-panel') return {textContent: panelEmail,
        querySelectorAll(){return panelEmail.split(/\s+/).map(textContent=>({textContent,children:[]}));}};
      if (selector === 'button.settings-trigger-button') return {textContent: settingsText};
      return null;
    }, querySelectorAll() { return [{textContent: costText, getAttribute() {return null;}}]; }},
    MutationObserver: class {observe() {}}, setInterval() {}};
  function configProject() {return 'https://labs.google/fx/tools/flow/project/project-1';}
  vm.createContext(pageContext); vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8'), pageContext);
  const transport = pageWindow.__lzxFlowTransport;
  const parameters = {aspectRatio: 'ASPECT_RATIO_9_16', resolution: 'VIDEO_RESOLUTION_720P'};
  transport.verifySettings(parameters);
  settingsText = 'Veo 3.1 Fast 720p 9:16 x1';
  assert.throws(() => transport.verifySettings(parameters), /Omni/);
  settingsText = 'Omni 1.1 Flash 720p 9:16 x1';
  transport.attach(async () => {}, () => ({tasks: []}), {async prepare() {}});
  await pageHooks.message({source: pageWindow, origin: 'https://labs.google', data: {sender: 'lzx-flow-extension',
    message: {action: 'prepare', request_id: 'prepare', params: parameters}}});
  assert.equal(pageMessages.find(message => message.request_id === 'prepare').data.cost, 7);
  assert.equal(pageMessages.find(message => message.request_id === 'prepare').data.email, 'current@example.test');
  costText = '1,000 credits';
  await pageHooks.message({source: pageWindow, origin: 'https://labs.google', data: {sender: 'lzx-flow-extension',
    message: {action: 'prepare', request_id: 'ambiguous-cost', params: parameters}}});
  assert.equal(pageMessages.find(message => message.request_id === 'ambiguous-cost').data.cost, null,
    'ambiguous formatted cost must never be misread as free');
  costText = '7 积分';
  transport.send({action: 'get_points_response', data: {email: 'wrong@example.test'}});
  assert.equal(pageMessages.at(-1).data.email, 'current@example.test');
  panelEmail = 'one@example.test two@example.test';
  transport.send({action: 'get_points_response', data: {}});
  assert.equal(pageMessages.at(-1).data.email, '', 'ambiguous account must remain unknown');
  let generations = 0;
  transport.attach(async message => {if (message.action === 'run_tasks') generations++;}, () => ({tasks: []}), {});
  panelEmail = 'current@example.test';
  await pageHooks.message({source: pageWindow, origin: 'https://labs.google', data: {sender: 'lzx-flow-extension',
    message: {action: 'run_tasks', request_id: 'wrong-account', params: {expected_account: 'different@example.test'}}}});
  assert.equal(generations, 0, 'account change must be rejected before paid page handler');
  assert.equal(pageMessages.find(message => message.request_id === 'wrong-account').success, false);
  console.log('Flow bridge protocol: startup, owned tab, replay, retry and durable download checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
