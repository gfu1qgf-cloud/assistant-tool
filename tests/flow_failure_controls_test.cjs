const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8');
function node(text = '', attrs = {}, children = {}) {
  return {textContent:text, isConnected:true, getAttribute:key=>attrs[key] ?? null,
    querySelectorAll:selector=>children[selector] || [], closest:()=>null};
}
function check(card, {ready = false, history = false, duplicate = false} = {}) {
  const events = [], failures = [], polls = [];
  const task = {taskId:'task', download_path:'FlowBridge/'+'a'.repeat(32)+'/result.mp4',
    mediaBindings:[{schema:2, source:'generation_response', taskId:'task', submissionId:'submission',
      mediaIds:['new-media'], readyIds:ready ? ['new-media'] : []}]};
  const window = {postMessage:value=>events.push(value.data), addEventListener(){}}; window.top = window;
  const document = {documentElement:null, readyState:'complete', addEventListener(){}, querySelector:()=>null,
    querySelectorAll:selector=>selector.includes('flow-video-tile') ? [card] : []};
  const context = {window, document, Date, console, location:{href:'https://flow.google.com/project/test', origin:'https://flow.google.com'},
    setInterval:fn=>{polls.push(fn); return polls.length;}, clearInterval(){}};
  vm.createContext(context); vm.runInContext(source, context);
  window.__lzxFlowTransport.attach(async()=>{}, ()=>({tasks:duplicate ? [task, {...task, taskId:'other',
    mediaBindings:[{...task.mediaBindings[0],taskId:'other'}]}] : [task]}),
    {initialized:()=>true, failed:(...args)=>failures.push(args)});
  polls.at(-1)(); polls.at(-1)();
  return {events:events.filter(event=>event?.type==='generation_failed'), failures};
}
const retry = node('', {'aria-label':'重試'});
const card = (text, buttons=[retry], attrs={}) => node(text, {'data-media-id':'new-media',...attrs},
  {'button, [role="button"]':buttons});
assert.equal(check(card('失敗 很抱歉，無法生成這部影片。我們會退還本次生成作業的費用。')).failures.length,1);
assert.equal(check(card('失敗 很抱歉，無法生成這部影片。')).events.length,1);
assert.equal(check(card('抱歉，无法生成该视频。', [node('', {title:'重试'})])).events.length,1);
assert.equal(check(card('生成失败，将退款。', [])).events.length,1);
assert.equal(check(card('', [node('', {'aria-label':'Retry generation'})], {'data-status':'failed'})).events.length,1);
assert.equal(check(card('', [retry]),{}).events.length,0,'retry control alone is not terminal failure evidence');
assert.equal(check(card('Failed to generate', [node('refresh')])).events.length,0,'a circular-arrow glyph is not proof of retry');
assert.equal(check(card('Failed to generate', [node('', {title:'Regenerate'})])).events.length,0);
assert.equal(check(card('無法生成這部影片。', [node('', {'aria-label':'重試', 'aria-disabled':'true'})])).events.length,0);
assert.equal(check(card('無法生成這部影片，退款。', [], {'aria-hidden':'true'})).events.length,0);
assert.equal(check(card('無法生成這部影片，退款。', [], {'data-media-id':'old-media'})).events.length,0);
assert.equal(check(card('無法生成這部影片，退款。', [], {'data-asset-id':'other-media'})).events.length,0);
assert.equal(check(card('無法生成這部影片，退款。'),{ready:true}).events.length,0);
assert.equal(check(card('無法生成這部影片，退款。'),{duplicate:true}).events.length,0);
assert.match(check(card('Failed to generate due to safety policy', [node('',{title:'Try again'})])).events[0].error,/safety policy/);

// Execute the actual built helper, including its exact-current-attempt guard.
const built = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8').replace(/\r\n/g,'\n');
const start = built.indexOf('    if (window.__lzxFlowTransport) {\n      if (!batchPromise)'), end = built.indexOf('    function connect() {', start);
assert(start >= 0 && end > start);
let attached, stops = 0;
const state = {isRunning:true, tasks:[{taskId:'task',download_path:'FlowBridge/'+'a'.repeat(32)+'/result.mp4',mediaBindings:[{mediaIds:['original']}]}]};
const context = {state, batchPromise:{}, window:{__lzxFlowTransport:{attach:(_handler,_snapshot,api)=>attached=api}},
  handleMessage(){}, location:{href:'https://flow.google.com/project/test'}, submitPackedTask(){throw Error('download/account fixture must never submit a paid task');}, syncPageVideoSettings(){},
  reconcileResultReceipts(){}, uploadPathsToFlow(){}, getLoadedPageImages(){}, simulateClick(){}, cleanupPageOverlays(){},
  stopBatch(){stops++; state.isRunning=false;}};
vm.createContext(context); vm.runInContext(`(function(){${built.slice(start,end)}})()`,context);
assert.equal(attached.skipDownload('b'.repeat(32)),false);
assert.equal(attached.skipDownload('../invalid'),false);
assert.equal(stops,0);
assert.equal(attached.skipDownload('a'.repeat(32)),true);
assert.equal(stops,1);
assert.deepEqual(state.tasks[0].mediaBindings[0].mediaIds,['original']);
state.tasks.push({download_path:'FlowBridge/'+'b'.repeat(32)+'/result.mp4'});
assert.equal(attached.skipDownload('a'.repeat(32)),false,'never stop another/newer local batch');
assert.equal(stops,1);
console.log('Localized retry/failed-state detection, exact media ownership, no icon-only replay, and guarded local wait release passed.');
