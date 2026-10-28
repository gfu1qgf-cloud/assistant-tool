const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const guard=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/download_guard.js'),'utf8');
const background=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/background.js'),'utf8');
const download=background.slice(background.indexOf('async function handleDownload('),background.indexOf('// 只处理本扩展发起'));
const bytes=Uint8Array.from([0,0,0,24,102,116,121,112,105,115,111,109,0,0,0,1,105,115,111,109,109,112,52,50]);
const batch='a'.repeat(32),name=`FlowBridge/${batch}/result.mp4`,task={download_path:name,taskId:'task',submissionId:'submission',mediaId:'known',tabId:7};
const url='https://flow-content.google/video/known?Expires=9999999999&Signature=valid';
function fixture() {
  let response=()=>new Response(bytes,{headers:{'Content-Type':'video/mp4'}}),downloads=0,probes=0,aborted=false;
  const context={URL,Uint8Array,AbortController,console:{log(){},warn(){},error(){}},recentAutoDownloads:new Map(),
    async fetch(_url,options){probes++;assert.equal(options.headers.Range,'bytes=0-511');options.signal.addEventListener('abort',()=>{aborted=true;});const result=await response();Object.defineProperty(result,'url',{value:_url});return result;},
    setTimeout:fn=>setImmediate(fn),clearTimeout:clearImmediate,
    chrome:{runtime:{},storage:{session:{async set(){}}},downloads:{download(_options,callback){downloads++;callback(42);},async search(){return [{state:'in_progress'}];}}}};
  vm.createContext(context);vm.runInContext(guard+'\n'+download,context);
  return {context,setResponse:fn=>{response=fn;},run:()=>context.handleDownload(url,name,task),stats:()=>({downloads,probes,aborted})};
}
(async()=>{
  for(const [mime,body] of [['text/html','<html>login required</html>'],['application/json','{"error":"not ready"}'],['image/png',new Uint8Array([137,80,78,71])],['video/mp4','<html>wrongly labelled error</html>']]) {
    const f=fixture();f.setResponse(()=>new Response(body,{headers:{'Content-Type':mime}}));
    const result=await f.run();assert(result.error);assert.equal(f.stats().downloads,0,'invalid content must never create a Chrome download');
  }
  for(const status of [202,401,403,404,500]) {
    const f=fixture();f.setResponse(()=>new Response('',{status}));assert((await f.run()).error);assert.equal(f.stats().downloads,0);
  }
  const changed=fixture();changed.setResponse(()=>new Response('<html>expired</html>',{headers:{'Content-Type':'text/html'}}));
  assert((await changed.run()).error);changed.setResponse(()=>new Response(bytes,{headers:{'Content-Type':'application/octet-stream'}}));
  assert.equal((await changed.run()).success,true);assert.equal(changed.stats().downloads,1);assert.equal(changed.stats().probes,2,'failed preflight cannot poison later exact-ID retries');
  const timeout=fixture();timeout.setResponse(()=>new Promise(()=>{}));assert((await timeout.run()).error.includes('超时'));
  assert.equal(timeout.stats().downloads,0);assert(timeout.stats().aborted);
  const filename=fixture();filename.setResponse(()=>new Response(bytes,{headers:{'Content-Type':'video/mp4','Content-Disposition':'attachment; filename="result.htm"'}}));
  assert((await filename.run()).error);assert.equal(filename.stats().downloads,0);
  const stream=fixture();let reads=0,cancelled=false;
  stream.setResponse(()=>({status:206,headers:new Headers({'Content-Type':'video/mp4'}),body:{getReader:()=>({async read(){reads++;return {value:bytes,done:false};},async cancel(){cancelled=true;}})}}));
  assert.equal((await stream.run()).success,true);assert.equal(reads,1);assert(cancelled,'range probe must cancel before consuming the full video');
  console.log('Download content guard: HTML/JSON/images, false MIME, HTTP failures, timeout, retry recovery, disposition and bounded stream read passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
