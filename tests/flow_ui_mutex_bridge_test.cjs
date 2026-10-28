const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const start=source.indexOf('const foregroundReplies = new Map();'),end=source.indexOf('async function nativeForeground(',start);
assert(start>=0 && end>start);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const messages=[],focus=[],revoked=[],leases=new Map();
  const context={uiLeases:leases,browserOptions:{ui_mutex:true,focus_before_action:true},authenticated:true,connectionEpoch:1,
    config:{project:'https://flow.google.com/project/ours'},send:message=>messages.push(message),
    sameProjectTab:(tab,url)=>tab.url===url,timingMs:(_key,fallback)=>fallback*1000,setTimeout,clearTimeout,
    chrome:{tabs:{async get(){return {id:7,url:'https://flow.google.com/project/ours'};},async sendMessage(id,message){revoked.push(message);}}},
    async readyWorkTab(id,message){focus.push({id,message});return 'focused';}};
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);
  const token='a'.repeat(32),permit=context.acquireUiLease(7,'prepare',token);await flush();
  assert.equal(messages.at(-1).type,'ui_mutex_request');assert.equal(focus.length,0,'queued browsers must not activate early');
  await context.uiMutexResult({token,state:'granted',expires_at:Date.now()/1000+30});
  const result=await permit;assert(result.ok);assert.equal(focus.length,1);
  assert.equal(focus[0].message.action,'focus_window');assert.equal(focus[0].message.foreground_token,token);
  await context.uiMutexResult({token,state:'granted'});assert.equal(focus.length,1,'duplicate grants cannot reactivate or rerun UI');
  context.releaseUiLease(token);assert.equal(messages.at(-1).type,'ui_mutex_release');assert.equal(leases.size,0);

  context.browserOptions.focus_before_action=false;
  const quietToken='f'.repeat(32),quiet=context.acquireUiLease(7,'upload_images',quietToken);await flush();
  await context.uiMutexResult({token:quietToken,state:'granted'});
  assert.equal((await quiet).focus,'disabled');assert.equal(focus.length,1,'mutex must not bypass automatic-focus OFF');
  assert.equal(leases.size,1);context.releaseUiLease(quietToken);
  const manualToken='9'.repeat(32),manual=context.acquireUiLease(7,'focus_window',manualToken);await flush();
  await context.uiMutexResult({token:manualToken,state:'granted'});assert.equal((await manual).ok,true);
  assert.equal(focus.length,2,'explicit manual focus remains enabled');context.releaseUiLease(manualToken);
  const waiting=context.acquireUiLease(7,'upload_images','b'.repeat(32));await flush();
  context.cancelUiLeases('disabled');assert.equal((await waiting).state,'disabled');assert.equal(leases.size,0);
  const disconnected=context.acquireUiLease(7,'prepare','c'.repeat(32));await flush();
  context.cancelUiLeases('disconnected');assert.equal((await disconnected).ok,false);assert.equal(leases.size,0);
  const stale=context.acquireUiLease(7,'submit','d'.repeat(32),'e'.repeat(32));await flush();context.connectionEpoch++;
  await context.uiMutexResult({token:'d'.repeat(32),state:'granted'});assert.equal((await stale).ok,false);
  assert.equal(focus.length,2,'a retired project or connection cannot activate or submit after a delayed grant');
  console.log('UI mutex bridge: desktop-before-focus, exact lease token, duplicate grant, live disable, disconnect and stale epoch passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
