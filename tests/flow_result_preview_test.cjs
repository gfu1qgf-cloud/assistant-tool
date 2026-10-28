// Exercise the actual page transport and download URL probe without Chrome or
// Google requests. Payment handlers are forbidden in every fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), crypto = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8');
const background = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/background.js'), 'utf8');
const batch = 'a'.repeat(32), id = 'bound-video', project = 'https://flow.google.com/project/ours';
const url = `https://flow-content.google/video/${id}?Expires=9999999999&Signature=signed`;
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  let clock = Date.now();
  const events = [], ui = [], hooks = {};
  const binding = {schema:2, source:'generation_response', taskId:'task', submissionId:'submission', mediaIds:[id], failedIds:[]};
  const task = {taskId:'task', download_path:`FlowBridge/${batch}/result.mp4`, status:['generating'], mediaBindings:[binding]};
  const video = {tagName:'VIDEO', src:'', readyState:0, paused:true, muted:false, playCount:0, pauseCount:0,
    play() {this.playCount++; this.paused=false; this.src=url; this.readyState=1; return Promise.resolve();},
    pause() {this.pauseCount++; this.paused=true;}};
  const thumb = {tagName:'IMG', src:`https://flow-content.google/image/${id}`};
  const tile = {isConnected:true, closest:()=>null, querySelectorAll:()=>[thumb, video],
    querySelector:selector => selector === 'video' ? video : selector === 'img' ? thumb : null};
  for (const node of [tile, thumb, video]) {
    node.dispatchEvent = event => ui.push(event.type);
    node.getBoundingClientRect = () => ({left:0, top:0, width:100, height:100});
  }
  const document = {documentElement:{}, querySelector:()=>null,
    querySelectorAll:selector => selector === 'flow-video-tile' ? [tile] : []};
  const window = {postMessage:message => events.push(message.data), addEventListener:(name, callback) => {hooks[name]=callback;}};
  window.top = window;
  const location = {origin:new URL(project).origin, href:project};
  class Event {constructor(type, init) {this.type=type; Object.assign(this, init);}}
  const context = {window, document, location, crypto, URL, console, MouseEvent:Event, PointerEvent:Event,
    Date:class extends Date {static now() {return clock;}}, MutationObserver:class {observe() {}}, setInterval(){},
    setTimeout:callback => setImmediate(() => {clock+=100; callback();})};
  vm.createContext(context); vm.runInContext(source, context);
  const api = window.__lzxFlowTransport;
  api.attach(() => {throw Error('generation is forbidden');}, () => ({tasks:[task]}), {});
  const receive = message => hooks.message({source:window, origin:location.origin, data:{sender:'lzx-flow-extension', message}});
  return {api, binding, task, video, tile, thumb, document, location, context, events, ui, receive, now:()=>clock};
}
(async () => {
  const f = fixture();
  assert.equal(await f.api.wakeResultPreview(id, batch), true);
  assert.equal(f.video.playCount, 1); assert.equal(f.video.pauseCount, 1);
  assert.equal(f.video.muted, false); assert.equal(f.video.paused, true);
  assert(f.ui.includes('pointerover') && f.ui.includes('mouseenter'));

  const cold = fixture(); cold.video.src = url;
  assert.equal(await cold.api.wakeResultPreview(id, batch), true);
  assert.equal(cold.video.playCount, 1, 'a valid URL must not suppress loading a cold preview');
  const loaded = fixture(); loaded.video.src=url; loaded.video.readyState=1;
  assert.equal(await loaded.api.wakeResultPreview(id, batch), true);
  assert.equal(loaded.video.playCount, 0); assert.equal(loaded.ui.length, 0);
  const forced = fixture();forced.video.src=url;forced.video.readyState=1;const forcedStart=forced.now();
  assert.equal(await forced.api.wakeResultPreview(id,batch,true),true);
  assert(forced.ui.includes('mouseenter'),'expired but already loaded videos still get an exact-tile hover');
  assert.equal(forced.video.playCount,1);assert.equal(forced.video.pauseCount,1);
  assert.equal(forced.video.muted,false);assert(forced.now()-forcedStart>=300 && forced.now()-forcedStart<=1800);


  for (const mutate of [f=>{f.binding.source='dom';}, f=>{f.binding.mediaIds=['other'];},
    f=>{f.binding.failedIds=[id];}, f=>{f.task.status=['success'];},
    f=>{f.tile.closest=()=>({tagName:'FLOW-IMAGE-TILE'});},
    f=>{f.document.querySelectorAll=()=>[f.tile, f.tile];}]) {
    const denied = fixture(); mutate(denied);
    assert.equal(await denied.api.wakeResultPreview(id, batch), false);
    assert.equal(denied.video.playCount, 0); assert.equal(denied.ui.length, 0);
  }
  assert.equal(await fixture().api.wakeResultPreview(id, 'b'.repeat(32)), false);

  const rejected = fixture(), start=rejected.now();
  rejected.video.muted=true;
  rejected.video.play=function() {this.playCount++; return Promise.reject(Error('autoplay denied'));};
  assert.equal(await rejected.api.wakeResultPreview(id, batch), false);
  assert(rejected.now()-start <= 1800); assert.equal(rejected.video.muted, true);
  assert.equal(rejected.video.playCount, 1, 'one bounded play attempt, no per-poll playback loop');

  const hanging = fixture(); let finish;
  hanging.video.play=function() {this.playCount++; return new Promise(resolve=>{finish=()=>{this.paused=false; resolve();};});};
  assert.equal(await hanging.api.wakeResultPreview(id, batch), false, 'a never-resolving play promise cannot hold UI');
  finish(); await flush(); assert.equal(hanging.video.paused, true); assert.equal(hanging.video.muted, false);

  const locked = fixture(); await locked.receive({action:'configure_browser_options', values:{ui_mutex:true}});
  const wake = locked.api.wakeResultPreview(id, batch); await flush();
  const command = locked.events.find(e=>e.type==='ui_mutex_command');
  assert.equal(command.operation, 'retry_download'); assert.equal(command.batch_id, batch);
  assert.equal(locked.ui.length, 0, 'no hover or playback before desktop grant');
  assert.equal(await locked.api.wakeResultPreview(id, batch), false, 'duplicate wake is coalesced');
  await locked.receive({action:'ui_mutex_result', token:command.token, ok:true, state:'granted', expires_at:Date.now()/1000+60});
  assert.equal(await wake, true); assert(locked.events.some(e=>e.release && e.token===command.token));

  const refused = fixture(); await refused.receive({action:'configure_browser_options', values:{ui_mutex:true}});
  const refusedWake=refused.api.wakeResultPreview(id,batch); await flush();
  const deniedToken=refused.events.find(e=>e.type==='ui_mutex_command').token;
  await refused.receive({action:'ui_mutex_result', token:deniedToken, ok:false, state:'rejected'});
  assert.equal(await refusedWake,false); assert.equal(refused.video.playCount,0);

  const revoked = fixture(); revoked.video.play=function() {this.playCount++; return new Promise(()=>{});};
  await revoked.receive({action:'configure_browser_options', values:{ui_mutex:true}});
  const revokedWake=revoked.api.wakeResultPreview(id,batch); await flush();
  const revokedToken=revoked.events.find(e=>e.type==='ui_mutex_command').token;
  await revoked.receive({action:'ui_mutex_result', token:revokedToken, ok:true, state:'granted', expires_at:Date.now()/1000+60});
  await revoked.receive({action:'ui_mutex_revoked', token:revokedToken, state:'expired'});
  assert.equal(await revokedWake,false); assert(revoked.events.some(e=>e.release && e.token===revokedToken));
  assert.equal(revoked.video.muted,false);

  const busy=fixture(); let unblock;
  busy.api.attach(()=>{},()=>({tasks:[busy.task]}),{prepare:()=>new Promise(resolve=>{unblock=resolve;})});
  const operation=busy.receive({action:'prepare',request_id:'prepare',params:{}}); await flush();
  assert.equal(await busy.api.wakeResultPreview(id,batch),false);
  assert.equal(busy.video.playCount,0); unblock(); await operation;

  const extract=background.slice(background.indexOf('async function freshDownloadUrl('),background.indexOf('async function connect()',background.indexOf('async function freshDownloadUrl(')));
  const dom={nodes:[], querySelectorAll(){return this.nodes;}};
  const pageContext={URL,Date,location:{href:project},document:dom,window:{__lzxFlowTransport:{
    async wakeResultPreview(media,attempt) {assert.equal(media,id);assert.equal(attempt,batch);dom.nodes=[{tagName:'VIDEO',src:url,readyState:1}];}}}};
  vm.createContext(pageContext);
  const bridge={config:{project},chrome:{tabs:{async get(){return {url:project};}},scripting:{async executeScript({func,args}) {
    pageContext.args=args;return [{result:await vm.runInContext(`(${func.toString()})(...args)`,pageContext)}];}}},
    async ensureTab(){return 7;},sameProjectTab:()=>true};
  vm.createContext(bridge);vm.runInContext(extract,bridge);
  assert.equal(await bridge.freshDownloadUrl(id,batch),url, 'download probe wakes exact preview then rescans');
  dom.nodes=[]; pageContext.window.__lzxFlowTransport.wakeResultPreview=async()=>{pageContext.location.href='https://flow.google.com/project/foreign';dom.nodes=[{src:url}];};
  assert.equal(await bridge.freshDownloadUrl(id,batch),'', 'navigation during wake cannot return another project result');
  console.log('Result preview recovery: exact receipt/tile, cold URL, bounded muted playback, denied/expired lock, busy page, late play and project-scoped URL rescan passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
