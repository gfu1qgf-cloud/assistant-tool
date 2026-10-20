const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const project='https://flow.google.com/project/fixture',batch='a'.repeat(32),extension='pnjppjjfgiccpcfajkpooklgohgihdom';
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
async function fixture({health,entry,downloads={},changed=false}={}) {
  const data={lzxConfig:{token:'x'.repeat(40),port:18189,profile:'Profile 1',project},lzxBatch:entry?batch:'',
    lzxBatches:entry?{[batch]:entry}:{},lzxDownloads:downloads};
  const hooks={},reloads=[],messages=[]; const event=name=>({addListener:fn=>hooks[name]=fn});
  const tab={id:7,windowId:1,url:project,active:true};
  class Socket {static OPEN=1;constructor(){this.readyState=1;}send(value){messages.push(JSON.parse(value));}close(){}}
  const chrome={runtime:{id:extension,getManifest:()=>({version:'1.1.18'}),onMessage:event('message'),onStartup:event('startup'),onInstalled:event('installed')},
    storage:{onChanged:event('changed'),local:{get:async keys=>Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,structuredClone(data[k])])),
      set:async value=>Object.assign(data,structuredClone(value)),remove:async key=>delete data[key]}},
    tabs:{query:async()=>[tab],get:async()=>({...tab,url:changed?'https://flow.google.com/project/other':project}),
      sendMessage:async()=>health,reload:async id=>reloads.push(id)},
    downloads:{onChanged:event('download'),search:async()=>[]},alarms:{onAlarm:event('alarm')},action:{onClicked:event('action')}};
  const context={chrome,WebSocket:Socket,URL,URLSearchParams,crypto,Date,console,setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){}};
  vm.createContext(context);vm.runInContext(source.replace("import './background.js';",'')+'\nglobalThis.api={repairProject,receive};',context);
  await new Promise(resolve=>setImmediate(resolve));
  return {api:context.api,reloads,data,messages};
}
(async()=>{
  const command={action:'repair_project',request_id:'repair',params:{idle:true,url:project}};
  const cold=await fixture(); await Promise.all([cold.api.repairProject(command),cold.api.repairProject(command)]);
  assert.deepEqual(cold.reloads,[7],'cold page without script handshake is reloaded exactly once');
  await assert.rejects(()=>cold.api.repairProject(command),/等待加载/);
  const healthy=await fixture({health:{healthy:true,running:false}});await healthy.api.repairProject(command);assert.equal(healthy.reloads.length,0);
  for(const state of ['sent','accepted']) {
    const possiblePaid=await fixture({entry:{state}});
    await assert.rejects(()=>possiblePaid.api.repairProject(command),e=>e.code==='FLOW_PAGE_REPAIR_BUSY');
    assert.equal(possiblePaid.reloads.length,0);
  }
  const running=await fixture({health:{healthy:false,running:true}});
  await assert.rejects(()=>running.api.repairProject(command),e=>e.code==='FLOW_PAGE_REPAIR_BUSY');
  const completed=await fixture({entry:{state:'accepted'},downloads:{10:{batch,reported:'complete'}}});
  await completed.api.repairProject(command);assert.deepEqual(completed.reloads,[7]);
  const other=await fixture({changed:true});await assert.rejects(()=>other.api.repairProject(command),/地址已变化/);assert.equal(other.reloads.length,0);
  const unapproved=await fixture();await assert.rejects(()=>unapproved.api.repairProject({...command,params:{url:project}}),/空闲确认/);
  const adapter=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8');
  let observerCalls=0;const domHooks={};
  const window={postMessage(){},addEventListener(){}};window.top=window;
  const context={window,location:{origin:'https://flow.google.com',href:project},document:{documentElement:null,
    addEventListener(name,fn){domHooks[name]=fn;},querySelector:()=>null,querySelectorAll:()=>[]},
    MutationObserver:class{observe(root){assert(root);observerCalls++;}},setInterval(){}};
  vm.createContext(context);vm.runInContext(adapter,context);
  assert(window.__lzxFlowTransport,'bridge must still register when document_start has no documentElement');
  context.document.documentElement={};domHooks.DOMContentLoaded();assert.equal(observerCalls,1);
  const built=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/injects/ui.js'),'utf8');
  const begin=built.indexOf('  async function init() {'),end=built.indexOf('  // 多时机激进挂载',begin);
  const scheduled=[];let paidRestarts=0;
  const startup={window:{__lzxFlowTransport:{},addEventListener(){}},document:{body:{appendChild(){}}},
    console:{log(){}},createUI(){},setupEvents(){},snapshotCurrentDomMedia(){},downloadedUrlsSet:new Set(),
    fabEl:null,overlayEl:null,state:{isRunning:false},bridgeUiReady:false,async loadState(){startup.state.isRunning=true;},
    batchPromise:null,showToast(){},startActivityCheck(){},startBatch(){paidRestarts++;},setInterval(){},setTimeout(fn){scheduled.push(fn);}};
  vm.createContext(startup);vm.runInContext(built.slice(begin,end),startup);await startup.init();scheduled.forEach(fn=>fn());
  assert.equal(paidRestarts,0,'restored isRunning must never start another paid batch');
  assert.equal(startup.state.isRunning,false);assert.equal(startup.bridgeUiReady,true);
  console.log('Flow cold startup: missing DOM root, independent bridge, idle-only bounded refresh, paid-work guards and no saved-batch restart passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
