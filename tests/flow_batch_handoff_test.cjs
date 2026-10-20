// Real built batch loop and completion wrapper, with offline media/downloads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8').replace(/\r\n/g, '\n');
const ui = read('browser_extensions/flow_bridge/injects/ui.js');
const quiet = {log() {}, warn() {}, error() {}};

async function terminalBatchTest() {
  let clock = 0, delayCount = 0, idle = 0, finish = 0;
  const task = {taskId: 'old-task', text: 'one prompt', image: {displayName: 'image'},
    status: ['generating'], downloaded: [false], downloadIds: [7],
    mediaBindings: [{schema: 2, source: 'generation_response', submissionId: 'old-submission',
      mediaIds: ['old-media'], readyIds: ['old-media'], urls: {}}]};
  const context = {console: quiet, Date: {now: () => clock}, Math,
    state: {tasks: [task], settings: {interval: 30, intervalRandom: 0}, presets: [], currentIndex: 0, currentLineIndex: 0},
    window: {__lzxFlowTransport: {batchIdle() {idle++;}}},
    document: {querySelector: () => ({}), querySelectorAll: () => [],
      getElementById: () => ({style: {setProperty() {}}})},
    alarmDialogEl: null, alarmTimer: null, isVidsPage: () => false,
    enableTabKeepAlive() {}, startActivityCheck() {}, findFlowGenerateButton: () => ({}),
    splitTextToLines: value => value.split('\n'), getTasksNeedingUpload: () => [],
    showToast() {}, async saveState() {}, snapshotCurrentDomMedia() {}, normalizeDuration: () => 8,
    parsePreset: () => ({before: '', after: ''}),
    updateLineStatus(_row, _line, status) {task.status[0] = status;},
    async retrySavedVideo() {return true;},
    async sendToExtension(action) {assert.equal(action, 'getDownloadStatus'); return {state: 'complete'};},
    async delay(ms) {delayCount++; clock += ms;}, async cleanupPageOverlays() {},
    onBatchFinished() {finish++;}, alert() {throw Error('unexpected alert');}};
  vm.createContext(context);
  const loop = ui.slice(ui.indexOf('  async function runBatch('), ui.indexOf('  function stopBatch()'));
  const wrapper = ui.slice(ui.indexOf('  let isStopRequested = false;'), ui.indexOf('  // ====================================================\n  // 7.5'));
  assert(loop && wrapper, 'exact upstream batch seams must exist');
  vm.runInContext(wrapper + loop, context);
  await context.startBatch();
  assert.equal(delayCount, 0, 'a one-job desktop batch must not run the upstream 30-second next-item countdown');
  assert.equal(idle, 1, 'page idle must be announced after batchPromise is released');
  assert.equal(vm.runInContext('batchPromise', context), null);
  assert(finish > 0);
  const accepted = [];
  context.location = {href: 'https://labs.google/project/test'};
  context.clientId = 'synthetic'; context.hostedStatusHook = null;
  context.renderTable = () => {};
  context.importTaskList = tasks => {
    Object.assign(task, {download_path: tasks[0].download_path, status: ['generating'],
      downloaded: [false], downloadIds: [7]});
    context.state.tasks.push(task);
    return 1;
  };
  context.send = event => {
    if (event.action === 'run_tasks_response') {
      assert.equal(event.success, true);
      assert(vm.runInContext('!!batchPromise', context), 'acceptance must announce an already-owned running batch');
      accepted.push(event.data.batch_id);
    }
  };
  const start = ui.indexOf('      if (action === "run_tasks") {');
  const end = ui.indexOf('      if (action === "stop_tasks") {', start);
  vm.runInContext(`async function handleBatch(msg) {const action = msg.action;${ui.slice(start, end)}}`, context);
  for (const batch of ['b'.repeat(32), 'c'.repeat(32)]) {
    await context.handleBatch({action: 'run_tasks', request_id: batch, params: {batch_id: batch,
      tasks: [{download_path: `FlowBridge/${batch}/result.mp4`}]}});
  }
  assert.equal(accepted.length, 2, 'two consecutive hosted jobs must both be accepted after prior teardown');
  assert.equal(delayCount, 0);
}

async function cleanupFailureTest() {
  let idle = 0;
  const context = {console: quiet, window: {__lzxFlowTransport: {batchIdle() {idle++;}}},
    document: {querySelectorAll: () => [], getElementById: () => ({style: {setProperty() {}}})},
    async runBatch() {}, showToast() {}, onBatchFinished() {throw Error('synthetic teardown failure');}};
  vm.createContext(context);
  vm.runInContext(ui.slice(ui.indexOf('  let isStopRequested = false;'),
    ui.indexOf('  // ====================================================\n  // 7.5')), context);
  await assert.rejects(context.startBatch(), /synthetic teardown failure/);
  assert.equal(vm.runInContext('batchPromise', context), null, 'cleanup failure must never leave the next batch permanently busy');
  assert.equal(idle, 1);
}

async function pagePreflightTest() {
  const events = [], hooks = {};
  let prepared = 0;
  const window = {postMessage(value) {events.push(value.data);}, addEventListener(name, fn) {hooks[name] = fn;}};
  window.top = window;
  const context = {window, location: {origin: 'https://labs.google', href: 'https://labs.google/project/test'},
    document: {documentElement: {}, querySelector: () => null}, MutationObserver: class {observe() {}}, setInterval() {}};
  vm.createContext(context);
  vm.runInContext(read('browser_extensions/flow_adapter/page.js'), context);
  window.__lzxFlowTransport.attach(() => {throw Error('no paid handler allowed');},
    () => ({running: true, tasks: []}), {async prepare() {prepared++;}});
  await hooks.message({source: window, origin: 'https://labs.google', data: {sender: 'lzx-flow-extension',
    message: {action: 'prepare', request_id: 'next', params: {}}}});
  const result = events.find(event => event.request_id === 'next');
  assert.equal(result?.code, 'FLOW_BATCH_BUSY', 'busy must be reported before settings/upload/paid submission');
  assert.equal(prepared, 0);
}

(async () => {
  await terminalBatchTest(); await cleanupFailureTest(); await pagePreflightTest();
  console.log('Flow consecutive batch handoff: no terminal countdown, reliable idle release and pre-submit busy wait passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
