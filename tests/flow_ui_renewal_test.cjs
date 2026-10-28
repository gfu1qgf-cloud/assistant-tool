const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  let now=100000;const events=[],hooks={};
  const window={postMessage(value){events.push(value.data);},addEventListener(name,fn){hooks[name]=fn;}};window.top=window;
  const location={origin:'https://flow.google.com',href:'https://flow.google.com/project/ours'};
  const context={window,location,crypto,console,URL,setInterval(){},setTimeout,Date:class extends Date{static now(){return now;}},
    MutationObserver:class{observe(){}},document:{documentElement:{},querySelector(){return {};},querySelectorAll(){return [];}}};
  vm.createContext(context);vm.runInContext(source,context);
  const receive=message=>hooks.message({source:window,origin:location.origin,data:{sender:'lzx-flow-extension',message}});
  const api=window.__lzxFlowTransport;api.attach(()=>{},()=>({tasks:[],url:location.href}),{});
  return {api,events,receive,clock:value=>now=value};
}
(async()=>{
  const f=fixture();await f.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  let finish;const hold=new Promise(r=>finish=r);let completed=false;
  const task=f.api.uiOperation('submit',async()=>{
    await f.api.waitPrompt({textContent:'correct prompt'},'correct prompt');
    await hold;f.api.assertUi();completed=true;
  },'a'.repeat(32));
  await flush();const token=f.events.at(-1).token;
  assert(!f.events.some(e=>e.ui_renew),'waiting for a permit is not progress');
  await f.receive({action:'ui_mutex_result',token,ok:true,state:'granted',renewal:true,expires_at:130});await flush();
  const report=f.events.find(e=>e.ui_renew);assert(report);assert.equal(report.type,'snapshot');
  const renewal=report.ui_renew;assert.equal(renewal.step,'prompt-written');
  assert(!f.events.some(e=>e.release),'renewal keeps the current lease');
  // Ignored metadata produces no acknowledgement at all.
  f.api.assertUi();f.api.renewUiAfterSuccess('prompt-written');
  assert.equal(f.events.filter(e=>e.ui_renew).length,1,'polling the same success cannot renew repeatedly');
  // A missing optional reply neither stalls nor fails the normal operation.
  f.api.renewUiAfterSuccess('reference-mounted');finish();await task;assert(completed);
  assert(f.events.some(e=>e.release&&e.stopped===true));

  const legacy=fixture();await legacy.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  const original=legacy.api.uiOperation('submit',()=>legacy.api.waitPrompt({textContent:'yes'},'yes'),'b'.repeat(32));
  await flush();const old=legacy.events.at(-1).token;
  await legacy.receive({action:'ui_mutex_result',token:old,ok:true,state:'granted',expires_at:130});await original;
  assert(!legacy.events.some(e=>e.ui_renew),'old main programs continue without the optional field');

  const expiry=fixture();await expiry.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  let resume;const paused=new Promise(r=>resume=r);
  const expiring=expiry.api.uiOperation('prepare',async()=>{await paused;expiry.api.assertUi();});
  await flush();const expToken=expiry.events.at(-1).token;
  await expiry.receive({action:'ui_mutex_result',token:expToken,ok:true,state:'granted',renewal:true,expires_at:130});await flush();
  await expiry.receive({action:'ui_mutex_revoked',token:expToken,state:'expired',stopping:true});
  assert(!expiry.events.some(e=>e.release),'receiving revoke is not proof the operation has stopped');
  await expiry.receive({action:'ui_mutex_result',token:expToken,state:'granted',renew_seq:1,expires_at:999});
  const rejected=assert.rejects(expiring,/操作权限已结束/);resume();await rejected;
  assert(expiry.events.some(e=>e.release&&e.stopped===true));

  const late=fixture();await late.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  let resolve;const blocked=new Promise(r=>resolve=r);
  const work=late.api.uiOperation('prepare',async()=>{await blocked;late.api.assertUi();});await flush();
  const lateToken=late.events.at(-1).token;
  await late.receive({action:'ui_mutex_result',token:lateToken,ok:true,state:'granted',renewal:true,expires_at:130});await flush();
  late.clock(131000);await late.receive({action:'ui_mutex_result',token:lateToken,state:'granted',renew_seq:1,expires_at:999});
  const neverRevive=assert.rejects(work,/操作权限已结束/);resolve();await neverRevive;
  console.log('Optional renewal: success only, no wait/duplicate renewal, rejection and lost reply preserve flow, legacy fallback, stop receipt and late-response fence passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
