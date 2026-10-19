// Real bridge parser, transport and handlers. No Chrome, network or generation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {installDownloadHelpers} = require('./flow_test_support.cjs');
const root = process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8').replace(/\r\n/g, '\n');
const proxy = read('browser_extensions/flow_bridge/injects/flow-proxy.js');
const ui = read('browser_extensions/flow_bridge/injects/ui.js');
const page = read('browser_extensions/flow_adapter/page.js');
const rpc = (id, payload) => [['wrb.fr', id, JSON.stringify(payload)]];
const valid = rpc('eb1hJf', [null, null,
  [['group-1', null, null, [null, null, null, null, 'our-media'], 'version-1']],
  [['our-media', 'version-1', 'group-1']]]);
const raw = ")]}'\n" + JSON.stringify(valid);

function fixture() {
  const events = [], sent = [], downloads = [], hooks = {}, documentHooks = {}, intervals = [];
  const process = {rowIndex: 0, lineIndex: 0, submissionId: 'submission-1'};
  const binding = {schema: 2, taskId: 'task-1', submissionId: process.submissionId, mediaIds: [], readyIds: [],
    downloadPath: 'FlowBridge/' + 'a'.repeat(32) + '/result.mp4'};
  const task = {taskId: 'task-1', download_path: binding.downloadPath, mediaBindings: [binding],
    status: ['generating'], downloaded: [], downloadIds: []};
  const window = {currentProcess: process,
    postMessage(msg) {sent.push(msg.data);},
    addEventListener(name, fn) {hooks[name] = fn;},
    dispatchEvent(event) {events.push(event); hooks[event.type]?.(event);}};
  window.top = window;
  const document = {documentElement: {}, readyState: 'complete',
    querySelector() {return null;}, querySelectorAll() {return [];},
    addEventListener(name, fn) {documentHooks[name] = fn;}};
  const context = {URL, Response, console: {log() {}, warn() {}, error() {}}, window, document,
    location: {href: 'https://flow.google.com/project/test', origin: 'https://flow.google.com'},
    state: {isRunning: true, tasks: [task], currentIndex: 0, currentLineIndex: 0},
    isStopRequested: false, alarmTimer: null,
    MutationObserver: class {observe() {}},
    setInterval(fn) {intervals.push(fn);}, clearInterval() {},
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
    async saveState() {}, setupDomVideoWatcher() {}, stopActivityCheck() {}, disableTabKeepAlive() {}, showToast() {},
    updateLineStatus(_row, line, status) {task.status[line] = status;},
    getStorageKey: () => 'project', downloadedUrlsSet: new Set(), isStaticShowcaseAsset: () => false,
    async sendToExtension(action, payload) {downloads.push({action, payload}); return {success: true, downloadId: 42};},
  };
  vm.createContext(context);
  installDownloadHelpers(context, ui);
  vm.runInContext(page, context);
  window.__lzxFlowTransport.attach(async () => {}, () => ({tasks: context.state.tasks, running: context.state.isRunning}), {});
  vm.runInContext(proxy.slice(proxy.indexOf('  function unescapeBoQ('), proxy.indexOf('  function setupFetchHook(')), context);
  vm.runInContext(ui.slice(ui.indexOf('  function stopBatch()'), ui.indexOf('  function onBatchFinished()')), context);
  vm.runInContext(ui.slice(ui.indexOf('  function videoIdentity('), ui.indexOf('  // 定位属于当前任务行的批次容器')), context);
  vm.runInContext(ui.slice(ui.indexOf('  async function triggerAutoDownload('), ui.indexOf('  // 模拟真实用户鼠标悬停')), context);
  const start = ui.indexOf('  function setupEvents()');
  vm.runInContext(ui.slice(start, ui.indexOf('  // ----------------------------------------------------', start)), context);
  context.setupEvents();
  return {context, process, binding, task, hooks, sent, downloads, documentHooks};
}

async function drain() {for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));}

