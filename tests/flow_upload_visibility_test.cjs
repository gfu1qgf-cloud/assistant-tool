const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const crypto = require('node:crypto');
const project = 'https://flow.google.com/project/ours';
const source = fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8')
  .replace("import './background.js';",'');

async function fixture({active=false, state='minimized', pendingUrl}={}) {
  const data={lzxConfig:{project,token:'test',port:18189,profile:'Profile 7'}};
  const tab={id:7,windowId:42,url:project,active,pendingUrl};
  const sent=[],updates=[],windows=[],forwarded=[],hooks={};
  const event=name=>({addListener(fn){hooks[name]=fn;}});
  class Socket {static OPEN=1; constructor(){this.readyState=1;} send(s){sent.push(JSON.parse(s));} close(){}}
  const chrome={runtime:{onMessage:event('message'),onStartup:event('start'),onInstalled:event('installed')},
    storage:{onChanged:event('storage'),local:{async get(keys){return Object.fromEntries(keys.filter(k=>k in data).map(k=>[k,data[k]]));},async set(v){Object.assign(data,v);}}},
    tabs:{async query(){return [tab];},async get(){return tab;},async update(id,value){updates.push({id,...value});Object.assign(tab,value);return tab;},
      async sendMessage(id,value){forwarded.push({id,action:value.message.action,active:tab.active,state});return {accepted:true};}},
    windows:{async get(){return {id:42,state};},async update(id,value){windows.push({id,...value});state=value.state||state;return {id,state};}},
    downloads:{onChanged:event('download')},alarms:{onAlarm:event('alarm')},action:{onClicked:event('action')}};
  const context={chrome,crypto,URL,WebSocket:Socket,console,setTimeout(fn){queueMicrotask(fn);},clearTimeout(){},setInterval(){},clearInterval(){}};
  vm.createContext(context);vm.runInContext(source+'\nglobalThis.api={receive};',context);
  await new Promise(r=>setImmediate(r));
  return {api:context.api,updates,windows,forwarded,sent};
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
  console.log('Flow upstream UI path: exact active project, minimized-window recovery, no foreground focus, no reload or polling churn passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
