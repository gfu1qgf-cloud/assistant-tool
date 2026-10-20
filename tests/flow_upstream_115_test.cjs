const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const root=path.join(__dirname,'..'),read=name=>fs.readFileSync(path.join(root,name),'utf8');
const quiet={log(){},warn(){},error(){}};

function page(managed) {
  const source=read('browser_extensions/flow_bridge/injects/ui.js');
  const bridge=source.slice(source.indexOf('  (function initAccountBridge() {'),source.lastIndexOf('\n})();'));
  const timers=[],intervals=[],sockets=[],native=[],storage=[],attachments=[];let dom=0;
  const window={}; if(managed)window.__lzxFlowTransport={attach:(...args)=>attachments.push(args)};
  class Socket {static OPEN=1;constructor(url){this.url=url;this.readyState=0;sockets.push(this);}send(){}close(){this.readyState=3;}}
  const context={window,location:{hostname:'flow.google.com',href:'https://flow.google.com/project/fixture'},console:quiet,WebSocket:Socket,
    document:{title:'Google Flow',querySelector(){dom++;return null;},querySelectorAll(){dom++;return[];}},
    localStorage:{getItem(key){storage.push(key);return'2';},setItem(key){storage.push(key);}},
    state:{isRunning:false,tasks:[]},batchPromise:null,bridgeUiReady:false,syncPageVideoSettings(){},
    uploadPathsToFlow(){throw Error('account bridge cannot upload');},getLoadedPageImages:()=>[],simulateClick(){throw Error('account bridge cannot click');},
    sendToExtension(action){native.push(action);return new Promise(()=>{});},
    setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},clearTimeout(){},
    setInterval(fn,ms){intervals.push({fn,ms});return intervals.length;},clearInterval(){}};
  vm.createContext(context);vm.runInContext(bridge,context);
  return {context,timers,intervals,sockets,native,storage,attachments,dom:()=>dom};
}

async function unpairedBackground() {
  const data={},writes=[],sockets=[],hooks={};
  const event=name=>({addListener:fn=>hooks[name]=fn});
  class Socket{constructor(){this.readyState=0;sockets.push(this);}}
  const chrome={runtime:{id:'pnjppjjfgiccpcfajkpooklgohgihdom',getManifest:()=>({version:'1.1.22'}),onStartup:event('startup'),onInstalled:event('installed'),onMessage:event('message')},
    storage:{local:{get:async keys=>Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,structuredClone(data[k])])),
      set:async value=>{writes.push(structuredClone(value));Object.assign(data,structuredClone(value));}},onChanged:event('storage')},
    alarms:{onAlarm:event('alarm')},downloads:{onChanged:event('download')},action:{onClicked:event('action')}};
  const context={chrome,WebSocket:Socket,crypto,URL,Date,console:quiet,setTimeout(){},setInterval(){},clearInterval(){}};
  vm.createContext(context);vm.runInContext(read('browser_extensions/flow_adapter/background.js').replace("import './background.js';",'')+
    '\nglobalThis.connectBridge=connect;',context);
  await new Promise(resolve=>setImmediate(resolve));
  await context.connectBridge();await context.connectBridge();
  assert.equal(sockets.length,0,'unconfigured manual profiles never probe a guessed desktop port');
  assert.equal(writes.length,0,'unconfigured manual profiles do not allocate or rewrite browser identity');
  data.lzxConfig={token:'x'.repeat(40),port:18189,profile:'Profile 1',project:'https://flow.google.com/project/fixture'};
  await context.connectBridge();
  assert.equal(sockets.length,1);assert.equal(writes.filter(value=>value.lzxBrowserId).length,1);
  sockets[0].readyState=3;sockets[0].onclose();
  hooks.storage({lzxConfig:{oldValue:{port:0},newValue:data.lzxConfig}},'local');
  await context.connectBridge();
  assert.equal(writes.filter(value=>value.lzxBrowserId).length,1,'configured reconnects reuse the saved identity without repeated writes');
}

(async()=>{
  const managed=page(true);
  assert.equal(managed.attachments.length,1,'managed transport attaches immediately without waiting for a legacy identity response');
  assert.equal(managed.native.length,0);assert.equal(managed.storage.length,0);assert.equal(managed.dom(),0);
  assert.equal(managed.sockets.length,0);assert.equal(managed.intervals.length,0);
  const manual=page(false);
  assert.equal(manual.attachments.length,0);assert.equal(manual.native.length,0);assert.equal(manual.dom(),0);
  assert.equal(manual.intervals.length,0);assert.equal(manual.context.document.title,'Google Flow');
  const start=manual.timers.find(timer=>timer.ms===3000);assert(start,'preserve upstream dormant startup');start.fn();
  assert.equal(manual.sockets.length,1);assert.equal(manual.sockets[0].url,'ws://[::1]:18188');
  assert.equal(manual.native.length,0);assert.equal(manual.dom(),0);
  manual.sockets[0].onclose();
  assert(manual.timers.some(timer=>timer.ms===5000),'closed desktop uses the upstream lightweight five-second retry');
  await unpairedBackground();
  console.log('FlowCue 1.1.5: dormant manual mode, no idle identity/DOM/title polling, early managed transport and stable saved bridge identity passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
