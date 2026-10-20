const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const project='https://flow.google.com/project/timing',extension='pnjppjjfgiccpcfajkpooklgohgihdom';
const read=name=>fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/',name),'utf8');
const settings={bridge_heartbeat_seconds:7,bridge_reconnect_initial_seconds:2,bridge_reconnect_max_seconds:8,
  bridge_reconnect_alarm_seconds:90,page_controls_wait_seconds:60,page_poll_seconds:4,
  page_initial_settle_seconds:3,page_dom_stable_seconds:2,page_health_wait_seconds:.8,page_health_valid_seconds:3,
  token:'not-a-page-setting',credit_balances:{private:true}};
async function background(saved={}) {
  const data={lzxConfig:{token:'x'.repeat(40),port:18189,profile:'Profile 1',project},...structuredClone(saved)};
  const sockets=[],sent=[],commands=[],intervals=[],alarms=[],hooks={};let writes=0;
  const event=name=>({addListener:fn=>hooks[name]=fn});
  const tab={id:7,windowId:1,url:project,active:true};
  class Socket{static OPEN=1;constructor(){this.readyState=1;sockets.push(this);}send(value){sent.push(JSON.parse(value));}close(){}}
  const chrome={runtime:{id:extension,getManifest:()=>({version:'1.1.20'}),onMessage:event('message'),
      onStartup:event('startup'),onInstalled:event('installed')},
    storage:{onChanged:event('changed'),local:{get:async keys=>Object.fromEntries(keys.filter(key=>key in data).map(key=>[key,structuredClone(data[key])])),
      set:async value=>{writes++;Object.assign(data,structuredClone(value));},remove:async key=>delete data[key]}},
    tabs:{query:async()=>[tab],get:async()=>tab,update:async()=>tab,
      sendMessage:async(_id,message)=>{commands.push(message);return message.action==='lzxTiming'?{configured:true}:{accepted:true};}},
    windows:{get:async()=>({state:'normal'})},downloads:{search:async()=>[],onChanged:event('downloads')},
    alarms:{create:(...args)=>alarms.push(args),onAlarm:event('alarm')},action:{onClicked:event('action')}};
  const context={chrome,WebSocket:Socket,URL,URLSearchParams,crypto,Date,console,setTimeout:()=>1,clearTimeout(){},
    setInterval:(_fn,ms)=>{intervals.push(ms);return intervals.length;},clearInterval(){}};
  vm.createContext(context);vm.runInContext(read('background.js').replace("import './background.js';",'')+'\nglobalThis.api={receive};',context);
  await new Promise(resolve=>setImmediate(resolve));
  return {api:context.api,data,sockets,sent,commands,intervals,alarms,writes:()=>writes};
}
(async()=>{
  const f=await background(),config=structuredClone(f.data.lzxConfig);
  await f.api.receive({type:'welcome',protocol:1,timing:settings});
  assert.equal(f.intervals.at(-1),7000);assert.equal(f.alarms.at(-1)[1].periodInMinutes,1.5);
  assert.equal(f.data.lzxTiming.page_controls_wait_seconds,60);
  assert(!('token' in f.data.lzxTiming));assert(!('credit_balances' in f.data.lzxTiming));
  assert.deepEqual(f.data.lzxConfig,config);assert.equal(f.sockets.length,1,'timing sync never re-pairs or reconnects');
  const before=f.writes();await f.api.receive({type:'timing_settings',timing:settings});
  assert.equal(f.writes(),before,'an unchanged setting must not rewrite storage or reset page timers');
  await f.api.receive({action:'prepare',request_id:'prepare',params:{duration:6}});
  const forwarded=f.commands.find(message=>message.action==='lzxCommand');
  assert.equal(forwarded.message.timing.page_controls_wait_seconds,60);
  assert.equal(forwarded.message.action,'prepare');assert(!('token' in forwarded.message.timing));
  assert(!f.commands.some(message=>message.message?.action==='run_tasks'),'saving settings cannot submit a generation');
  const restart=await background(f.data);restart.sockets[0].onopen();
  assert.equal(restart.intervals.at(-1),7000,'profile-local settings survive worker restart');
  assert.equal(restart.sent.at(-1).capabilities.includes('timing_settings_v1'),true);

  let now=10000;const hooks={},posted=[],timers=[];const location={origin:'https://flow.google.com',href:project,hash:''};
  const window={__lzxFlowNativeBridge:true,postMessage:value=>posted.push(value),addEventListener:(name,fn)=>hooks[name]=fn};window.top=window;
  const content={window,location,URLSearchParams,Promise,console,Date:{now:()=>now},document:{getElementById:()=>({})},
    chrome:{storage:{local:{get:async()=>({lzxTiming:settings})}},runtime:{sendMessage:async()=>({}),
      onMessage:{addListener:fn=>hooks.runtime=fn}}},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;}};
  vm.createContext(content);vm.runInContext(read('content.js'),content);await new Promise(resolve=>setImmediate(resolve));
  assert(posted.some(event=>event.message?.action==='configure_timing'));
  hooks.message({source:window,origin:location.origin,data:{sender:'lzx-flow-page',data:{type:'snapshot',data:{running:false,boot_ready:true}}}});
  let health;hooks.runtime({action:'lzxPageHealth'},{},value=>health=value);
  assert.equal(timers.at(-1).ms,800);now+=1000;timers.at(-1).fn();assert.equal(health.healthy,true);

  const pageHooks={},polls=[];let pageNow=0;
  const prompt={isConnected:true},button={isConnected:true};
  const mainWindow={postMessage(){},addEventListener:(name,fn)=>pageHooks[name]=fn};mainWindow.top=mainWindow;
  const main={window:mainWindow,location,Date:{now:()=>pageNow},console,
    document:{documentElement:null,readyState:'complete',addEventListener(){},querySelector:selector=>
      selector.includes('ProseMirror')?prompt:selector==='button.settings-trigger-button'?button:null,querySelectorAll:()=>[]},
    MutationObserver:class{observe(){}},setInterval:(_fn,ms)=>{polls.push(ms);return polls.length;},clearInterval(){}};
  vm.createContext(main);vm.runInContext(read('page.js'),main);
  pageHooks.message({source:mainWindow,origin:location.origin,data:{sender:'lzx-flow-extension',
    message:{action:'configure_timing',values:settings}}});
  assert.equal(polls.at(-1),4000,'page polling follows desktop settings before the upstream bridge attaches');
  const api=mainWindow.__lzxFlowTransport;api.attach(async()=>{},()=>({tasks:[],running:false}),{initialized:()=>true});
  pageNow=2000;assert.equal(api.readiness().controls_ready,false);
  pageNow=3000;assert.equal(api.readiness().controls_ready,true,'configured initial and DOM stability waits must both elapse');
  console.log('Flow timing settings: live heartbeat and page waits, worker restart, private-data filtering, no reconnect/replay, content health and early page synchronization passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
