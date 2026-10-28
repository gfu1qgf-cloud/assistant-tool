const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8');
const events=[],hooks={};let work=0;
const window={postMessage(value){events.push(value.data);},addEventListener(name,callback){hooks[name]=callback;}};window.top=window;
const location={origin:'https://flow.google.com',href:'https://flow.google.com/project/cancel'};
const ctx={window,location,crypto,console,setInterval(){},setTimeout,MutationObserver:class{observe(){}},
  document:{documentElement:{},querySelector(){return null;},querySelectorAll(){return [];}}};
vm.runInNewContext(source,ctx);
const receive=message=>hooks.message({source:window,origin:location.origin,data:{sender:'lzx-flow-extension',message}});
(async()=>{
  window.__lzxFlowTransport.attach(async()=>work++,()=>({tasks:[]}),{});
  const request='a'.repeat(32);
  await receive({action:'cancel_operation',target_request_id:request});
  await receive({action:'upload_images',request_id:request});
  const reply=events.find(e=>e.request_id===request&&e.action==='upload_images_response');
  assert(reply);assert.equal(reply.success,false);assert.equal(reply.code,'FLOW_OPERATION_CANCELLED');assert.equal(work,0);
  assert.equal(window.__lzxFlowTransport.uiMutexActive(),false);
  console.log('Retired page request rejected before controls, account reads or upload, even with UI mutex disabled.');
})().catch(error=>{console.error(error);process.exitCode=1;});
