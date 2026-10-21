// Exercise the real adapter router + upstream bound downloader; no network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  const old = 'a'.repeat(32), active = 'b'.repeat(32), events = [], downloads = [], journal = [];
  const saved = {lzxBatches: {[old]: 1, [active]: 2}, lzxDownloads: {}};
  const items = new Map(), session = {};
  let forwards = 0;
  const context = {URL, ownedTab: 99, activeBatch: active, closingBrowser:false, commandsInFlight:0,
    recentAutoDownloads:new Map(), console: {log() {}, warn() {}, error() {}},
    local: async () => saved, send: value => events.push(value), applyBrowserOptions: async () => {},
    forward: async () => {forwards++; throw Error('recovery must not enter a generation page handler');},
    serialize: async fn => fn(), reportDownload: async id => journal.push({reported: id}),
    async notifyDownload() {},
    async __lzxDownloadBound(id, task) {journal.push({id, task});},
    chrome: {runtime: {}, storage: {session: {async set(value) {Object.assign(session, value);}}},
      downloads: {async search({id}) {return items.has(id) ? [items.get(id)] : [];},
        download(options, callback) {downloads.push(options); callback(50);}}}}
  vm.createContext(context);
  const background = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/background.js'), 'utf8');
  const start = background.indexOf('async function handleDownload(');
  const end = background.indexOf('// 只处理本扩展发起', start);
  assert(start >= 0 && end > start);
  vm.runInContext(background.slice(start, end), context);
  context.__lzxRetryBoundDownload = context.handleDownload;
  const adapter = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/background.js'), 'utf8');
  vm.runInContext(adapter.slice(adapter.indexOf('async function receive('), adapter.indexOf('async function connect(')), context);
  const message = {action: 'retry_download', request_id: 'recover-old', params: {batch_id: old, restore: {
    task_id: 'original-task', download_path: `FlowBridge/${old}/result.mp4`, bindings: [{schema: 2,
      source: 'generation_response', taskId: 'original-task', submissionId: 'original-submission', mediaIds: ['original-media'], readyIds: ['original-media'],
      urls: {'original-media': 'https://flow-content.google/video/original-media?Expires=9999999999&Signature=saved-signature'}}]}}};
  await context.receive(message);
  assert.equal(events.at(-1).success, true);
  assert.equal(context.activeBatch, active, 'recovering an old download must not replace the active paid batch');
  assert.equal(forwards, 0); assert.equal(downloads.length, 1);
  assert.equal(downloads[0].filename, `FlowBridge/${old}/result.mp4`);
  assert.equal(journal[0].task.taskId, 'original-task');
  assert.equal(journal[0].task.mediaId, 'original-media');
  saved.lzxDownloads[50] = {batch: old, taskId: 'original-task', submissionId: 'original-submission', mediaId: 'original-media'};
  items.set(50, {state: 'in_progress', exists: true});
  await context.receive({...message, request_id: 'repeat'});
  assert.equal(downloads.length, 1, 'repeated retry must not duplicate an in-progress download');
  assert.equal(events.at(-1).data?.downloadId, 50, 'reused downloads must acknowledge their exact Chrome ID');
  saved.lzxDownloads = {}; items.clear();
  const mismatch = structuredClone(message);
  mismatch.params.restore.bindings[0].urls['original-media'] = 'https://flow-content.google/video/different-media';
  await context.receive(mismatch);
  assert.equal(events.at(-1).success, false); assert.equal(downloads.length, 1);
  const unsafe = structuredClone(message); unsafe.params.restore.download_path = '../other.mp4';
  await context.receive(unsafe); assert.equal(events.at(-1).success, false);
  const unknown = structuredClone(message); unknown.params.batch_id = 'c'.repeat(32);
  unknown.params.restore.download_path = `FlowBridge/${unknown.params.batch_id}/result.mp4`;
  await context.receive(unknown); assert.equal(events.at(-1).success, false);
  assert.equal(context.activeBatch, active); assert.equal(forwards, 0);
  const asb = structuredClone(message);
  asb.params.restore.bindings[0].urls['original-media'] = 'https://flow.google.com/asb/original-media=s0,v0,x1';
  await context.receive(asb);
  assert.equal(events.at(-1).success, true, 'real Flow ASB downloads must pass the bound downloader');
  assert.equal(downloads.length, 2);
  for (const url of ['http://flow.google.com/asb/original-media',
    'https://flow.google.com.evil.test/asb/original-media', 'https://evil.test/asb/original-media',
    'https://flow.google.com:8443/asb/original-media', 'https://user@flow.google.com/asb/original-media',
    'https://flow.google.com/asb/different-media=s0', 'https://flow.google.com/project/original-media',
    'https://flow.google.com/asb/original-media/other']) {
    const bad = structuredClone(asb); bad.params.restore.bindings[0].urls['original-media'] = url;
    await context.receive(bad); assert.equal(events.at(-1).success, false, url);
    assert.equal(downloads.length, 2, 'invalid host, protocol, path or media cannot start a download');
  }
  console.log('Flow parked-result recovery: exact old binding, active-batch isolation, deduplication and path/media rejection passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
