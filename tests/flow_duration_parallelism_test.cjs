const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.LZX_FLOW_COMPARE_ROOT || path.join(__dirname, '..');
const adapter = fs.readFileSync(path.join(root, 'browser_extensions/flow_adapter/page.js'), 'utf8');
const built = fs.readFileSync(path.join(root, 'browser_extensions/flow_bridge/injects/ui.js'), 'utf8');
const durationStart = built.indexOf('  function durationError(');
const durationEnd = built.indexOf('  // 自动切换生成模型', durationStart);
const normalizeStart = built.indexOf('  function normalizeDuration(');
const normalizeEnd = built.indexOf('  // 手动解析 JSON', normalizeStart);
assert(durationStart >= 0 && durationEnd > durationStart && normalizeStart >= 0);

function fixture({initialOpen = false, mountDelay = 0, available = [6,8,10], model = 'Omni 1.1 Flash',
                   missing = false, remountOnClick = false, id = 'fixture'} = {}) {
  let now = 100000, view = null, opens = 0, clicks = 0, mountAt = Infinity, selected = 8;
  const hooks = {}, messages = [];
  function option(seconds) {
    return {textContent: seconds + 's', isConnected: true,
      classList: {contains: () => selected === seconds}, getAttribute: () => null,
      querySelector: () => ({getAttribute: () => null, click() {
        clicks++; selected = seconds;
        if (remountOnClick) {view.isConnected = false; view = null; mountAt = now + 400;}
      }})};
  }
  function group(label, values) {return {getAttribute: () => label, querySelectorAll: () => values};}
  function panel() {
    const groups = [group('Aspect ratio', [{...option(1), textContent:'9:16', classList:{contains:()=>true}}]),
      group('Resolution', [{...option(1), textContent:'720p', classList:{contains:()=>true}}]),
      group('Outputs', [{...option(1), textContent:'x1', classList:{contains:()=>true}}]),
      group('Duration', available.map(option))];
    return {isConnected:true, getAttribute:()=>null,
      querySelector: selector => selector === '.model-select-trigger-content' ? {textContent:model} : null,
      querySelectorAll:()=>groups};
  }
  if (initialOpen) view = panel();
  const trigger = {isConnected:true, getAttribute:()=>null, textContent:'Omni 1.1 Flash 720p 9:16 x1 8s',
    click() {opens++; mountAt = now + mountDelay; if (!missing && !mountDelay) view = panel();}};
  const stale = {...panel(), isConnected:false};
  const window = {postMessage(value) {messages.push(value.data);}, addEventListener(type, callback) {hooks[type]=callback;}};
  window.top=window;
  const context = {window, console:{log(){},warn(){}}, Date:{now:()=>now},
    location:{href:'https://labs.google/fx/tools/flow/project/'+id, origin:'https://labs.google'},
    MutationObserver:class{observe(){}}, setInterval(){}, isStopRequested:false,
    document:{documentElement:{}, querySelector(selector) {
      if (selector==='button.settings-trigger-button') return trigger;
      if (selector==='flow-account-panel') return {querySelectorAll:()=>[{textContent:id+'@example.test',children:[]}]};
      if (selector.includes('flow-prompt-box-settings')) return view;
      return null;
    }, querySelectorAll(selector) {return selector.includes('flow-prompt-box-settings') ? [stale,view].filter(Boolean) : [];}}};
  function advance(callback, delay) {now += delay; if (!missing && mountAt <= now && !view) view=panel(); queueMicrotask(callback);}
  context.setTimeout=advance;
  context.delay=ms=>new Promise(resolve=>advance(resolve,ms));
  vm.createContext(context); vm.runInContext(adapter,context);
  vm.runInContext(built.slice(normalizeStart,normalizeEnd)+built.slice(durationStart,durationEnd),context);
  const transport=window.__lzxFlowTransport;
  return {context,transport,hooks,messages,run:seconds=>context.applyDurationSetting(seconds),
    get selected(){return selected;},get opens(){return opens;},get clicks(){return clicks;},get elapsed(){return now-100000;}};
}

(async()=>{
  const closed=fixture(); await closed.run(10);
  assert.equal(closed.selected,10); assert.equal(closed.opens,1); assert.equal(closed.clicks,1);
  const remount=fixture({initialOpen:true,remountOnClick:true}); await remount.run(10);
  assert.equal(remount.selected,10); assert.equal(remount.clicks,1,'rerender must re-read selected state without repeat clicking');
  const settings={videoModel:'omni_1_1_flash',aspectRatio:'ASPECT_RATIO_9_16',resolution:'VIDEO_RESOLUTION_720P',count:1,duration:10};
  assert.equal(remount.transport.verifySettings(settings,remount.transport.settingsPanel()),true);
  assert.throws(()=>remount.transport.verifySettings({...settings,duration:8},remount.transport.settingsPanel()),
    error=>error.code==='FLOW_SETTINGS_MISMATCH','actual selected seconds override stale collapsed summary');
  const unsupported=fixture({initialOpen:true,available:[6,8]});
  await assert.rejects(()=>unsupported.run(10),error=>error.code==='FLOW_DURATION_UNSUPPORTED' &&
    JSON.stringify(error.data.available_durations)==='[6,8]');
  const wrongModel=fixture({initialOpen:true,available:[6,8],model:'Veo 3.1 Fast'});
  await assert.rejects(()=>wrongModel.run(10),error=>error.code==='FLOW_SETTINGS_NOT_READY',
    'a different model cannot establish an Omni duration limit');
  const missing=fixture({missing:true});
  await assert.rejects(()=>missing.run(10),error=>error.code==='FLOW_SETTINGS_NOT_READY');
  assert(missing.elapsed>=8000 && missing.elapsed<=8200); assert(missing.opens<=3);
  const pages=Array.from({length:4},(_,i)=>fixture({id:'profile'+i,mountDelay:i*400,remountOnClick:i===2}));
  await Promise.all(pages.map(page=>page.run(10)));
  for(const page of pages) assert.equal(page.selected,10);
  // Exercise the real page command error envelope used by the desktop policy.
  const protocol=fixture({initialOpen:true,available:[6,8]});
  protocol.transport.attach(async()=>{},()=>({tasks:[],running:false}),{prepare:params=>protocol.run(params.duration)});
  await protocol.hooks.message({source:protocol.context.window,origin:protocol.context.location.origin,
    data:{sender:'lzx-flow-extension',message:{action:'prepare',request_id:'unsupported',params:settings}}});
  const response=protocol.messages.find(item=>item.request_id==='unsupported');
  assert.equal(response.code,'FLOW_DURATION_UNSUPPORTED'); assert.equal(response.data.duration,10);
  console.log('Flow duration parallelism: closed/replaced panels, selected seconds, scoped unsupported options, bounded waits and four concurrent pages passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
