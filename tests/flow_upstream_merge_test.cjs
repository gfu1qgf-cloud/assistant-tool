// Execute the merged upstream seams offline; no live browser or paid generation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8').replace(/\r\n/g, '\n');
const ui = read('browser_extensions/flow_bridge/injects/ui.js');
const proxy = read('browser_extensions/flow_bridge/injects/flow-proxy.js');
const background = read('browser_extensions/flow_bridge/background.js');
const quiet = {log() {}, warn() {}, error() {}};
const signed = 'https://flow-content.google/video/our-media?Expires=9999999999&Signature=abc%2Bdef%3D%2Fxyz';
const manifest = JSON.parse(read('browser_extensions/flow_bridge/manifest.json'));
const provenance = JSON.parse(read('browser_extensions/flow_bridge/SOURCE.json'));
const derivedId = crypto.createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest('hex').slice(0, 32)
  .replace(/[0-9a-f]/g, ch => String.fromCharCode(97 + parseInt(ch, 16)));
assert.equal(derivedId, 'pnjppjjfgiccpcfajkpooklgohgihdom', 'upstream merge must not create a new extension identity');
assert.equal(provenance.upstream_version, '1.1.4');
assert.equal(provenance.commit, 'e4e8f34ea6a0e0d7d9e0e87bbf7218e66d315ec2');
assert.equal(provenance.bridge_version, manifest.version);

async function testDownloadLock() {
  const issued = [], bound = [], items = new Map(), stored = {};
  let next = 1, fail = false;
  const context = {URL, console: quiet, recentAutoDownloads: new Map(),
    chrome: {runtime: {}, storage: {session: {async set(value) {Object.assign(stored, value);}}},
      downloads: {async search({id}) {return items.has(id) ? [items.get(id)] : [];},
        download(options, callback) {
          issued.push(options);
          if (fail) {context.chrome.runtime.lastError = {message: 'subdirectory unavailable'}; callback(); return;}
          const id = next++;
          items.set(id, {id, state: 'in_progress', exists: true}); callback(id);
        }}},
    async notifyDownload() {}, async __lzxDownloadBound(id, task) {bound.push({id, task});}};
  vm.createContext(context);
  const start = background.indexOf('async function handleDownload(');
  vm.runInContext(background.slice(start, background.indexOf('// 只处理本扩展发起', start)), context);
  const filename = 'FlowBridge/'+'a'.repeat(32)+'/result.mp4';
  const task = {taskId: 'task', submissionId: 'submission', mediaId: 'our-media', download_path: filename};
  const results = await Promise.all([context.handleDownload(signed, filename, task), context.handleDownload(signed, filename, task)]);
  assert.equal(issued.length, 1, 'simultaneous repeated messages must create one download');
  assert.equal(results[0].downloadId, results[1].downloadId);
  assert.equal(issued[0].saveAs, false, 'automatic download must not ask for Save As');
  assert.equal(issued[0].conflictAction, 'uniquify');
  assert.equal(bound[0].task.download_path, filename);
  items.get(1).state = 'interrupted';
  const retried = await context.handleDownload(signed, filename, task);
  assert.equal(retried.downloadId, 2, 'an interrupted download must not stay cached as success');
  items.get(2).state = 'complete'; items.get(2).exists = false;
  assert.equal((await context.handleDownload(signed, filename, task)).downloadId, 3,
    'a deleted download must not block recovery');
  const issuedBefore = issued.length;
  assert(!(await context.handleDownload(signed, filename, {...task, manual: true})).success,
    'managed jobs cannot bypass ownership through the upstream manual path');
  assert.equal(issued.length, issuedBefore);
  fail = true;
  const another = 'FlowBridge/'+'b'.repeat(32)+'/result.mp4';
  assert(!(await context.handleDownload(signed, another, {...task, download_path: another})).success);
  assert.equal(issued.length, issuedBefore+1, 'managed failed downloads cannot flatten the per-attempt output path');
  assert.equal(issued.at(-1).filename, another);
}

