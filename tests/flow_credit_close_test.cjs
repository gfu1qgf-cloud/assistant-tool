const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), crypto = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/background.js'), 'utf8');
const project = 'https://flow.google.com/project/fixture', batch = 'a'.repeat(32);
const binding = (reported = 'complete', extra = {}) => ({batch, taskId:'task', submissionId:'submission', mediaId:'media',
  download_path:`FlowBridge/${batch}/result.mp4`, reported, ...extra});
const command = () => ({action:'close_exhausted', request_id:'close', params:{idle:true, profile:'Profile 1',
  url:project, balance:9, until:Date.now()/1000 + 21600}});
async function fixture(options = {}) {
  const data = {lzxConfig:{token:'x'.repeat(40), port:18189, profile:'Profile 1', project}, lzxBatch:batch,
    lzxBatches:{[batch]:{state:'accepted'}}, lzxDownloads:{1:binding()}, ...options.data};
  const hooks = {}, messages = [], closed = [], sockets = [], windows = new Set(options.noWindows ? [] : [1,2]); let searches = 0;
  const event = name => ({addListener:fn => hooks[name] = fn});
  const tab = {id:7, windowId:1, url:project, active:true};
  const tabs = [tab, ...(options.extraTabs || [])]; let tabQueries=0;
  class Socket {static OPEN=1; constructor(){this.readyState=1;sockets.push(this);} send(value){messages.push(JSON.parse(value));} close(){this.readyState=3;}}
  const chrome = {runtime:{id:'pnjppjjfgiccpcfajkpooklgohgihdom', getManifest:()=>({version:'1.1.19'}),
      onMessage:event('message'), onStartup:event('startup'), onInstalled:event('installed')},
    storage:{onChanged:event('changed'), local:{get:async keys=>Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,structuredClone(data[k])])),
      set:async value=>Object.assign(data, structuredClone(value)), remove:async key=>delete data[key]}},
    tabs:{query:async()=>options.tabsAt?.(++tabQueries, tabs) || tabs, get:async()=>({...tab,url:options.changedProject ? project+'-changed':project}),
      sendMessage:async id => options.health ? options.health(id) : {healthy:true, running:false, native_busy:false}},
    downloads:{onChanged:event('download'), search:async query=>query.state ?
      (options.progress?.(++searches) ? [{id:99,state:'in_progress'}] : []) : []},
    windows:{onRemoved:event('windowRemoved'), getAll:async()=>{options.onWindows?.(hooks); return [...windows].map(id=>({id}));},
      remove:async id=>{if(options.removeFailure) throw Error('window unavailable'); closed.push(id);windows.delete(id);}},
    alarms:{onAlarm:event('alarm')}, action:{onClicked:event('action')}};
  const context={chrome, WebSocket:Socket, URL, URLSearchParams, crypto, Date, console,
    setTimeout:()=>1, clearTimeout(){}, setInterval:()=>1, clearInterval(){}};
  vm.createContext(context);
  vm.runInContext(source.replace("import './background.js';", '') + '\nglobalThis.api={receive,connect,setBusy:()=>commandsInFlight=1};', context);
  await new Promise(resolve=>setImmediate(resolve));
  return {api:context.api, data, messages, closed, sockets, hooks, windows};
}
async function refused(options, mutate) {
  const f = await fixture(options); const message=command(); mutate?.(message);
  await f.api.receive(message);
  assert.equal(f.closed.length, 0);
  assert.equal(f.messages.at(-1)?.success, false);
  return f;
}
(async()=>{
  const done=await fixture(); await done.api.receive(command());
  assert.deepEqual(done.closed,[1,2]); assert.equal(done.messages.at(-1).success,true);
  assert.equal(done.messages.at(-1).data.closed,true);
  await done.api.connect();await done.sockets.at(-1).onopen?.();
  assert.equal(done.messages.at(-1).type,'window_absence','a closed background reports absence without registering another occupied browser');
  const empty=await fixture({noWindows:true});await empty.api.connect();assert.equal(empty.sockets.length,0);
  const remembered=await fixture({noWindows:true,data:{lzxBrowserId:'known-browser'}});
  await remembered.sockets.at(-1).onopen();
  assert.equal(remembered.messages.at(-1).type,'window_absence');
  assert.equal(remembered.messages.at(-1).browser_id,'known-browser');
  await remembered.api.connect();assert.equal(remembered.sockets.length,1,'absence reporting is rate limited');
  const legacy=await fixture({data:{lzxBatches:{[batch]:Date.now()}}});
  await legacy.api.receive(command());assert.deepEqual(legacy.closed,[1,2],'old timestamp claims need exact completed downloads, not a guessed accepted state');
  await refused({data:{lzxBatches:{[batch]:Date.now()},lzxDownloads:{1:binding('interrupted')}}});
  for (const state of ['sent','unknown']) await refused({data:{lzxBatches:{[batch]:{state}}}});
  await refused({data:{lzxBatches:{}}});
  await refused({data:{lzxDownloads:{1:binding('interrupted')}}});
  await refused({data:{lzxOutbox:{pending:{type:'download',data:{state:'complete'}}}}});
  await refused({progress:()=>true});
  await refused({progress:count=>count===2});
  await refused({changedProject:true});
  await refused({health:()=>undefined});
  await refused({health:()=>({healthy:true})});
  await refused({health:()=>({healthy:false,running:false})});
  await refused({health:()=>({healthy:true,running:false,native_busy:true})});
  await refused({health:()=>({healthy:true,running:false})});
  await refused({extraTabs:[{id:8,url:project+'-second'}],health:id=>({healthy:true,running:id===8})});
  await refused({data:{lzxBatches:{[batch]:{state:'accepted'},['b'.repeat(32)]:{state:'accepted'}}}});
  for(const args of [{idle:false},{profile:'Other'},{url:project+'-other'},{balance:10},{balance:-1},{until:1}]) {
    await refused({},message=>Object.assign(message.params,args));
  }
  const changed=await fixture({onWindows:hooks=>hooks.changed({lzxConfig:{oldValue:{},newValue:{project:project+'-changed'}}},'local')});
  await changed.api.receive(command());assert.equal(changed.closed.length,0,'a retired connection must never close a changed profile');
  const retry=await fixture({data:{lzxDownloads:{1:binding('interrupted'),2:binding('complete')}}});
  await retry.api.receive(command()); assert.deepEqual(retry.closed,[1,2],'exact-result successful retry settles the old interruption');
  await refused({data:{lzxDownloads:{1:binding('interrupted'),2:binding('complete',{mediaId:'other'})}}});
  const busy=await fixture(); busy.api.setBusy(); await busy.api.receive(command());
  assert.equal(busy.closed.length,0);assert.equal(busy.messages.at(-1).code,'FLOW_CLOSE_BUSY');
  const failure=await fixture({removeFailure:true}); await failure.api.receive(command());
  assert.equal(failure.messages.at(-2).data.closing,true,'close intent must precede the native operation');
  assert.equal(failure.messages.at(-1).success,false);
  await failure.api.receive(command());
  assert.notEqual(failure.messages.at(-1).code,'FLOW_CLOSE_BUSY','a failed native close must release its operation lock');
  const backupCommand=()=>({...command(),params:{idle:true,profile:'Profile 1',url:project,
    reason:'high_credit_backup',account:'rich@example.test'}});
  const backup=await fixture();await backup.api.receive(backupCommand());
  assert.deepEqual(backup.closed,[1,2],'an explicitly skipped rich backup can close without a forged low balance');
  for(const options of [{progress:()=>true},{data:{lzxDownloads:{1:binding('interrupted')}}},
      {health:()=>({healthy:true,running:true,native_busy:false})},{removeFailure:true}]) {
    const busyBackup=await fixture(options);await busyBackup.api.receive(backupCommand());
    assert.equal(busyBackup.messages.at(-1).success,false,'rich backups retain every download/native guard');
  }
  const inventory=await fixture();await inventory.api.receive({type:'welcome'});
  await inventory.hooks.windowRemoved();
  assert.equal(inventory.messages.some(m=>m.type==='windows_closed'),false,'a partial close is not a vacancy');
  inventory.windows.clear();await inventory.hooks.windowRemoved();
  assert.equal(inventory.messages.at(-1).type,'windows_closed');
  const removedBatch='b'.repeat(32);
  const retired=await fixture({data:{lzxBatches:{[batch]:{state:'accepted'},[removedBatch]:{state:'accepted'}}}});
  const removalProof=command();removalProof.params.released_batches=[removedBatch];
  await retired.api.receive(removalProof);
  assert.deepEqual(retired.closed,[1,2],'a host-confirmed removed task must not hold a window forever');
  assert.equal(retired.data.lzxBatches[removedBatch].state,'accepted','keep durable replay guards and task history');
  const parked=await fixture({data:{lzxBatches:{[batch]:{state:'accepted'},[removedBatch]:{state:'accepted',packed:true}},
    lzxDownloads:{1:binding(),2:binding('interrupted',{batch:removedBatch,download_path:`FlowBridge/${removedBatch}/result.mp4`})}}});
  await parked.api.receive(removalProof);
  assert.deepEqual(parked.closed,[1,2],'host-confirmed cloud-complete parked downloads can release their old pack');
  const personalParked=await fixture({extraTabs:[{id:8,url:'https://mail.google.com/'}],
    data:{lzxBatches:{[batch]:{state:'accepted'},[removedBatch]:{state:'accepted',packed:true}}}});
  await personalParked.api.receive(removalProof);assert.equal(personalParked.closed.length,0,'parked receipts never authorize closing personal tabs');
  const unknown=await fixture({data:{lzxBatches:{[batch]:{state:'accepted'},[removedBatch]:{state:'accepted'},['c'.repeat(32)]:{state:'accepted'}}}});
  await unknown.api.receive(removalProof);assert.equal(unknown.closed.length,0,'an unrelated unknown paid batch stays protected');
  const downloading=await fixture({progress:()=>true});await downloading.api.receive(removalProof);
  assert.equal(downloading.closed.length,0,'removed task proofs cannot bypass live Chrome downloads');
  for(const invalid of [{},'not-an-array',['invalid-id'],[null]]) {
    await refused({},message=>message.params.released_batches=invalid);
  }
  const manualCommand=()=>({action:'close_browser',request_id:'manual-close',params:{idle:true,
    profile:'Profile 1',url:project,reason:'manual_runtime'}});
  const manual=await fixture();
  assert.equal(manual.closed.length,0,'startup alone never closes a browser');
  await manual.api.receive(manualCommand());
  assert.deepEqual(manual.closed,[1,2],'manual close shuts all windows of this Chrome profile without credit requirements');
  assert.equal(manual.messages.at(-1).action,'close_browser_response');
  assert.equal(manual.messages.at(-1).data.closed,true);
  const manualEmpty=await fixture({data:{lzxBatch:null,lzxBatches:{},lzxDownloads:{}}});
  await manualEmpty.api.receive(manualCommand());assert.deepEqual(manualEmpty.closed,[1,2]);
  for(const options of [{progress:()=>true},{data:{lzxDownloads:{1:binding('interrupted')}}},
      {data:{lzxOutbox:{pending:{type:'download'}}}}, {changedProject:true},
      {health:()=>undefined},{health:()=>({healthy:true,running:true,native_busy:false})},
      {health:()=>({healthy:true,running:false,native_busy:true})},
      {extraTabs:[{id:8,url:project+'-second'}],health:id=>({healthy:true,running:id===8,native_busy:false})},
      {data:{lzxBatches:{[batch]:{state:'unknown'}}}}]) {
    const blocked=await fixture(options);await blocked.api.receive(manualCommand());
    assert.equal(blocked.closed.length,0);assert.equal(blocked.messages.at(-1).success,false);
  }
  for(const params of [{profile:'Other'},{url:project+'-other'},{idle:false},{reason:'high_credit_backup'}]) {
    const invalid=await fixture();const message=manualCommand();Object.assign(message.params,params);
    await invalid.api.receive(message);assert.equal(invalid.closed.length,0);
    assert.equal(invalid.messages.at(-1).success,false);
  }
  const working=await fixture();working.api.setBusy();await working.api.receive(manualCommand());
  assert.equal(working.closed.length,0);assert.equal(working.messages.at(-1).code,'FLOW_CLOSE_BUSY');
  const failedManual=await fixture({removeFailure:true});await failedManual.api.receive(manualCommand());
  assert.equal(failedManual.messages.at(-1).success,false);
  await failedManual.api.receive(manualCommand());
  assert.notEqual(failedManual.messages.at(-1).code,'FLOW_CLOSE_BUSY','manual native failure releases its lock');
  const autoLow=await fixture();const lowMessage=manualCommand();Object.assign(lowMessage.params,{reason:'auto_low_credit',balance:5,until:Date.now()/1000+60});
  await autoLow.api.receive(lowMessage);assert.deepEqual(autoLow.closed,[1,2]);
  const fault=await fixture({health:()=>({healthy:false,running:false,native_busy:false})});
  const faultMessage=manualCommand();faultMessage.params.reason='auto_page_fault';
  await fault.api.receive(faultMessage);assert.deepEqual(fault.closed,[1,2],'a known idle but unhealthy page can close for one restart');
  const unknownIdle=await fixture({health:()=>undefined});await unknownIdle.api.receive(faultMessage);
  assert.equal(unknownIdle.closed.length,0,'missing idle evidence cannot close an unknown page');
  const busyFault=await fixture({health:()=>({healthy:false,running:true,native_busy:false})});await busyFault.api.receive(faultMessage);
  assert.equal(busyFault.closed.length,0,'a page fault cannot close a running generation');
  for (const message of [command(), manualCommand(), lowMessage, faultMessage, backupCommand()]) {
    for (const other of [{id:8,url:'https://mail.google.com/'}, {id:8,url:project+'-other'},
        {id:8,url:project,pendingUrl:'https://example.com/'}, {id:8,url:'chrome://settings/'},
        {id:8}, {id:8,url:'chrome://newtab/'}]) {
      const personal=await fixture({extraTabs:[other]});await personal.api.receive(message);
      assert.equal(personal.closed.length,0,'manual, batch and automatic close must protect every non-project tab');
      assert.equal(personal.messages.at(-1).code,'FLOW_CLOSE_BUSY');
      assert.match(personal.messages.at(-1).error,/非本项目标签页/);
      assert.equal(personal.messages.some(m=>m.data?.closing),false,'personal-tab refusal cannot report close intent');
    }
    let checked=false;
    const late=await fixture({health:()=>{checked=true;return {healthy:true,running:false,native_busy:false};},
      tabsAt:(count,tabs)=>checked ? [...tabs,{id:8,url:'https://example.com/'}]:tabs});
    await late.api.receive(message);assert.equal(late.closed.length,0,'recheck personal tabs after asynchronous health/download checks');
    assert.match(late.messages.at(-1).error,/非本项目标签页/);
  }
  const editor=await fixture({extraTabs:[{id:8,windowId:2,url:project+'/edit/video'}]});
  await editor.api.receive(manualCommand());assert.deepEqual(editor.closed,[1,2],'a same-project video editor is still a managed tab');
  let checkedProject=false;
  const lateProject=await fixture({health:()=>{checkedProject=true;return {healthy:true,running:false,native_busy:false};},
    tabsAt:(count,tabs)=>checkedProject ? [...tabs,{id:8,url:project+'/edit/video'}]:tabs});
  await lateProject.api.receive(manualCommand());assert.equal(lateProject.closed.length,0,'new project tabs also need a fresh idle health check');
  let personalOpen=true;
  const formerlyBusy=await fixture({tabsAt:(count,tabs)=>personalOpen ? [...tabs,{id:8,url:'https://example.com/'}]:tabs});
  await formerlyBusy.api.receive(manualCommand());assert.equal(formerlyBusy.closed.length,0);
  personalOpen=false;
  await formerlyBusy.api.receive(manualCommand());assert.deepEqual(formerlyBusy.closed,[1,2],'refusal releases the guard so closing personal tabs permits a later explicit close');
  console.log('Flow low-credit close: manual/automatic reasons, idle unhealthy recovery, profile binding, exact receipts, downloads and active-operation protection passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
