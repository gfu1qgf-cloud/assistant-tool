const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'..');
const ui=fs.readFileSync(path.join(root,'browser_extensions/flow_bridge/injects/ui.js'),'utf8');
const current=fs.readFileSync(path.join(root,'browser_extensions/flow_bridge/content-isolated.js'),'utf8');
const legacy=fs.readFileSync(path.join(root,'third_party/flowcue/extension/content-isolated.js'),'utf8');
const mainCode=ui.slice(ui.indexOf('  const extensionCallbacks ='),ui.indexOf('  const downloadedUrlsSet ='))+
  ui.slice(ui.indexOf('  async function saveState()'),ui.indexOf('  // 从 Storage 读取配置和状态'));
const quiet={log(){},warn(){},error(){}};
async function fixture(kinds) {
  const listeners=[],handles=[],toasts=[],acks=[],worlds=[];let writes=0,retired=0;
  const schedule=(fn,ms)=>{const timer=setTimeout(fn,ms===1500?20:ms>=15000?200:ms);handles.push(timer);return timer;};
  const newWindow=()=>{const window={};window.top=window;
    window.addEventListener=(name,callback)=>{if(name==='message')listeners.push({window,callback});};
    window.removeEventListener=(_name,callback)=>{const index=listeners.findIndex(item=>item.callback===callback);if(index>=0){listeners.splice(index,1);retired++;}};
    window.postMessage=data=>queueMicrotask(()=>{if(data.sender==='flow-isolated-ack')acks.push(data);
      for(const item of [...listeners])item.callback({source:item.window,data});});return window;};
  const main={window:newWindow(),console:quiet,setTimeout:schedule,clearTimeout,Date,
    state:{tasks:[],settings:{},presets:[],taskRecord:{}},downloadedUrlsSet:new Set(),getStorageKey:()=> 'project',showToast:text=>toasts.push(text)};
  vm.createContext(main);vm.runInContext(mainCode,main);
  for(const kind of kinds) {
    const window=newWindow(),bad=kind!=='healthy';worlds.push(window);
    const chrome={runtime:{getManifest(){if(bad)throw Error('Extension context invalidated.');return {version:'1.1.45'};},
      sendMessage(payload,callback){if(bad)throw Error('Extension context invalidated.');writes++;setImmediate(()=>callback({success:true}));}}};
    let isolated=(kind==='legacy-orphan'?legacy:current).split('// 监听来自后台 Service Worker')[0];
    const context={window,chrome,console:quiet,setTimeout:schedule};vm.createContext(context);vm.runInContext(isolated+'\n})();',context);
  }
  return {main,toasts,acks,worlds,stats:()=>({writes,retired}),async save(){await main.saveState();await new Promise(resolve=>setImmediate(resolve));},close(){handles.forEach(clearTimeout);}};
}
(async()=>{
  const sender=current.slice(current.indexOf('function sendMessageToBackground('),current.indexOf('// 已处理的 callbackId'));
  const context={chrome:{runtime:{lastError:{message:'Extension context invalidated'},sendMessage(_payload,callback){callback();}}},
    console:quiet,setTimeout:fn=>fn()};
  vm.createContext(context);vm.runInContext(sender,context);
  assert.equal((await context.sendMessageToBackground({action:'nativeFileDrag'})).dispatched,false,
    'a first request rejected by an invalid context never reached the native uploader');
  context.chrome.runtime.lastError={message:'message port closed'};
  assert.notEqual((await context.sendMessageToBackground({action:'nativeFileDrag'})).dispatched,false,
    'a closed response port cannot prove whether the native drop already occurred');
  const orphan=await fixture(['orphan']);try {
    assert.equal((await orphan.main.sendToExtension('nativeFileDrag',{})).dispatched,false);
    assert.equal(orphan.stats().writes,0);
  }finally{orphan.close();}
  for(const kinds of [['healthy'],['legacy-orphan','healthy'],['orphan','healthy']]) {
    const f=await fixture(kinds);try {await f.save();assert.equal(f.stats().writes,1);assert.equal(f.toasts.length,0,'a stale failure cannot override a successful live bridge');
      if(kinds[0]==='orphan') {assert.equal(f.stats().retired,1);assert.equal(f.worlds[0].__lzxFlowNativeBridge,false);assert.equal(f.acks.length,1,'a dead bridge cannot acknowledge side effects');}}
    finally{f.close();}
  }
  const failed=await fixture(['legacy-orphan']);try {await failed.save();await failed.save();await failed.save();assert.equal(failed.toasts.length,1,'one continuous failure cannot flood repeated refresh toasts');}finally{failed.close();}
  console.log('Bridge retirement: healthy writes beat old invalidation, dead listeners retire before ACK, and genuine repeated failures produce one bounded warning.');
})().catch(error=>{console.error(error);process.exitCode=1;});