function testRpcAndSigning() {
  const context = {URL, console: quiet, window: {__lzxFlowTransport: {}, dispatchEvent() {}}};
  vm.createContext(context);
  vm.runInContext(proxy.slice(proxy.indexOf('  function unescapeBoQ('), proxy.indexOf('  function setupFetchHook(')), context);
  const a = JSON.stringify(['aaaa']), b = JSON.stringify(['bbbb']);
  assert.equal(a.length, b.length);
  const frames = context.readRpcFrames([['wrb.fr', 'jwpduf', a], ['wrb.fr', 'jwpduf', b]]);
  assert.equal(frames.length, 2, 'different RPC frames with equal payload length must both survive');
  assert.equal(context.readRpcFrames([['wrb.fr', 'jwpduf', a], ['wrb.fr', 'jwpduf', a]]).length, 1);
  const escaped = JSON.stringify(JSON.stringify({url: signed}));
  const urls = context.extractMediaAssets(escaped, true).urls;
  assert(urls.includes(signed), 'multi-layer escaped CDN signing query must stay complete');
  assert.equal(new URL(urls[0]).searchParams.get('Signature'), 'abc+def=/xyz');
  assert.equal(context.extractMediaAssets('https://flow-content.google/video/our-media?Expires=9999999999').urls.length, 0,
    'a truncated signing URL must not be downloaded as an XML error');
}

function testMessageRetryRules() {
  const posted = [], timers = [];
  const context = {window: {postMessage(message) {posted.push(message);}}, console: quiet,
    setTimeout(callback, ms) {timers.push({callback, ms});}};
  vm.createContext(context);
  const start = ui.indexOf('  const extensionCallbacks = new Map();');
  vm.runInContext(ui.slice(start, ui.indexOf('  // 监听来自 content-isolated.js', start)), context);
  for (const action of ['nativeFileDrag', 'download']) {
    const before = posted.length, timerStart = timers.length;
    context.sendToExtension(action, {path: 'test'});
    for (const timer of timers.slice(timerStart)) if ([800, 2500].includes(timer.ms)) timer.callback();
    assert.equal(posted.length, before+1, action+' must not be automatically sent a second time during startup handshake');
  }
}

async function testOneClickAndEmptyBinding() {
  let clicks = 0;
  const editor = {textContent: 'one prompt', focus() {}, dispatchEvent() {},
    pmViewDesc: {view: {state: {schema: {nodes: {paragraph: {create(_attrs, text) {return text;}},
      doc: {create(_attrs, content) {return {content};}}}, text: text => text},
      doc: {content: {size: 0}}, tr: {replaceWith() {return {};}}}, dispatch() {}}}};
  const button = {disabled: false, hasAttribute() {return false;}, getAttribute() {return null;},
    classList: {contains() {return false;}}, querySelectorAll() {return [];}, querySelector() {return null;},
    getBoundingClientRect() {return {left: 10, top: 10, width: 40, height: 40};}, click() {clicks++;}};
  const context = {console: quiet, crypto, window: {__lzxFlowTransport: {}},
    state: {settings: {}, tasks: [{}]}, isStopRequested: false,
    document: {querySelector() {return editor;}},
    InputEvent: class {}, Event: class {}, MouseEvent: class {},
    async syncPageVideoSettings() {}, async cleanupPageOverlays() {}, async saveState() {}, async delay() {},
    findFlowGenerateButton: () => button, getStorageKey: () => 'project',
    resolveDownloadPath: value => value, async sendToExtension() {return {success: true};},
    snapshotCurrentDomMedia() {}, isFlowCurrentlyGenerating: () => false, updateLineStatus() {}};
  vm.createContext(context);
  const start = ui.indexOf('  async function submitTaskToPage(');
  vm.runInContext(ui.slice(start, ui.indexOf('  function getProjectTiles()', start)), context);
  await context.submitTaskToPage({lineText: 'one prompt', rowIndex: 0, lineIndex: 0,
    download_path: 'FlowBridge/'+'a'.repeat(32)+'/result.mp4'});
  assert.equal(clicks, 1, 'even a missing network acknowledgement must not trigger another paid click');
  const task = context.state.tasks[0], binding = task.mediaBindings[0];
  const retryStart = ui.indexOf('  async function retrySavedVideo(');
  vm.runInContext(ui.slice(retryStart, ui.indexOf('  async function triggerAutoDownload(', retryStart)), context);
  assert.equal(await context.retrySavedVideo(task, 0, 0), true);
  assert.equal(task.mediaBindings[0], binding, 'an empty original binding must not be removed to enable resubmission');
  assert.equal(await context.retrySavedVideo(task, 0, 0, true), false);
  assert.equal(clicks, 1);
}

(async () => {
  await testDownloadLock(); testRpcAndSigning(); testMessageRetryRules(); await testOneClickAndEmptyBinding();
  console.log('FlowCue 1.1.4 merge: stable extension ID, exact upstream metadata, saveAs=false, duplicate-download lock/retry, signing, equal-length RPC frames and single paid click passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
