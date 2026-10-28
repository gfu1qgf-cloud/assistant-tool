const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/lzx-page.js'),'utf8');
function fixture(){
  let now=Date.parse('2026-10-23T12:00:00Z');
  const state={open:false,hidden:false,account:'manual@example.test',balance:'5',running:false},messages=[];
  const forbidden=()=>{throw Error('passive detection must not operate the browser');};
  const node=text=>({textContent:text,children:[],getAttribute:()=>null,closest:()=>null,getClientRects:()=>state.hidden?[]:[{}],click:forbidden});
  const panel={...node(''),matches:()=>true,querySelector:()=>null,querySelectorAll(selector){
    if(selector==='*')return [node(state.account)];
    if(selector.includes('credits-count'))return [node(state.balance)];
    return [];
  }};
  const document={documentElement:{},querySelector(selector){return state.open&&selector.includes('flow-account-panel')?panel:null;},
    querySelectorAll(selector){return state.open&&selector==='flow-account-panel, .flow-account-panel-overlay'?[panel]:[];},
    addEventListener(){},hasFocus:()=>true,visibilityState:'visible',dispatchEvent:forbidden};
  class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  const window={postMessage(data){messages.push(data.data);},addEventListener(){},focus:forbidden};window.top=window;
  const context={window,document,location:{href:'https://flow.google.com/project/passive',origin:'https://flow.google.com'},
    Date:Clock,setInterval(){return 1;},clearInterval(){},MutationObserver:class{observe(){}},fetch:forbidden};
  vm.runInNewContext(source,context);
  const api=window.__lzxFlowTransport;
  api.attach(()=>{},()=>({tasks:[],running:state.running}),{account:forbidden,click:forbidden,reconcileResults(){}});
  const tick=(ms=1000)=>{now+=ms;api.batchIdle();};
  const observed=()=>messages.filter(item=>item.type==='credit_observation');
  return {state,tick,observed};
}
const f=fixture();f.tick();f.tick();assert.equal(f.observed().length,0,'closed menu must stay closed');
f.state.open=true;f.tick();f.tick();assert.equal(f.observed().length,1);
assert.equal(f.observed()[0].account,'manual@example.test');assert.equal(f.observed()[0].balance,5);
f.tick();assert.equal(f.observed().length,1,'unchanged visible balance is throttled');
f.state.balance='0';f.tick();f.tick();assert.equal(f.observed().at(-1).balance,0);
for(const bad of ['点数每天21:24刷新','refund 50 cost 10','9999999999','-5','']){
  const x=fixture();x.state.open=true;x.state.balance=bad;x.tick();x.tick();assert.equal(x.observed().length,0,bad);
}
const hidden=fixture();hidden.state.open=true;hidden.state.hidden=true;hidden.tick();hidden.tick();assert.equal(hidden.observed().length,0);
const busy=fixture();busy.state.open=true;busy.state.running=true;busy.tick();busy.tick();assert.equal(busy.observed().length,0);
const changed=fixture();changed.state.open=true;changed.tick();changed.state.account='other@example.test';changed.tick();
assert.equal(changed.observed().length,0);changed.tick();assert.equal(changed.observed()[0].account,'other@example.test');
console.log('Passive credit observation: closed/hidden/busy panels ignored, zero accepted, natural account changes tracked, no click/focus/fetch/close and throttled updates passed.');
