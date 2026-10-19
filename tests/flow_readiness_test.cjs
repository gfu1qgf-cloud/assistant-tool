const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8');
const settings = {videoModel: 'omni_1_1_flash', aspectRatio: 'ASPECT_RATIO_9_16', resolution: 'VIDEO_RESOLUTION_720P'};

function fixture({appear = 5000, hidden = false, navigate = false} = {}) {
  let time = 0, preparations = 0, prompt;
  const events = [], hooks = {}, preparedAt = [];
  const node = () => ({isConnected: true, getAttribute() {return null;}, getClientRects() {return hidden ? [] : [{}];}});
  prompt = node(); const trigger = {...node(), textContent: 'Omni 1.1 Flash 720p 9:16 x1'};
  const window = {postMessage(value) {events.push(value.data);}, addEventListener(type, callback) {hooks[type] = callback;}};
  window.top = window;
  const location = {href: 'https://flow.google.com/project/selected', origin: 'https://flow.google.com'};
  class Clock extends Date {static now() {return time;}}
  const context = {window, location, Date: Clock, console, MutationObserver: class {observe() {}},
    setInterval() {}, setTimeout(callback, delay) {time += delay; if (navigate && time >= 2000) location.href = location.origin+'/project/another'; queueMicrotask(callback);},
    document: {readyState: 'complete', visibilityState: 'hidden', documentElement: {},
      querySelector(selector) {
        if (selector.includes('ProseMirror')) return time >= appear ? prompt : null;
        if (selector === 'button.settings-trigger-button') return time >= appear ? trigger : null;
        if (selector === 'flow-account-panel') return {textContent: 'saved@example.test',
          querySelectorAll(){return [{textContent:'saved@example.test',children:[]}];}};
        return null;
      }, querySelectorAll() {return [];}}};
  vm.createContext(context); vm.runInContext(source, context);
  const api = window.__lzxFlowTransport;
  api.attach(async () => {}, () => ({url: location.href, tasks: []}), {
    async prepare(params) {preparations++; preparedAt.push(time); api.verifySettings(params);}});
  return {api, events, preparedAt, get preparations() {return preparations;},
    setTime(value) {time = value;}, remount() {prompt = node();},
    async prepare() {await hooks.message({source: window, origin: location.origin,
      data: {sender: 'lzx-flow-extension', message: {action: 'prepare', request_id: 'prepare',
        params: {...settings, wait_for_ready: true}}}});}};
}

(async () => {
  const slow = fixture();
  assert.equal(slow.api.readiness().controls_ready, false);
  await slow.prepare();
  assert.equal(slow.preparations, 1);
  assert(slow.preparedAt[0] >= 6500, 'DOM appearing is not enough: controls must remain stable for 1.5 seconds');
  assert.equal(slow.events.find(e => e.request_id === 'prepare').success, true);
  assert(slow.events.some(e => e.type === 'snapshot' && e.data.controls_ready === false));
  assert.equal(slow.api.readiness().controls_ready, true, 'background browser windows remain supported');
  slow.remount(); assert.equal(slow.api.readiness().controls_ready, false, 'Angular remount resets stabilization');

  const missing = fixture({appear: Infinity}); await missing.prepare();
  assert.equal(missing.preparations, 0);
  assert.equal(missing.events.find(e => e.request_id === 'prepare').code, 'FLOW_PAGE_NOT_READY');
  const invisible = fixture({appear: 0, hidden: true}); await invisible.prepare();
  assert.equal(invisible.preparations, 0, 'hidden/skeleton controls cannot authorize preparation');
  const changed = fixture({appear: 5000, navigate: true}); await changed.prepare();
  assert.equal(changed.preparations, 0, 'waiting never follows another project');
  const built = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8');
  const begin = built.indexOf('  function handleUnusualActivityDetected()');
  const end = built.indexOf('  function pauseAlarmCountdown()', begin);
  assert(begin >= 0 && end > begin);
  let alarms = 0, reloads = 0, pauses = 0;
  const warning = {state: {}, console: {warn() {}}, saveState() {}, alarmDialogEl: {style: {}},
    playAlarmSound() {}, alarmTimer: 1, alarmCountdown: 0, document: {getElementById() {return {}; }},
    window: {__lzxFlowTransport: {sitePaused() {pauses++;}}},
    setInterval() {alarms++;}, clearInterval() {}, location: {reload() {reloads++;}}};
  vm.createContext(warning); vm.runInContext(built.slice(begin, end), warning);
  warning.handleUnusualActivityDetected();
  assert.equal(warning.state.isSuspended, true);
  assert.equal(pauses, 1); assert.equal(alarms, 0); assert.equal(reloads, 0,
    'upstream safety warnings must pause in bridge mode, never start a reload countdown');
  console.log('Flow readiness: delayed controls, stability, remount, hidden controls, bounded timeout and project-change rejection passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
