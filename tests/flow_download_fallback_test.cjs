// Real media preflight + bound native downloader + adapter recovery routing.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'../'+name),'utf8');
const adapter=read('browser_extensions/flow_adapter/background.js');
const built=read('browser_extensions/flow_bridge/background.js');
const downloader=built.slice(built.indexOf('async function handleDownload('),built.indexOf('// 只处理本扩展发起'));
const routing=adapter.slice(adapter.indexOf('async function retryBoundDownload('),adapter.indexOf('async function freshDownloadUrl('));
const batch='a'.repeat(32),project='https://flow.google.com/project/original',id='known';
const old=`https://flow-content.google/video/${id}?Expires=9999999999&Signature=old`;
const fresh=`https://flow-content.google/video/${id}?Expires=9999999999&Signature=new`;
const name=`FlowBridge/${batch}/result.mp4`,bytes=Uint8Array.from([0,0,0,24,102,116,121,112,105,115,111,109,0,0,0,1]);
function fixture() {
  const probes=[],downloads=[],wake=[],events=[],bound=[];
  const saved={lzxBatches:{[batch]:{}},lzxDownloads:{}};
  let tabProject=project,respond=url=>url===old?new Response('expired',{status:403}):new Response(bytes,{headers:{'Content-Type':'video/mp4'}});
  const binding={schema:2,source:'generation_response',taskId:'task',submissionId:'submit',mediaIds:[id],readyIds:[id],urls:{[id]:old}};
  const message={request_id:'retry',params:{batch_id:batch,restore:{task_id:'task',download_path:name,bindings:[binding],refresh_url:true,media_id:id,project}}};
  const context={URL,Uint8Array,AbortController,console:{log(){},warn(){},error(){}},config:{project},connectionEpoch:1,ownedTab:7,
    recentAutoDownloads:new Map(),local:async()=>saved,ensureTab:async()=>7,sameProjectTab:(tab,url)=>tab.url===url,
    async freshDownloadUrl(_id,_batch,force){assert.equal(_id,id);assert.equal(_batch,batch);wake.push(!!force);return force?fresh:old;},
    async fetch(url){probes.push(url);const response=respond(url);Object.defineProperty(response,'url',{value:fresh});return response;},
    async __lzxDownloadBound(downloadId,task){bound.push({downloadId,task});},
    send:message=>events.push(message),serialize:async fn=>fn(),reportDownload:async()=>{},
    setTimeout:fn=>setImmediate(fn),clearTimeout:clearImmediate,
    chrome:{runtime:{},tabs:{async get(){return {id:7,url:tabProject};}},storage:{session:{async set(){}}},
      downloads:{async search(){return [];},download(options,callback){downloads.push(options);callback(42);}}}};
  vm.createContext(context);vm.runInContext(read('browser_extensions/flow_adapter/download_guard.js')+'\n'+downloader,context);
  context.__lzxRetryBoundDownload=context.handleDownload;vm.runInContext(routing,context);
  context.warmDownloadPage=async()=>{}; // Foreground phase has a separate executable suite.
  return {context,message,saved,probes,downloads,wake,events,bound,run:()=>context.retryBoundDownload(message),
    response:fn=>{respond=fn;},navigate:url=>{tabProject=url;}};
}
(async()=>{
  const direct=fixture();await direct.run();
  assert.equal(direct.probes.length,2);assert(direct.probes[1].includes('name=known'));
  assert.equal(direct.downloads.length,1);assert.equal(direct.bound[0].task.mediaId,id);
  assert.equal(direct.bound[0].task.submissionId,'submit');assert.equal(direct.events.at(-1).data.downloadId,42);
  assert.deepEqual(direct.wake,[false],'successful ID fallback must not unnecessarily play the video');

  const preview=fixture();preview.response(url=>url===fresh?new Response(bytes,{headers:{'Content-Type':'video/mp4'}}):new Response('<html>stale</html>',{headers:{'Content-Type':'text/html'}}));
  await preview.run();assert.equal(preview.downloads.length,1);assert.equal(preview.probes.length,3);
  assert.deepEqual(preview.wake,[false,true]);assert.equal(preview.downloads[0].filename,name);

  const rejected=fixture();rejected.response(()=>new Response('<html>login</html>',{headers:{'Content-Type':'text/html'}}));
  await assert.rejects(rejected.run(),/原视频预览后取址/);
  assert.equal(rejected.downloads.length,0,'all bad responses must be filtered before a Chrome item exists');
  assert.equal(rejected.probes.length,3,'one recovery command is bounded');

  const unknown=fixture();unknown.context.__lzxRetryBoundDownload=async()=>({error:'native result unknown'});
  await assert.rejects(unknown.run(),/native result unknown/);assert.deepEqual(unknown.wake,[false]);
  assert.equal(unknown.downloads.length,0,'unknown initiation cannot authorize another download');

  const changed=fixture();changed.response(()=>{changed.navigate('https://flow.google.com/project/other');return new Response('expired',{status:403});});
  await assert.rejects(changed.run(),/原项目或连接已经变化/);assert.equal(changed.probes.length,1);assert.equal(changed.downloads.length,0);

  const retained=fixture();retained.saved.lzxDownloads[42]={batch,taskId:'task',submissionId:'submit',mediaId:id};
  retained.context.chrome.downloads.search=async()=>[{id:42,state:'in_progress',exists:true}];
  await retained.run();assert.equal(retained.probes.length,0);assert.equal(retained.downloads.length,0);

  const invalid=fixture();invalid.message.params.restore.bindings[0].mediaIds=['different'];
  await assert.rejects(invalid.run(),/原媒体/);assert.equal(invalid.probes.length,0);
  console.log('Bound download fallback: stale URL -> exact ID -> forced preview, HTML filtering, bounded retries, unknown outcomes, project change and existing downloads passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
