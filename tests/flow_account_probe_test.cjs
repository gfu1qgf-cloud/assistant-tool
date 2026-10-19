const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8').replace(/\r\n/g, '\n');
const adapter = read('browser_extensions/flow_adapter/page.js');
const built = read('browser_extensions/flow_bridge/injects/ui.js');
const upstream = read('third_party/FlowCue/extension/injects/ui.js');
const slice = text => text.slice(text.indexOf('    const EXACT_EMAIL_RE'), text.indexOf('    function countMediaTiles()'));
assert.equal(slice(built), slice(upstream), 'account and credits readers must stay identical to pinned FlowCue');

async function probe({email = 'actual@example.test', displayName = '', menuOpen = false, aria = false, pointsVisible = false, points = 50} = {}) {
  let opened = menuOpen, clicks = 0;
  const messages = [], hooks = {};
  const window = {postMessage(v) {messages.push(v.data);}, addEventListener(k,v) {hooks[k]=v;}};
  window.top = window;
  const document = {documentElement:{}, querySelector(selector) {
    if (selector==='flow-account-panel button.close-button') return {click(){opened=false;}};
    if (selector.includes('flow-account-panel')) return opened && !aria ? {textContent:displayName+email,
      querySelectorAll(){return [{textContent:displayName,children:[]},{textContent:email,children:[]}];}} : null;
    if (selector.includes('span.credits-count')) return (opened || pointsVisible) && points!==null ? {textContent:String(points)} : null;
    if (selector==='div.header-user-button[role="button"]') return {click(){opened=true;clicks++;}};
    return null;
  }, querySelectorAll(selector) {return aria && selector.includes("[aria-label*='@']") ? [{getAttribute(){return 'Google Account: '+email;}}] : [];}};
  const context = {window,document,location:{href:'https://flow.google.com/project/test',origin:'https://flow.google.com'},
    console,setInterval(){},MutationObserver:class{observe(){}},isVidsPage:()=>false,sleep:async()=>{},log(){}};
  vm.createContext(context);vm.runInContext(adapter,context);
  const transport=window.__lzxFlowTransport;
  const start=built.indexOf('      if (action === "get_points") {');
  const end=built.indexOf('      if (action === "clear_tasks") {',start);
  context.send = msg => transport.send(msg);
  vm.runInContext('let cachedEmail="stale@example.test", cachedPoints=999, lastProbeAt=0, probing=null; const clientId="test"; const EMAIL_RE=/([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,})/;\n'+slice(built)+
    '\nasync function handleMessage(msg){ const action=msg.action;'+built.slice(start,end)+'}',context);
  transport.attach(context.handleMessage,()=>({tasks:[]}),{});
  await hooks.message({source:window,origin:context.location.origin,data:{sender:'lzx-flow-extension',
    message:{action:'get_points',request_id:'probe',params:{wait_for_ready:true}}}});
  return {reply:messages.find(m=>m.action==='get_points_response'),clicks,opened};
}
(async()=>{
  const normal=await probe();
  assert.equal(normal.reply.data.email,'actual@example.test');assert.equal(normal.reply.data.remaining_points,50);
  assert.equal(normal.opened,false);assert.equal(normal.reply.success,true);
  const named = await probe({displayName:'EnglishDisplayName', menuOpen:true});
  assert.equal(named.reply.data.email,'actual@example.test','bridge must not prepend the display name to upstream email');
  const aria=await probe({aria:true,pointsVisible:true});
  assert.equal(aria.reply.data.email,'actual@example.test','reuse upstream aria-label identity even when no custom panel exists');
  assert.equal(aria.clicks,0,'visible credits and upstream identity do not need menu toggling');
  const empty=await probe({email:''});assert.equal(empty.reply.success,false);assert.equal(empty.reply.data.email,'');
  assert.equal(empty.reply.code,'FLOW_ACCOUNT_NOT_READY','fresh probe must clear stale identity');
  const zero=await probe({points:0});assert.equal(zero.reply.data.remaining_points,0);assert.equal(zero.reply.success,true);
  console.log('Flow account: actual upstream reader + handler + bridge; closed menu, aria identity, absent generation controls, zero credits and cache reset passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
