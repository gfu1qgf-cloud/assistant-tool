// Execute the built upstream download functions and the real page bridge offline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {installDownloadHelpers} = require('./flow_test_support.cjs');

(async () => {
  const upstream = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8').replace(/\r\n/g, '\n');
  const attempt = 'a'.repeat(32), mediaId = 'bound-media';
  const binding = {schema: 2, source: 'generation_response', taskId: 'bound-task', mediaIds: [mediaId], readyIds: [mediaId],
    urls: {[mediaId]: `https://flow-content.google/video/${mediaId}?Expires=9999999999&Signature=valid-signature`},
    submissionId: 'bound-submission', downloadPath: `FlowBridge/${attempt}/result.mp4`};
  const task = {taskId: 'bound-task', download_path: binding.downloadPath,
    mediaBindings: [binding], downloadIds: [], downloaded: [false], status: ['failed']};
  const unrelated = {download_path: `FlowBridge/${'b'.repeat(32)}/result.mp4`, mediaBindings: []};
  const state = {isRunning: false, tasks: [task, unrelated], currentIndex: 0, currentLineIndex: 0};
  const downloads = [];
  let currentDownload = null, attached;
  const context = {state, batchPromise: null, isStopRequested: false,
    uploadPathsToFlow() {throw Error('download-only recovery cannot upload');}, getLoadedPageImages:()=>[], simulateClick() {throw Error('download-only recovery cannot click');},
    cleanupPageOverlays() {throw Error('download-only recovery cannot change page overlays');},
    console: {log() {}, warn() {}, error() {}},
    document: {querySelectorAll: () => []},
    mediaIdFromVideoUrl: url => typeof url === 'string' ? new URL(url).pathname.split('/').at(-1) : '',
    mediaIdFromImageThumbnailUrl: () => null, isVideoThumbnailImg: () => false, triggerTileHover() {},
    findMediaBinding(id) {
      return state.tasks.flatMap((task, rowIndex) => (task.mediaBindings || []).map((binding, lineIndex) =>
        binding?.mediaIds?.includes(id) ? {task, binding, rowIndex, lineIndex} : null)).find(Boolean);
    },
    updateLineStatus(row, line, status) {state.tasks[row].status[line] = status;},
    async saveState() {}, getStorageKey: () => 'bound-project',
    async sendToExtension(action, data) {
      if (action === 'getDownloadStatus') return currentDownload;
      assert.equal(action, 'download', 'retry must not submit or generate anything');
      downloads.push(data);
      return {success: true, downloadId: 42};
    },
    handleMessage() {throw Error('retry must not enter generation handler');},
    submitPackedTask(){throw Error('download/account fixture must never submit a paid task');}, syncPageVideoSettings() {throw Error('retry must not change generation settings');},
    location: {href: 'https://labs.google/fx/tools/flow/project/test'},
    window: {__lzxFlowTransport: {attach(_handler, _snapshot, helpers) {attached = helpers;}}}
  };
  vm.createContext(context);
  installDownloadHelpers(context, upstream);
  const start = upstream.indexOf('  async function retrySavedVideo(');
  const end = upstream.indexOf('  // 模拟真实用户鼠标悬停', start);
  assert(start >= 0 && end > start, 'upstream retry seam must exist');
  vm.runInContext(upstream.slice(start, end), context);
  const attachStart = upstream.indexOf('    if (window.__lzxFlowTransport) {\n      if (!batchPromise)');
  const attachEnd = upstream.indexOf('    function connect() {', attachStart);
  assert(attachStart >= 0 && attachEnd > attachStart, 'upstream transport seam must exist');
  vm.runInContext(`(function () {${upstream.slice(attachStart, attachEnd)}})()`, context);

  assert.equal(await attached.retry(attempt), true, 'finished batch must still download its bound result');
  assert.equal(state.isRunning, false, 'download-only retry must not restart the generation batch');
  assert.equal(downloads.length, 1);
  assert.equal(downloads[0].filename, binding.downloadPath);
  assert.equal(downloads[0].task.mediaId, mediaId);
  assert.equal(downloads[0].task.submissionId, 'bound-submission');
  assert.equal(downloads[0].task.taskId, 'bound-task');
  assert.equal(await attached.retry('c'.repeat(32)), false, 'another batch must not download the current result');
  assert.equal(await attached.retry('../invalid'), false);
  assert.equal(downloads.length, 1);
  assert.equal(await context.triggerAutoDownload(binding.urls[mediaId], mediaId), false,
    'ordinary automatic downloads must remain disabled after the batch stops');

  state.tasks.reverse(); state.currentIndex = 1;
  currentDownload = {state: 'in_progress'};
  assert.equal(await attached.retry(attempt), true);
  assert.equal(downloads.length, 1, 'an existing download must not be duplicated');
  currentDownload = {state: 'complete'};
  assert.equal(await attached.retry(attempt), true);
  assert.equal(task.downloaded[0], true);
  assert.equal(downloads.length, 1);

  task.downloaded[0] = false; task.downloadIds[0] = null;
  binding.readyIds = []; binding.urls = {};
  assert.equal(await attached.retry(attempt), false, 'not-ready media must not claim a download was started');
  assert.equal(state.isRunning, false);
  assert.equal(downloads.length, 1);

  const messages = [], hooks = {}, pageWindow = {
    postMessage(value) {messages.push(value.data);},
    addEventListener(name, callback) {hooks[name] = callback;}
  };
  pageWindow.top = pageWindow;
  const pageContext = {window: pageWindow,
    location: {origin: 'https://labs.google', href: context.location.href},
    document: {documentElement: {}, querySelector: () => null},
    MutationObserver: class {observe() {}}, setInterval() {}};
  vm.createContext(pageContext);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8'), pageContext);
  let receivedBatch;
  pageWindow.__lzxFlowTransport.attach(() => {throw Error('no generation allowed');},
    () => ({running: false, tasks: []}), {async retry(batch) {receivedBatch = batch; return true;}});
  await hooks.message({source: pageWindow, origin: 'https://labs.google', data: {
    sender: 'lzx-flow-extension', message: {action: 'retry_download', request_id: 'retry', params: {batch_id: attempt}}
  }});
  assert.equal(receivedBatch, attempt, 'page bridge must forward the bound batch ID to the retry helper');
  assert.equal(messages.find(message => message.request_id === 'retry').success, true);
  console.log('Flow stopped-batch download retry: binding, no regeneration, deduplication and page routing passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