(async () => {
  for (const mime of ['application/json', 'application/octet-stream', 'text/plain']) {
    const {context} = fixture();
    const parsed = await context.parseFetchResponse(new Response(raw, {headers: {'Content-Type': mime}}));
    assert.notEqual(parsed, null, 'anti-XSSI RPC must survive MIME ' + mime);
    assert.equal(context.readRpcFrames(parsed)[0].rpc, 'eb1hJf');
  }
  {
    const f = fixture();
    f.context.receiveGenerationData({acknowledged: true}, f.process, true);
    assert.equal(f.context.state.isRunning, true, 'an unrecognized acknowledgement must not stop the batch');
    assert.equal(f.task.status[0], 'generating');
    assert(f.sent.some(e => e.type === 'binding_missing'));
    f.sent.length = 0;
    await f.hooks.message({source: f.context.window, origin: f.context.location.origin,
      data: {sender: 'lzx-flow-extension', message: {action: 'snapshot'}}});
    assert(f.sent.some(e => e.type === 'generation_response'), 'read-only reconnect probe replays an unbound receipt from RAM');
    f.context.receiveGenerationData(valid, f.process, true);
    assert.deepEqual([...f.binding.mediaIds], ['our-media']);
    f.sent.length = 0;
    await f.hooks.message({source: f.context.window, origin: f.context.location.origin,
      data: {sender: 'lzx-flow-extension', message: {action: 'snapshot'}}});
    assert(!f.sent.some(e => e.type === 'generation_response'), 'a completed ownership binding no longer replays diagnostics');
  }
  {
    const f = fixture();
    f.context.state.isRunning = false; f.context.window.currentProcess = null;
    f.context.receiveGenerationData(valid, f.process, true);
    assert.deepEqual([...f.binding.mediaIds], ['our-media'], 'exact late response survives batch cleanup');
    assert.equal(f.binding.source, 'generation_response');
    await f.hooks['flow-task-media-ready']({detail: {mediaId: 'our-media', url: 'https://flow-content.google/video/our-media?Expires=9999999999&Signature=valid-signature'}});
    assert.equal(f.downloads.length, 1, 'late, bound ready result is downloaded without restarting the batch');
    assert.equal(f.downloads[0].payload.task.mediaId, 'our-media');
    assert.equal(f.context.state.isRunning, false);
  }
  {
    const f = fixture();
    f.context.window.currentProcess = null;
    f.task.mediaBindings[0] = {...f.binding, submissionId: 'new-submission'};
    const before = f.sent.filter(e => e.type === 'generation_response').length;
    f.context.receiveGenerationData(valid, f.process, true);
    assert.equal(f.task.mediaBindings[0].mediaIds.length, 0, 'old response cannot claim a replaced task slot');
    assert.equal(f.sent.filter(e => e.type === 'generation_response').length, before);
  }
  {
    const f = fixture();
    f.context.receiveGenerationData(valid, f.process, true);
    f.context.state.isRunning = false; f.context.window.currentProcess = null;
    vm.runInContext(ui.slice(ui.indexOf('  function setupDomVideoWatcher()'), ui.indexOf('  function setupEvents()')), f.context);
    f.context.setupDomVideoWatcher();
    const video = {tagName: 'VIDEO', readyState: 4, currentTime: 0,
      getAttribute() {return 'https://flow-content.google/video/our-media?Expires=9999999999&Signature=valid-signature';}, closest() {return {querySelector(){return null;}};}};
    f.documentHooks.loadedmetadata({target: video});
    await drain();
    assert.equal(f.downloads.length, 1, 'metadata of an exact bound video is handled after timeout');
    f.documentHooks.loadedmetadata({target: {...video, getAttribute() {return 'https://flow-content.google/video/unrelated?Expires=9999999999&Signature=valid-signature';}}});
    await drain(); assert.equal(f.downloads.length, 1, 'no unrelated DOM media adoption');
  }
  {
    const source = read('browser_extensions/flow_adapter/background.js'), sent = [];
    let listener;
    const context = {chrome: {runtime: {id: 'our-extension', onMessage: {addListener(fn) {listener = fn;}}}},
      ownedTab: 7, activeBatch: 'b'.repeat(32), send: event => sent.push(event)};
    vm.createContext(context);
    const start = source.indexOf('chrome.runtime.onMessage.addListener(');
    vm.runInContext(source.slice(start, source.indexOf('chrome.storage.onChanged.addListener(', start)), context);
    const sender = {id: 'our-extension', tab: {id: 7}, frameId: 0, url: 'https://flow.google.com/project/test'};
    for (const type of ['generation_response', 'binding_missing']) {
      listener({action: 'lzxPageEvent', data: {type, download_path: 'FlowBridge/'+'a'.repeat(32)+'/result.mp4'}}, sender);
      assert.equal(sent.at(-1).batch_id, 'a'.repeat(32), 'late receipt must keep its original batch');
    }
    listener({action: 'lzxPageEvent', data: {type: 'generation_response', download_path: '../result.mp4'}}, sender);
    assert.equal(sent.length, 2, 'invalid original download path is not forwarded');
  }
  console.log('Late Flow response: framed JSON/binary parsing, non-stopping acknowledgement, cleanup recovery, exact ownership and stopped-batch auto-download passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
