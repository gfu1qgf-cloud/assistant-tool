const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8');
const start = source.indexOf('  function triggerTileHover('), end = source.indexOf('  // DOM 视频观察器', start);
assert(start > 0 && end > start);
function check(bridge) {
  const context = {window: bridge ? {__lzxFlowTransport: {}} : {},tileHoverHistory:new WeakMap()};
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  let scrolls = 0, events = 0;
  const tile = {tagName: 'FLOW-VIDEO-TILE', querySelector() {return null;}, closest() {return null;},
    scrollIntoView() {scrolls++;}, dispatchEvent() {events++;},
    getBoundingClientRect() {return {top: 1200, left: 0, width: 300, height: 200};}};
  for (let n=0; n<40; n++) context.triggerTileHover(tile);
  assert.equal(scrolls, 0, 'new upstream and bridge both preserve the viewport');
  assert(events <= 9, 'new upstream throttles repeated hover events');
  assert(events > 0, 'lazy-load hover discovery remains enabled without moving the viewport');
}
check(true); check(false);
const pageWindow = {postMessage() {}, addEventListener() {}}; pageWindow.top = pageWindow;
const context = {window: pageWindow, location: {origin: 'https://flow.google.com', href: 'https://flow.google.com/project/fixture'},
  document: {documentElement: {}, querySelector() {return null;}}, MutationObserver: class {observe() {}}, setInterval() {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8'), context);
const transport = pageWindow.__lzxFlowTransport;
transport.attach(() => {}, () => ({tasks: [{taskId: 'current', mediaBindings: [{schema: 2,
  mediaIds: ['new-media'], readyIds: ['new-media']}]}]}), {});
let located = 0;
const currentTile = {scrollIntoView() {located++;}, isConnected: true};
assert.equal(transport.locateResult(currentTile, 'wrong-media', 'current'), false);
assert.equal(transport.locateResult(currentTile, 'new-media', 'other-task'), false);
assert.equal(transport.locateResult(currentTile, 'new-media', 'current'), true);
for (let n=0; n<40; n++) transport.locateResult(currentTile, 'new-media', 'current');
assert.equal(located, 1, 'the current result is located once by task/media identity, never by page order');
assert(source.includes('bound.task === state.tasks[state.currentIndex]'), 'old rows must not request automatic result positioning');
console.log('Flow viewport: repeated bridge video scans do not scroll; hover discovery and upstream standalone behavior retained.');
