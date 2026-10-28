// Actual adapter code, isolated tabs and page worlds; no Google or real Chrome.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=process.env.LZX_VIDEO_PAGE_ROOT || path.join(__dirname,'..');
const source=fs.readFileSync(path.join(root,'browser_extensions/flow_adapter/background.js'),'utf8');
const code=source.slice(source.indexOf('async function retryBoundDownload('),source.indexOf('async function connect()',source.indexOf('async function retryBoundDownload(')));
const project='https://flow.google.com/project/ours',media='known',pageUrl=project+'/edit/'+media,batch='a'.repeat(32);
const signed=`https://flow-content.google/video/${media}?Expires=9999999999&Signature=fresh`;
const resolver=`https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=${media}&mediaUrlType=MEDIA_URL_TYPE_FULL_MEDIA`;
function fixture({detail=true,nodes=true,network=false}={}) {
  const tabs=new Map([[7,{id:7,url:project,active:false}]]);
  if(detail)tabs.set(8,{id:8,url:pageUrl+'?display=1#video',active:true});
  const reads=[],native=[],events=[],fetches=[],wakes=[];
  const binding={schema:2,source:'generation_response',taskId:'task',submissionId:'submission',mediaIds:[media],readyIds:[],urls:{}};
  const message={request_id:'retry',params:{batch_id:batch,restore:{task_id:'task',download_path:`FlowBridge/${batch}/result.mp4`,
    bindings:[binding],refresh_url:true,media_id:media,project,video_page:pageUrl}}};
  const sameProjectTab=(tab,url)=>{
    try {const a=new URL(tab.pendingUrl || tab.url),b=new URL(url);return a.origin===b.origin && a.pathname.split('/edit/')[0]===b.pathname.split('/edit/')[0];}
    catch {return false;}
  };
  let navigateDuringRead=false;
  const context={URL,config:{project},connectionEpoch:1,ownedTab:7,ensureTab:async()=>7,sameProjectTab,
    local:async()=>({lzxBatches:{[batch]:{}},lzxDownloads:{}}),send:message=>events.push(message),
    async __lzxRetryBoundDownload(url,filename,identity){native.push({url,filename,identity});return url===signed?{success:true,downloadId:42}:{success:false,code:'FLOW_DOWNLOAD_NOT_VIDEO',error:'unresolved'};},
    chrome:{tabs:{async query(){return [...tabs.values()];},async get(id){return tabs.get(id);}},downloads:{async search(){return [];}},
      scripting:{async executeScript({target,func,args}) {
        const id=target.tabId;reads.push(id);const tab=tabs.get(id);
        const world={URL,Date,AbortController,setTimeout,clearTimeout,location:{href:tab.url},
          document:{querySelectorAll(){return nodes && id===8?[{tagName:'VIDEO',src:signed,readyState:1}]:[];}},
          window:{__lzxFlowTransport:{async wakeResultPreview(){wakes.push(id);}}}};
        if(network)world.fetch=async (url,options)=>{
          fetches.push({id,url,credentials:options.credentials});
          const response=new Response(new Uint8Array([0,0,0,24,102,116,121,112]),{status:200});
          Object.defineProperty(response,'url',{value:signed});return response;
        };
        vm.createContext(world);world.args=args;
        const result=await vm.runInContext(`(${func.toString()})(...args)`,world);
        if(navigateDuringRead)tabs.get(id).url=project+'/edit/another';
        return [{result}];
      }}}};
  vm.createContext(context);vm.runInContext(code,context);
  context.warmDownloadPage=async()=>{}; // Exact-page routing is independent of the bounded UI phase.
  return {context,message,tabs,reads,native,events,fetches,wakes,run:()=>context.retryBoundDownload(message),navigate:()=>navigateDuringRead=true};
}
(async()=>{
  const exact=fixture();await exact.run();assert.deepEqual(exact.reads,[8]);assert.equal(exact.native[0].url,signed);
  assert.equal(exact.native[0].identity.tabId,8);assert.equal(exact.context.ownedTab,7,'reading a result must never move the generation owner');
  const empty=fixture({nodes:false,network:true});await empty.run();
  assert.equal(empty.fetches[0].id,8);assert.equal(empty.fetches[0].url,resolver);assert.equal(empty.fetches[0].credentials,'include');
  assert.equal(empty.native[0].url,signed,'no rendered tile still resolves a verified ID inside the exact original page');
  const automatic=fixture({detail:false,nodes:false,network:true});delete automatic.message.params.restore.video_page;
  await automatic.run();assert.deepEqual(automatic.reads,[7]);assert.equal(automatic.native[0].url,signed,'normal recovery can authenticate an offscreen result without a DOM tile');
  const absent=fixture({detail:false});await assert.rejects(absent.run(),/视频详情页/);assert.equal(absent.native.length,0);
  const wrong=fixture();wrong.message.params.restore.video_page=project+'/edit/foreign';
  await assert.rejects(wrong.run(),/编号不一致/);assert.equal(wrong.native.length,0);
  const foreign=fixture();foreign.message.params.restore.video_page='https://flow.google.com/project/foreign/edit/known';
  await assert.rejects(foreign.run(),/原项目/);assert.equal(foreign.native.length,0);
  const leaving=fixture();leaving.tabs.get(8).pendingUrl=project+'/edit/other';
  await assert.rejects(leaving.run(),/视频详情页/);assert.equal(leaving.native.length,0);
  const changed=fixture();changed.navigate();await assert.rejects(changed.run(),/原项目或连接已经变化/);assert.equal(changed.native.length,0);
  console.log('Video-page recovery: exact detail tab, unchanged generation owner, authenticated no-DOM lookup, wrong ID/project and navigation guards passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
