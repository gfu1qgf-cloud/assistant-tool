const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8');
const settings = {videoModel: 'omni_1_1_flash', aspectRatio: 'ASPECT_RATIO_9_16',
  resolution: 'VIDEO_RESOLUTION_720P', count: 1, duration: 8};
function node(text, checked = true) {
  return {textContent: text, isConnected: true, classList: {contains: () => checked},
    getAttribute: () => null, querySelector: () => null};
}
function panel(model = 'Omni 1.1 Flash', count = 'x1') {
  const groups = [['Aspect ratio', '9:16'], ['Resolution', '720p'], ['Outputs', count]];
  return {isConnected: true, getAttribute: () => null,
    querySelector: selector => selector === '.model-select-trigger-content' ? node(model) : null,
    querySelectorAll: () => groups.map(([label, value]) => ({getAttribute: () => label,
      querySelectorAll: () => [node(value), node('unselected x1 720p 9:16 Omni 1.1 Flash', false)]}))};
}
function page(id, viewAt) {
  let time = 100000, view = viewAt(0);
  const context = {window: {postMessage() {}, addEventListener() {}},
    location: {origin: 'https://labs.google', href: 'https://labs.google/project/' + id},
    Date: {now: () => time}, MutationObserver: class {observe() {}}, setInterval() {},
    setTimeout(callback, ms) {time += ms; view = viewAt(time - 100000); callback();},
    document: {documentElement: {}, querySelector(selector) {
      if (selector === 'button.settings-trigger-button') return node('Omni 1.1 Flash');
      return null;
    }, querySelectorAll(selector) {return selector.includes('flow-prompt-box-settings') ? [view].filter(Boolean) : [];}}};
  context.window.top = context.window; vm.createContext(context); vm.runInContext(source, context);
  return {transport: context.window.__lzxFlowTransport, context, elapsed: () => time - 100000};
}
(async () => {
  // A new visible panel replaces the detached one after Angular finishes a
  // mode switch. The requested settings and unselected options are not proof.
  const detached = {...panel('Veo 3.1 Fast', 'x2'), isConnected: false};
  const slow = page('slow', elapsed => elapsed >= 600 ? panel() : detached);
  await slow.transport.waitForSettings(settings);
  assert.ok(slow.elapsed() >= 600, 'must await the new actual selected controls');

  const wrong = page('wrong', () => panel('Veo 3.1 Fast', 'x2'));
  await assert.rejects(() => wrong.transport.waitForSettings(settings), error => error.code === 'FLOW_SETTINGS_MISMATCH');
  const absent = page('absent', () => null);
  await assert.rejects(() => absent.transport.waitForSettings(settings), error => error.code === 'FLOW_SETTINGS_NOT_READY');
  assert.ok(absent.elapsed() >= 5000 && absent.elapsed() <= 5500, 'missing controls have a bounded wait');

  // Six distinct page contexts settle independently; a slow or wrong page
  // must not serialize or overwrite another profile's proof.
  const pages = Array.from({length: 6}, (_, i) => page('profile-' + i,
    elapsed => elapsed >= i * 200 ? panel() : detached));
  await Promise.all(pages.map(p => p.transport.waitForSettings(settings)));
  for (const p of pages) assert.equal(p.transport.verifySettings(settings, p.transport.settingsPanel()), true);

  // Execute the actual built upstream toggle helper against replaced panels.
  const built = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8');
  const start = built.indexOf('    function setToggle(ariaLabels, matcher) {');
  const end = built.indexOf('    // 3.1', start);
  let active = null, oldClicks = 0, newClicks = 0;
  function toggleView(click) {
    const option = {textContent: '720p', cloneNode: () => ({textContent:'720p', querySelectorAll: () => []}),
      classList: {contains: () => false}, querySelector: selector => selector === 'mat-icon' ? null :
        {getAttribute: () => null, click}};
    return {querySelector: () => ({querySelectorAll: () => [option]})};
  }
  const old = toggleView(() => oldClicks++);
  const newer = toggleView(() => newClicks++);
  const context = {window: {__lzxFlowTransport: {settingsPanel: () => active}}, console, initial: old,
    Object, Error};
  vm.createContext(context);
  vm.runInContext('let overlay = initial; ' + built.slice(start, end) + '; globalThis.set = setToggle;', context);
  active = old; context.set('Resolution', '720p');
  active = newer; context.set('Resolution', '720p');
  assert.equal(oldClicks, 1); assert.equal(newClicks, 1, 'every field must target the current live panel');
  console.log('Flow concurrent settings: live panel replacement, bounded wait, strict mismatch and six independent profiles passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
