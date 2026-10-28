// Real patched native downloader, including filename events before its callback.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=process.env.LZX_GUARD_ROOT || path.join(__dirname,'..'),guard=fs.readFileSync(path.join(root,'browser_extensions/flow_adapter/download_guard.js'),'utf8');
const built=fs.readFileSync(path.join(root,'browser_extensions/flow_bridge/background.js'),'utf8');
const download=built.slice(built.indexOf('async function handleDownload('),built.indexOf('// 按下载 ID 保存归属'));
const bytes=Uint8Array.from([0,0,0,24,102,116,121,112,105,115,111,109,0,0,0,1]);
const name=`FlowBridge/${'a'.repeat(32)}/result.mp4`,task={download_path:name,taskId:'task',submissionId:'submission',mediaId:'known'};
const signed='https://flow-content.google/video/known?Expires=9999999999&Signature=fresh';
const redirect='https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=known';
const extension='pnjppjjfgiccpcfajkpooklgohgihdom';
function fixture({mime='video/mp4',cancel=true,includeGuard=true,session={},items=new Map(),late=false}={}) {
  const hooks={},issued=[],suggestions=[],cancelled=[],bound=[],probes=[];let actualMime=mime,next=43;
  const event=key=>({addListener:fn=>{(hooks[key] ||= []).push(fn);}});
  const context={URL,Uint8Array,AbortController,console:{log(){},warn(){},error(){}},recentAutoDownloads:new Map(),
    async fetch(url){probes.push(url);const response=new Response(bytes,{headers:{'Content-Type':'video/mp4'}});Object.defineProperty(response,'url',{value:signed});return response;},
    setTimeout,clearTimeout,
    chrome:{runtime:{id:extension},storage:{session:{async set(value){Object.assign(session,value);},async get(keys){return Object.fromEntries(keys.filter(key=>key in session).map(key=>[key,session[key]]));},async remove(key){delete session[key];}}},
      downloads:{onCreated:event('created'),onChanged:event('changed'),onDeterminingFilename:event('filename'),
        async search({id}){return items.has(id)?[items.get(id)]:[];},
        async cancel(id){cancelled.push(id);if(!cancel)throw Error('not yet cancelled');items.get(id).state='interrupted';},
        download(options,callback){issued.push(options);const id=next++;const item={id,url:options.url,finalUrl:options.url,byExtensionId:extension,
          mime:late?'':actualMime,filename:late?'result.mp4':actualMime==='text/html'?'result.htm':name,state:'in_progress',exists:true};items.set(id,item);
          for(const fn of hooks.created || [])fn(item);
          if(!late)for(const fn of hooks.filename || [])fn(item,value=>suggestions.push(value));
          callback(id);
        }}},
    async __lzxDownloadBound(id,data){bound.push({id,data});},async notifyDownload(){}};
  vm.createContext(context);vm.runInContext((includeGuard?guard+'\n':'')+download,context);
  return {context,issued,suggestions,cancelled,bound,session,items,probes,hooks,
    run:url=>context.handleDownload(url || redirect,name,task),setMime:value=>{actualMime=value;}};
}
(async()=>{
  const valid=fixture();const success=await valid.run();assert(success.success);
  assert.equal(valid.issued[0].url,signed,'native download uses the final URL actually checked, avoiding another redirect');
  assert.equal(valid.bound.length,1);assert.equal(valid.hooks.filename.length,1,'exactly one filename-suggestion listener');

  const changed=fixture({mime:'text/html'});const rejected=await changed.run();
  assert.equal(rejected.success,undefined);assert.equal(rejected.code,'FLOW_DOWNLOAD_ACTUAL_NOT_VIDEO');
  assert.deepEqual(changed.cancelled,[43]);assert.equal(changed.items.get(43).state,'interrupted');
  assert.equal(changed.bound.length,0,'a successful range probe cannot authorize an actual HTML download');
  await new Promise(resolve=>setImmediate(resolve));
  assert(!changed.suggestions.some(value=>value?.filename?.endsWith('.mp4')),'never disguise an HTML page as an MP4');

  const absent=fixture({includeGuard:false});const unavailable=await absent.run();
  assert.equal(unavailable.code,'FLOW_DOWNLOAD_GUARD_UNAVAILABLE');assert.equal(absent.issued.length,0);

  const uncertain=fixture({mime:'text/html',cancel:false});
  assert.equal((await uncertain.run()).code,'FLOW_DOWNLOAD_OUTCOME_UNCONFIRMED');
  assert.equal((await uncertain.run()).code,'FLOW_DOWNLOAD_OUTCOME_UNCONFIRMED');
  assert.equal(uncertain.issued.length,1,'unconfirmed cancellation cannot create another download');
  const restarted=fixture({session:uncertain.session,items:uncertain.items});
  assert.equal((await restarted.run()).code,'FLOW_DOWNLOAD_OUTCOME_UNCONFIRMED');assert.equal(restarted.issued.length,0,'block survives a worker restart');

  const late=fixture({late:true});assert((await late.run()).success);
  const item=late.items.get(43);item.mime='text/html';item.filename='result.htm';
  for(const fn of late.hooks.changed)fn({id:43,mime:{current:'text/html'}});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(item.state,'interrupted','late MIME metadata is also checked');

  const foreign=fixture();const other={id:100,url:signed,byExtensionId:'another-extension',mime:'text/html',filename:'page.htm',state:'in_progress'};
  for(const fn of foreign.hooks.created)fn(other);
  for(const fn of foreign.hooks.filename)fn(other,()=>{});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(foreign.cancelled.length,0);
  console.log('Actual native download guard: final verified URL, range/video -> native/HTML, one filename listener, cancellation, missing guard, restart-safe uncertainty and foreign downloads passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
