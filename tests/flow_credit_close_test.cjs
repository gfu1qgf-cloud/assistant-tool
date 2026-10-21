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
  const tabs = [tab, ...(options.extraTabs || [])];
  class Socket {static OPEN=1; constructor(){this.readyState=1;sockets.push(this);} send(value){messages.push(JSON.parse(value));} close(){this.readyState=3;}}
  const chrome = {runtime:{id:'pnjppjjfgiccpcfajkpooklgohgihdom', getManifest:()=>({version:'1.1.19'}),
      onMessage:event('message'), onStartup:event('startup'), onInstalled:event('installed')},
    storage:{onChanged:event('changed'), local:{get:async keys=>Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,structuredClone(data[k])])),
      set:async value=>Object.assign(data, structuredClone(value)), remove:async key=>delete data[key]}},
    tabs:{query:async()=>tabs, get:async()=>({...tab,url:options.changedProject ? project+'-changed':project}),
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
  console.log('Flow low-credit close: profile binding, idle pages, all batch receipts, exact-result retry, download acknowledgement, in-flight operations and failure recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
