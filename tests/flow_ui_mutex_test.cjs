const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const events=[],hooks={};
  const window={postMessage(value){events.push(value.data);},addEventListener(name,callback){hooks[name]=callback;}};window.top=window;
  const location={origin:'https://flow.google.com',href:'https://flow.google.com/project/ours'};
  const context={window,location,crypto,console,setInterval(){},setTimeout,MutationObserver:class{observe(){}},
    document:{documentElement:{},querySelector(){return null;},querySelectorAll(){return [];}}};
  vm.createContext(context);vm.runInContext(source,context);
  const receive=message=>hooks.message({source:window,origin:location.origin,data:{sender:'lzx-flow-extension',message}});
  return {api:window.__lzxFlowTransport,events,receive};
}
(async()=>{
  const f=fixture();let operations=0;
  await f.api.uiOperation('prepare',async()=>operations++);
  assert.equal(operations,1);assert.equal(f.events.length,0,'off preserves ordinary parallel page behavior');
  await f.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  let finish;const hold=new Promise(resolve=>finish=resolve);
  const work=f.api.uiOperation('submit',async()=>{operations++;assert(f.api.uiMutexActive());await hold;},'a'.repeat(32));
  await flush();assert.equal(operations,1,'UI cannot run before the desktop grants the lease');
  const request=f.events.at(-1);assert.equal(request.operation,'submit');assert.equal(request.batch_id,'a'.repeat(32));
  await f.receive({action:'ui_mutex_result',token:request.token,ok:true,state:'granted',expires_at:Date.now()/1000+30});
  await flush();assert.equal(operations,2);assert(!f.events.some(e=>e.release),'keep lease during actual page manipulation');
  finish();await work;assert(f.events.some(e=>e.release && e.token===request.token));
  assert(!f.api.uiMutexActive(),'release before cloud generation or downloads wait');

  const failed=f.api.uiOperation('upload_images',async()=>{throw Error('upload failed');});
  await flush();const failedRequest=f.events.at(-1);
  const rejected=assert.rejects(failed,/upload failed/);
  await f.receive({action:'ui_mutex_result',token:failedRequest.token,ok:true,state:'granted',expires_at:Date.now()/1000+30});
  await rejected;assert(f.events.some(e=>e.release && e.token===failedRequest.token),'exceptions release the lease');

  const waiting=f.api.uiOperation('prepare',async()=>operations++);await flush();const waitingToken=f.events.at(-1).token;
  await f.receive({action:'ui_mutex_revoked',token:waitingToken,state:'disabled'});await waiting;
  assert.equal(operations,3,'turning off while queued immediately restores normal operation');

  await f.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  let revoke;const block=new Promise(resolve=>revoke=resolve);
  const revoked=f.api.uiOperation('prepare',async()=>{await block;f.api.assertUi();});await flush();const active=f.events.at(-1);
  const cancelled=assert.rejects(revoked,/操作权限已结束/);
  await f.receive({action:'ui_mutex_result',token:active.token,ok:true,state:'granted',expires_at:Date.now()/1000+30});await flush();
  await f.receive({action:'ui_mutex_revoked',token:active.token,state:'expired'});revoke();await cancelled;
  assert(f.events.some(e=>e.release && e.token===active.token));
  console.log('UI mutex page phases: off, queued, exact submit scope, success/failure release, live disable and revoked-lease guards passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
