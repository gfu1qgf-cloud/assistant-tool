const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const start=source.indexOf('async function receive('),end=source.indexOf('async function closeExhausted(',start);
assert(start>=0&&end>start);
async function check(action,manual){
 const events=[];let acquired=0,released=0,performed=0;
 const c={crypto,console,browserOptions:{ui_mutex:true},closingBrowser:false,commandsInFlight:0,
  applyBrowserOptions:async()=>{},ensureTab:async()=>7,
  acquireUiLease:async()=>{acquired++;return {ok:true};},releaseUiLease:()=>released++,
  readyWorkTab:async()=>{performed++;return 'focused';},repairProject:async()=>{performed++;return {};},send:e=>events.push(e)};
 vm.createContext(c);vm.runInContext(source.slice(start,end),c);
 await c.receive({action,request_id:'a'.repeat(32),params:manual?{manual_ui:true}:{}});
 assert.equal(acquired,manual?0:1);assert.equal(released,manual?0:1);assert.equal(performed,1);
 assert.equal(c.commandsInFlight,0);assert.equal(events.at(-1).success,true);
}
(async()=>{
 for(const action of ['focus_window','repair_project']){await check(action,true);await check(action,false);}
 console.log('Manual focus and F5 bypass automatic UI queue; automatic commands still acquire and release their own lease.');
})().catch(e=>{console.error(e);process.exitCode=1;});
