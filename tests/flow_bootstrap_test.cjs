const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const project='https://flow.google.com/project/cold',extension='pnjppjjfgiccpcfajkpooklgohgihdom';
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
async function fixture({running=false,healthy=false,changed=false,pending=false,nativeMissing=false}={}) {
  const data={lzxConfig:{token:'x'.repeat(40),port:18189,profile:'Profile 1',project}};
  const hooks={},injections=[],reloads=[],commands=[],errors=[],sent=[];let installed=false;
  const event=name=>({addListener:fn=>hooks[name]=fn});
  const tab={id:7,windowId:1,url:project,active:true,...(pending?{pendingUrl:project}: {})};
  class Socket {static OPEN=1;constructor(){this.readyState=1;}send(value){sent.push(JSON.parse(value));}close(){}}
  const chrome={runtime:{id:extension,getManifest:()=>({version:'1.1.21'}),onMessage:event('message'),onStartup:event('startup'),onInstalled:event('installed')},
    storage:{onChanged:event('changed'),local:{get:async keys=>Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,structuredClone(data[k])])),
      set:async value=>Object.assign(data,structuredClone(value)),remove:async key=>delete data[key]}},
    tabs:{query:async()=>[tab,{id:8,url:'https://flow.google.com/project/other'}],get:async()=>({...tab}),onUpdated:event('updated'),onCreated:event('created'),
      sendMessage:async(id,message)=>{commands.push(message);assert.equal(id,7);
        if(message.action==='lzxPageHealth') return installed||healthy?{healthy:true,running}:nativeMissing?{healthy:false,native_bridge:false}:undefined;
        if(!installed&&!healthy) throw Error('Receiving end does not exist');return {accepted:true};},reload:async id=>reloads.push(id)},
    scripting:{executeScript:async request=>{injections.push(request);
      if(request.func) return [{frameId:0,documentId:'doc-original',result:{url:changed?'https://flow.google.com/project/other':project}}];
      installed=true;return [{frameId:0,documentId:'doc-original'}];},insertCSS:async request=>injections.push(request)},
    downloads:{onChanged:event('download'),search:async()=>[]},alarms:{create(){},onAlarm:event('alarm')},action:{onClicked:event('action')}};
  const context={chrome,WebSocket:Socket,URL,URLSearchParams,crypto,Date,console:{log(){},warn(){},error:(...v)=>errors.push(v)},
    setTimeout:fn=>{queueMicrotask(fn);return 1;},clearTimeout(){},setInterval:()=>1,clearInterval(){}};
  vm.createContext(context);vm.runInContext(source.replace("import './background.js';",'')+'\nglobalThis.api={bootstrapProject,receive,forward};',context);
  await new Promise(resolve=>setImmediate(resolve));
  return {api:context.api,injections,reloads,commands,hooks,errors,data,sent};
}
(async()=>{
  const f=await fixture();await Promise.all([f.api.bootstrapProject(),f.api.bootstrapProject()]);
  assert.equal(f.reloads.length,0,'missing startup scripts must recover without navigation');
  const scripts=f.injections.filter(r=>r.files?.some(p=>p.endsWith('.js')));
  assert.equal(scripts.length,2,'one injection per world despite concurrent recovery');
  assert.deepEqual(Array.from(scripts[0].files),['content-isolated.js','lzx-content.js']);
  assert.deepEqual(Array.from(scripts[1].files),['lzx-reference.js','lzx-page.js','injects/flow-proxy.js','injects/ui.js']);
  for(const r of f.injections.filter(r=>r.files)){assert.equal(r.target.tabId,7);assert.deepEqual(Array.from(r.target.documentIds),['doc-original']);}
  await f.api.forward({action:'snapshot'});assert(f.commands.some(m=>m.message?.action==='snapshot'));
  await f.api.bootstrapProject();assert.equal(f.injections.filter(r=>r.files?.some(p=>p.endsWith('.js'))).length,2,'a working page is left running');
  const busy=await fixture({healthy:true,running:true});await busy.api.bootstrapProject();assert.equal(busy.injections.length,0);assert.equal(busy.reloads.length,0);
  const other=await fixture({changed:true});await assert.rejects(()=>other.api.bootstrapProject(),/项目/);assert(!other.injections.some(r=>r.files));
  const navigation=await fixture({pending:true});await navigation.api.bootstrapProject();assert.equal(navigation.injections.length,0,'wait for committed navigation before injecting');
  const startup=await fixture();await startup.api.receive({type:'welcome'});assert(startup.injections.some(r=>r.files));
  assert.equal(startup.reloads.length,0);assert(!startup.commands.some(m=>m.message?.action==='run_tasks'));
  const partial=await fixture({nativeMissing:true});await partial.api.bootstrapProject();
  assert(partial.injections.some(r=>r.files?.includes('content-isolated.js')),'a live desktop content receiver cannot hide a missing upstream native bridge');
  const failure={type:'generation_failed',submissionId:'sub',mediaId:'new-media',failure_source:'server_status',
    download_path:'FlowBridge/'+'a'.repeat(32)+'/result.mp4'};
  f.hooks.message({action:'lzxPageEvent',data:failure},{id:extension,frameId:0,tab:{id:7}});
  await new Promise(resolve=>setImmediate(resolve));
  const event=Object.values(f.data.lzxOutbox)[0];assert.equal(event.type,'generation_failed');
  assert.equal(event.batch_id,'a'.repeat(32));assert(event.event_id);
  await f.api.receive({type:'welcome'});assert(f.sent.filter(e=>e.event_id===event.event_id).length>=2,'a lost failure notification replays on reconnect');
  await f.api.receive({type:'ack',event_id:event.event_id});assert.equal(Object.keys(f.data.lzxOutbox).length,0);
  console.log('Flow bootstrap: missed startup scripts recover in the original document, exact project only, concurrent deduplication, healthy running-page preservation and no paid replay passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
