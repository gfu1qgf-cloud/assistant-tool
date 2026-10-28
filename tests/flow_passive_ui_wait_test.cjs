const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),crypto=require('node:crypto');
const source=fs.readFileSync(process.env.LZX_PAGE_SOURCE || path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8');
function fixture({deny=0,spinner=false,binding=false}={}) {
  let now=1000,requests=0;const events=[],hooks={};let receive;
  const window={postMessage(value){const event=value.data;events.push(event);
    if(event.type==='ui_mutex_command'&&!event.release)queueMicrotask(()=>receive({action:'ui_mutex_result',token:event.token,
      ok:++requests!==deny,state:requests===deny?'rejected':'granted',expires_at:now/1000+30}));},
    addEventListener(name,callback){hooks[name]=callback;}};window.top=window;
  const location={origin:'https://flow.google.com',href:'https://flow.google.com/project/ours'};
  const context={window,location,crypto,console,URL,Date:class extends Date{static now(){return now;}},
    setInterval(){},setTimeout(fn,ms){queueMicrotask(()=>{now+=ms;fn();});},MutationObserver:class{observe(){}},
    document:{documentElement:{},querySelector:s=>spinner&&s.startsWith('flow-generate-icon-button')?{}:null,querySelectorAll:()=>[]}};
  vm.createContext(context);vm.runInContext(source,context);
  receive=message=>hooks.message({source:window,origin:location.origin,data:{sender:'lzx-flow-extension',message}});
  const api=window.__lzxFlowTransport;
  api.attach(()=>{},()=>({tasks:[{mediaBindings:binding?[{submissionId:'ours',mediaIds:['exact']}]:[]}]}),{});
  return{api,events,receive,location,context,clock:()=>now,wait:async ms=>{now+=ms;},requests:()=>requests};
}
(async()=>{
  assert.equal(typeof fixture().api.uiWait,'function','a read-only wait must have an explicit lease-yield path');
  const f=fixture();await f.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  let drags=0,captures=0;
  await f.api.uiOperation('upload_images',async()=>{
    assert(f.api.uiMutexActive());drags++;
    await f.api.uiWait(async()=>{
      assert(!f.api.uiMutexActive(),'network observation is outside the lease');
      await f.wait(80000);
      await f.api.uiOperation('upload_images',async()=>{assert(f.api.uiMutexActive());captures++;});
      assert(!f.api.uiMutexActive(),'screenshot fallback returns to the passive wait');
    });
    assert(f.api.uiMutexActive(),'reacquire a new lease before selecting the material');f.api.assertUi();
  });
  assert.equal(drags,1);assert.equal(captures,1);assert.equal(f.requests(),3);
  const releases=f.events.filter(e=>e.type==='ui_mutex_command'&&e.release);
  assert.equal(releases.length,3);assert(releases[0].passive_wait&&releases[1].passive_wait);
  assert(!releases[2].passive_wait);assert.equal(new Set(releases.map(e=>e.token)).size,3);

  for(const kind of ['failed','moved','denied','disabled']) {
    const g=fixture({deny:kind==='denied'?2:0});await g.receive({action:'configure_browser_options',values:{ui_mutex:true}});
    const work=g.api.uiOperation('upload_images',async()=>{
      await g.api.uiWait(async()=>{
        if(kind==='failed')throw Error('cancelled before resume');
        if(kind==='moved')g.location.href='https://flow.google.com/project/other';
        if(kind==='disabled')await g.receive({action:'configure_browser_options',values:{ui_mutex:false}});
      });
    });
    if(kind==='disabled')await work;else await assert.rejects(work,/cancelled|页面已变化|互斥申请未完成/);
    assert(!g.api.uiMutexActive());assert.equal(g.requests(),kind==='denied'?2:1);
  }
  for(const bound of [false,true]) {
    const g=fixture({spinner:!bound,binding:bound});await g.receive({action:'configure_browser_options',values:{ui_mutex:true}});
    await g.api.uiOperation('submit',()=>g.api.afterSubmit({rowIndex:0,lineIndex:0,submissionId:'ours'}),'a'.repeat(32));
    const release=g.events.find(e=>e.type==='ui_mutex_command'&&e.release);
    assert.equal(release.passive_wait===true,!bound,'yield pending receipt; preserve fast confirmed-submit continuity');
  }
  const off=fixture();await off.api.uiOperation('upload_images',()=>off.api.uiWait(async()=>{}));
  assert(!off.events.some(e=>e.type==='ui_mutex_command'));
  const unready=fixture();await unready.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  await unready.receive({action:'prepare',request_id:'c'.repeat(32),params:{wait_for_ready:true}});
  assert(unready.events.some(e=>e.code==='FLOW_PAGE_NOT_READY'));
  assert(unready.events.some(e=>e.type==='ui_mutex_command'&&e.release&&e.passive_wait));
  assert.equal(unready.requests(),1,'controls readiness waits without a lease, timeout does not reacquire');
  console.log('PASS passive upload waits, exact-token regrant, screenshot lease, failure/cancel/navigation/disable and submit receipt continuity');
})().catch(e=>{console.error(e);process.exitCode=1;});
