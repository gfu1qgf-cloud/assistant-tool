// Execute the actual generated media parser and downloader, offline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {installDownloadHelpers} = require('./flow_test_support.cjs');
const load = name => fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/', name), 'utf8').replace(/\r\n/g, '\n');
const quiet = {log() {}, warn() {}, error() {}};

(async () => {
  const source = load('flow-proxy.js'), events = [], process = {submissionId: 'our-submission', lineText: 'same prompt text'};
  const context = {URL, console: quiet, window: {__lzxFlowTransport: {}, currentProcess: process,
    dispatchEvent(event) {events.push(event);}}, CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}}};
  vm.createContext(context);
  const start = source.indexOf('  function unescapeBoQ('), end = source.indexOf('  function setupFetchHook(');
  assert(start >= 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
  const rpc = (id, payload) => [['wrb.fr', id, JSON.stringify(payload)]];
  const status = rpc('jwpduf', [['unrelated-media', 'unrelated-group', null, null, null, 'same prompt text']]);
  context.receiveGenerationData(status, process, false);
  assert.equal(events.filter(e => e.type === 'flow-task-media-bound').length, 0,
    'single status item or matching prompt cannot establish media ownership');
  const media = 'our-media', group = 'our-group', version = 'our-version';
  const response = rpc('eb1hJf', [null, null, [[group, null, null, [null, null, null, null, media], version]], [[media, version, group]]]);
  context.receiveGenerationData(response, process, false);
  assert.equal(events.filter(e => e.type === 'flow-task-media-bound').length, 0, 'status requests cannot masquerade as a submission');
  context.receiveGenerationData(response, process, true);
  let bound = events.filter(e => e.type === 'flow-task-media-bound');
  assert.equal(bound.length, 1); assert.equal(bound[0].detail.source, 'generation_response');
  assert.equal(bound[0].detail.submissionId, 'our-submission'); assert.deepEqual([...bound[0].detail.mediaIds], [media]);
  const old = {submissionId: 'old-submission'};
  context.receiveGenerationData(response, old, true);
  assert.equal(events.filter(e => e.type === 'flow-task-media-bound').length, 1, 'late old response cannot bind the current task');
  context.receiveGenerationData({media: [{name: 'our-second-media'}]}, process, true);
  bound = events.filter(e => e.type === 'flow-task-media-bound');
  assert.equal(bound.length, 2); assert.equal(bound[1].detail.source, 'generation_response');
  context.receiveGenerationData({media: [{name: 'history-media'}]}, process, false);
  assert.equal(events.filter(e => e.type === 'flow-task-media-bound').length, 2);

  const ui = load('ui.js'), downloads = [], binding = {schema: 2, taskId: 'task', submissionId: 'our-submission',
    mediaIds: [], readyIds: [], urls: {}, downloadPath: 'FlowBridge/'+'a'.repeat(32)+'/result.mp4'};
  const task = {taskId: 'task', mediaBindings: [binding], status: ['generating'], downloaded: [], downloadIds: []};
  const hooks = {}, video = {getAttribute() {return 'https://flow-content.google/video/unrelated';}, closest() {return {};}};
  const downloadContext = {console: quiet, state: {isRunning: true, tasks: [task], currentIndex: 0, currentLineIndex: 0},
    window: {__lzxFlowTransport: {}, currentProcess: {rowIndex: 0, lineIndex: 0, submissionId: 'our-submission'},
      addEventListener(name, fn) {hooks[name] = fn;}},
    document: {querySelectorAll() {return [video];}}, wasVideoSeen: () => false, isStaticShowcaseAsset: () => false,
    mediaIdFromVideoUrl: url => new URL(url).pathname.split('/').at(-1),
    findMediaBinding(id) {return binding.mediaIds.includes(id) ? {task, binding, rowIndex: 0, lineIndex: 0} : null;},
    async saveState() {}, updateLineStatus() {}, getStorageKey: () => 'project', setupDomVideoWatcher() {},
    async sendToExtension(_action, data) {downloads.push(data); return {success: true, downloadId: 42};}};
  vm.createContext(downloadContext);
  installDownloadHelpers(downloadContext, ui);
  const dlStart = ui.indexOf('  async function triggerAutoDownload('), dlEnd = ui.indexOf('  // 模拟真实用户鼠标悬停', dlStart);
  vm.runInContext(ui.slice(dlStart, dlEnd), downloadContext);
  assert.equal(await downloadContext.triggerAutoDownload('https://flow-content.google/video/unrelated', 'unrelated'), false);
  assert.equal(binding.mediaIds.length, 0); assert.equal(downloads.length, 0, 'a visible unrelated video must not be adopted or downloaded');
  const eventsStart = ui.indexOf('  function setupEvents()'), eventsEnd = ui.indexOf('  // ----------------------------------------------------', eventsStart);
  vm.runInContext(ui.slice(eventsStart, eventsEnd), downloadContext); downloadContext.setupEvents();
  hooks['flow-task-media-bound']({detail: {submissionId: 'our-submission', mediaIds: ['unrelated'], source: 'dom'}});
  assert.equal(binding.mediaIds.length, 0, 'legacy DOM events must not get trusted provenance');
  hooks['flow-task-media-bound']({detail: {submissionId: 'other-submission', mediaIds: [media], source: 'generation_response'}});
  assert.equal(binding.mediaIds.length, 0);
  hooks['flow-task-media-bound']({detail: {submissionId: 'our-submission', mediaIds: [media], source: 'generation_response'}});
  assert.equal(binding.source, 'generation_response'); assert.deepEqual([...binding.mediaIds], [media]);
  hooks['flow-task-media-bound']({detail: {submissionId: 'our-submission', mediaIds: ['replacement'], source: 'generation_response'}});
  assert.deepEqual([...binding.mediaIds], [media], 'a confirmed submission cannot be rebound to another media');
  binding.readyIds = [media]; binding.urls[media] = `https://flow-content.google/video/${media}?Expires=9999999999&Signature=valid-signature`;
  assert.equal(await downloadContext.triggerAutoDownload(binding.urls[media], media), true);
  assert.equal(downloads[0].task.mediaId, media); assert.equal(downloads[0].task.submissionId, 'our-submission');
  binding.mediaIds = []; binding.readyIds = []; binding.source = undefined; task.downloadIds = [];
  const domHooks = {};
  Object.assign(downloadContext, {MutationObserver: class {observe() {}}, setInterval() {},
    document: {documentElement: {}, addEventListener(name, fn) {domHooks[name] = fn;}, querySelectorAll() {return [video];}}});
  video.tagName = 'VIDEO'; video.readyState = 4; video.currentTime = 1;
  const watchStart = ui.indexOf('  function setupDomVideoWatcher()'), watchEnd = ui.indexOf('  function setupEvents()', watchStart);
  vm.runInContext(ui.slice(watchStart, watchEnd), downloadContext); downloadContext.setupDomVideoWatcher();
  domHooks.loadedmetadata({target: video});
  assert.equal(binding.mediaIds.length, 0, 'DOM watcher must not infer ownership from a visible tile without prompt');
  assert.equal(downloads.length, 1);
  console.log('Flow identity: no DOM/status adoption; exact generation response, old-response rejection and bound-only download passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
