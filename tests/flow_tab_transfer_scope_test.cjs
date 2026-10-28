const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let prefix=fs.readFileSync(path.join(__dirname,'flow_tab_launch_test.cjs'),'utf8').split('(async () => {')[0];
prefix=prefix.replace('globalThis.api = {ensureTab, receive, resumeReloadSetup}', 'globalThis.api = {ensureTab, receive, resumeReloadSetup, forward}')
  .replace('return {api: context.api, tabs, data, created, updates, focused, messages, sockets, hooks, reloads};',
    'return {api: context.api, tabs, data, created, updates, focused, messages, sockets, hooks, reloads, probeChrome:chrome};');
const env={require,__dirname,console,setImmediate,structuredClone,URL,URLSearchParams};vm.createContext(env);vm.runInContext(prefix,env);
(async()=>{
  const project='https://flow.google.com/project/our-project';
  const f=await env.fixture([{id:1,windowId:42,url:'https://flow.google.com/project/foreign',active:true},
    {id:7,windowId:42,url:project,active:true},{id:8,windowId:42,url:project,active:false},
    {id:9,windowId:42,url:project+'/edit/result',active:true}],{lzxOwnedTab:8});
  const targets=[];f.probeChrome.tabs.sendMessage=async(id,command)=>{targets.push({id,action:command.message?.action});return {accepted:true};};
  assert.equal(await f.api.ensureTab(),8);
  for(const action of ['prepare','upload_images','snapshot'])await f.api.forward({action,request_id:action,params:{}});
  assert(targets.every(item=>item.id===8),'multiple same-project and foreign tabs cannot move upload/detection away from the owned tab');
  f.tabs.delete(8);assert.equal(await f.api.ensureTab(),7);
  const before=f.messages.length;
  await f.hooks.message({action:'lzxPageEvent',data:{type:'snapshot',data:{tasks:[]}}},
    {id:'pnjppjjfgiccpcfajkpooklgohgihdom',frameId:0,tab:{id:8,url:project}},()=>{});
  assert.equal(f.messages.length,before,'late old-tab results are ignored after handoff');
  let reply;
  f.hooks.message({action:'lzxUiMutex',token:'c'.repeat(32),operation:'retry_download',batch_id:'a'.repeat(32)},
    {id:'pnjppjjfgiccpcfajkpooklgohgihdom',frameId:0,tab:f.tabs.get(7)},value=>{reply=value;});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(reply.ok,true,'the real extension entry point must admit download preview permits');
  assert.equal(f.created.length,0);
  console.log('Tab transfer scope: owned-page routing across prepare/upload/snapshot, explicit handoff, late-old-page rejection and download-preview mutex entry passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
