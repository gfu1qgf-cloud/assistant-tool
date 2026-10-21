const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const crypto = require('node:crypto');
const project = 'https://flow.google.com/project/ours';
const source = fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8')
  .replace("import './background.js';",'');

async function fixture({active=false, state='minimized', pendingUrl, focused=false, focusFailure=false, storageFailure=false}={}) {
  const data={lzxConfig:{project,token:'test',port:18189,profile:'Profile 7'},lzxBatch:'a'.repeat(32)};
  const tab={id:7,windowId:42,url:project,active,pendingUrl};
  const sent=[],updates=[],windows=[],forwarded=[],hooks={};
  const event=name=>({addListener(fn){hooks[name]=fn;}});
  class Socket {static OPEN=1; constructor(){this.readyState=1;} send(s){sent.push(JSON.parse(s));} close(){}}
  const chrome={runtime:{onMessage:event('message'),onStartup:event('start'),onInstalled:event('installed')},
    storage:{onChanged:event('storage'),local:{async get(keys){return Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,data[k]]));},async set(v){if(storageFailure&&v.lzxBrowserOptions)throw Error('test cache failure');Object.assign(data,v);}}},
    tabs:{async query(){return [tab];},async get(){return tab;},async update(id,value){updates.push({id,...value});Object.assign(tab,value);return tab;},
      async sendMessage(id,value){forwarded.push({id,action:value.message.action,active:tab.active,state,focused});return {accepted:true};}},
    windows:{async get(){return {id:42,state,focused};},async update(id,value){windows.push({id,...value});if(focusFailure)throw Error('test focus denied');state=value.state||state;focused=value.focused??focused;return {id,state,focused};}},
    downloads:{onChanged:event('download')},alarms:{onAlarm:event('alarm')},action:{onClicked:event('action')}};
  const context={chrome,crypto,URL,WebSocket:Socket,console,setTimeout(fn){queueMicrotask(fn);},clearTimeout(){},setInterval(){},clearInterval(){}};
  vm.createContext(context);vm.runInContext(source+'\nglobalThis.api={receive};',context);
  await new Promise(r=>setImmediate(r));
  return {api:context.api,updates,windows,forwarded,sent,data};
}

(async()=>{
  for(const action of ['get_points','prepare','upload_images','run_tasks']){
    const f=await fixture();
    await f.api.receive({action,request_id:action,params:action==='run_tasks'?{batch_id:'a'.repeat(32),tasks:[{}]}:{}});
    assert.equal(f.forwarded[0]?.active,true,action+' must activate its exact saved project tab before using upstream UI');
    assert.equal(f.forwarded[0]?.state,'normal',action+' cannot leave the working project minimized');
    assert.deepEqual(f.windows,[{id:42,state:'normal',focused:false}]);
    assert(f.updates.every(x=>!x.url),'no navigation or reload');
  }
  const visible=await fixture({active:true,state:'maximized'});
  await visible.api.receive({action:'prepare',params:{}});
  assert.equal(visible.updates.length,0);assert.equal(visible.windows.length,0,'leave an existing visible window untouched');
  const passive=await fixture();
  await passive.api.receive({action:'snapshot',params:{}});
  assert.equal(passive.windows.length,0);assert.equal(passive.updates.length,0,'polling must not keep switching browser windows');
  const leaving=await fixture({pendingUrl:'https://flow.google.com/project/another'});
  await leaving.api.receive({action:'prepare',params:{}});
  assert.equal(leaving.updates.length,0);assert.equal(leaving.windows.length,0,'never move a different project');
  const enable=async f=>f.api.receive({type:'timing_settings',browser_options:{focus_before_action:true}});
  for(const action of ['get_points','prepare','upload_images','run_tasks','retry_download']){
    const f=await fixture();await enable(f);
    await f.api.receive({action,params:action==='run_tasks'?{batch_id:'a'.repeat(32),tasks:[{}]}:
      action==='retry_download'?{batch_id:'a'.repeat(32)}:{}});
    assert.deepEqual(f.windows,[{id:42,state:'normal',focused:true}],action+' restores and focuses its own window');
    assert.equal(f.forwarded[0]?.focused,true);
    assert.equal(f.data.lzxBrowserOptions.focus_before_action,true);
  }
  const covered=await fixture({active:true,state:'maximized'});await enable(covered);
  await covered.api.receive({action:'prepare',params:{}});
  assert.deepEqual(covered.windows,[{id:42,focused:true}],'keep maximized geometry');
  const already=await fixture({active:true,state:'maximized',focused:true});await enable(already);
  await already.api.receive({action:'prepare',params:{}});assert.equal(already.windows.length,0);
  const disabled=await fixture({active:true,state:'normal'});await enable(disabled);
  await disabled.api.receive({type:'timing_settings',browser_options:{focus_before_action:false}});
  await disabled.api.receive({action:'prepare',params:{}});
  assert.equal(disabled.windows.length,0,'turning off takes effect before the next action');
  assert.equal(disabled.data.lzxBrowserOptions.focus_before_action,false,'false survives persistence');
  const commandOverride=await fixture({active:true,state:'normal'});await enable(commandOverride);
  await commandOverride.api.receive({action:'prepare',params:{},browser_options:{focus_before_action:false}});
  assert.equal(commandOverride.windows.length,0,'the current command overrides an older cached option');
  const polling=await fixture();await enable(polling);
  await polling.api.receive({action:'snapshot',params:{}});
  assert.equal(polling.windows.length,0);assert.equal(polling.updates.length,0,'enabled focus never affects polling');
  const wrongProject=await fixture({pendingUrl:project+'-other'});await enable(wrongProject);
  await wrongProject.api.receive({action:'prepare',params:{}});
  assert.equal(wrongProject.windows.length,0);assert.equal(wrongProject.updates.length,0);
  const denied=await fixture({active:true,state:'normal',focusFailure:true});await enable(denied);
  await denied.api.receive({action:'run_tasks',params:{batch_id:'a'.repeat(32),tasks:[{}]}});
  assert.equal(denied.forwarded.length,1,'focus denial must not retry or reject the paid action');
  assert.equal(denied.data.lzxBatches['a'.repeat(32)].state,'sent','durable duplicate protection remains');
  const cacheFailure=await fixture({active:true,state:'normal',storageFailure:true});await enable(cacheFailure);
  await cacheFailure.api.receive({action:'prepare',params:{}});
  assert.equal(cacheFailure.forwarded.length,1,'optional setting cache failure must not block page work');
  const malformed=await fixture({active:true,state:'normal'});
  await malformed.api.receive({type:'timing_settings',browser_options:{focus_before_action:'false'}});
  await malformed.api.receive({action:'prepare',params:{}});assert.equal(malformed.windows.length,0);
  console.log('Flow upstream UI path: exact project, optional foreground focus, off-state persistence, denied-focus fallback, no reload or polling churn passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
