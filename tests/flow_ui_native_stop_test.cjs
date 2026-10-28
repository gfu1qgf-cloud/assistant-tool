// Run the shipped native drag function with a deferred CDP input command.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const bridge=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const native=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/background.js'),'utf8');
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
  const leases=new Map(),messages=[],inputs=[];let finishDrag;
  const token='a'.repeat(32),entry={held:true,renewal:true,epoch:1,tabId:7,expires_at:Date.now()/1000+60};leases.set(token,entry);
  const c={uiLeases:leases,browserOptions:{ui_mutex:true},authenticated:true,connectionEpoch:1,
    send:m=>messages.push(m),setTimeout(fn,ms){if(ms===120)return setTimeout(fn,1);return setTimeout(fn,ms);},clearTimeout,console,
    serialByTab:(_id,work)=>Promise.resolve().then(work),recentTabDrags:new Map(),DRAG_DEDUP_WINDOW_MS:100,
    chrome:{tabs:{async sendMessage(){}},debugger:{async attach(){},async detach(){inputs.push('detach');},
      async sendCommand(_target,method,args){
        if(method==='Input.dispatchDragEvent') {inputs.push(args.type);if(args.type==='dragEnter')await new Promise(r=>finishDrag=r);}
      }}}};
  vm.createContext(c);vm.runInContext(bridge.slice(bridge.indexOf('const foregroundReplies = new Map();'),bridge.indexOf('async function nativeForeground(')),c);
  vm.runInContext(native.slice(native.indexOf('async function dispatchNativeFileDrag('),native.indexOf('async function dispatchNativeClick(')),c);
  const work=c.dispatchNativeFileDrag(7,['C:/synthetic/image.png'],10,20,token);await flush();
  assert.deepEqual(inputs,['dragEnter']);assert.equal(entry.nativeActive,1);
  c.revokeUiLease(token);c.releaseUiLease(token,false,true);
  assert(leases.has(token),'page callback timeout cannot free a native command still in flight');
  const stopped=assert.rejects(work,e=>/权限已结束/.test(e.message)&&e.uploadDispatched===false);
  finishDrag();await stopped;
  assert.deepEqual(inputs,['dragEnter','detach'],'no dragOver/drop can execute after the old lease is revoked');
  assert.equal(messages.at(-1).type,'ui_mutex_release');assert(messages.at(-1).stopped);assert(!leases.has(token));
  const replacement='b'.repeat(32);leases.set(replacement,{...entry,revoking:false,releasing:false,nativeActive:0});
  await assert.rejects(c.dispatchNativeFileDrag(7,['C:/synthetic/late.png'],10,20,token),/权限已结束/);
  assert.deepEqual(inputs,['dragEnter','detach'],'a delayed old-token drag cannot run under a later lease');
  console.log('Native stop: in-flight CDP completion, stop-before-handoff, no delayed drop, and exact-token fencing passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
