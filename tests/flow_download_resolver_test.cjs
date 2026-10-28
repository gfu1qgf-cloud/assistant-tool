// Exercise actual URL selection and preflight/native boundary, never Google.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=process.env.LZX_GUARD_ROOT || path.join(__dirname,'..');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const adapter=read('browser_extensions/flow_adapter/background.js');
const built=read('browser_extensions/flow_bridge/background.js');
const download=built.slice(built.indexOf('async function handleDownload('),built.indexOf('// 按下载 ID 保存归属'));
const guard=read('browser_extensions/flow_adapter/download_guard.js');
const task={download_path:`FlowBridge/${'a'.repeat(32)}/result.mp4`,taskId:'task',submissionId:'submission',mediaId:'known'};
const resolver='https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=known&mediaUrlType=MEDIA_URL_TYPE_FULL_MEDIA';
const signed='https://flow-content.google/video/known?Expires=9999999999&Signature=fresh';
const project='https://flow.google.com/project/ours';
const bytes=Uint8Array.from([0,0,0,24,102,116,121,112,105,115,111,109,0,0,0,1]);
function nativeFixture({final=resolver,mime='video/mp4',status=200,actual='text/html',session={},items=new Map()}={}) {
  const issued=[],cancelled=[],bound=[],hooks={},events=[];let next=50;
  const event=key=>({addListener:fn=>(hooks[key] ||= []).push(fn)});
  const context={URL,Uint8Array,AbortController,setTimeout,clearTimeout,console:{log(){},warn(){},error(){}},recentAutoDownloads:new Map(),
    async fetch(){const result=new Response(status===200?bytes:'unauthorized',{status,headers:{'Content-Type':mime}});if(final!==null)Object.defineProperty(result,'url',{value:final});return result;},
    chrome:{runtime:{id:'ours'},storage:{session:{async get(keys){if(typeof keys==='string')keys=[keys];return Object.fromEntries(keys.filter(k=>k in session).map(k=>[k,session[k]]));},async set(data){Object.assign(session,data);},async remove(key){delete session[key];}}},
      downloads:{onCreated:event('created'),onChanged:event('changed'),onDeterminingFilename:event('filename'),async search({id}){return items.has(id)?[items.get(id)]:[];},async cancel(id){cancelled.push(id);items.get(id).state='interrupted';},
        download(options,callback){issued.push(options);const id=next++;const item={id,url:options.url,finalUrl:options.url,byExtensionId:'ours',mime:actual,filename:actual==='text/html'?'result.htm':task.download_path,state:'in_progress'};items.set(id,item);for(const fn of hooks.created || [])fn(item);callback(id);}}},
    async __lzxDownloadBound(id,data){bound.push({id,data});},async notifyDownload(){},send:value=>events.push(value)};
  vm.createContext(context);vm.runInContext(guard+'\n'+download,context);
  return {context,issued,cancelled,bound,session,items,run:()=>context.handleDownload(resolver,task.download_path,task)};
}
function pageFixture(nodes,{resolveUrl=signed,status=200,network=false}={}) {
  const dom={nodes,querySelectorAll(){return this.nodes;}},wakes=[];
  const page={URL,Date,location:{href:project},document:dom,window:{__lzxFlowTransport:{async wakeResultPreview(){wakes.push(true);}}}};
  if(network)Object.assign(page,{AbortController,setTimeout,clearTimeout,async fetch(url,options){assert.equal(url,resolver);assert.equal(options.credentials,'include');const response=new Response(bytes,{status});Object.defineProperty(response,'url',{value:resolveUrl});return response;}});
  vm.createContext(page);
  const context={config:{project},chrome:{tabs:{async get(){return {url:project};}},scripting:{async executeScript({func,args}){page.args=args;return [{result:await vm.runInContext(`(${func.toString()})(...args)`,page)}];}}},ensureTab:async()=>7,sameProjectTab:()=>true};
  vm.createContext(context);vm.runInContext(adapter.slice(adapter.indexOf('async function freshDownloadUrl('),adapter.indexOf('async function connect()',adapter.indexOf('async function freshDownloadUrl('))),context);
  return {run:()=>context.freshDownloadUrl('known','a'.repeat(32)),wakes};
}
(async()=>{
  // A valid small response from a resolver does not make its URL a CDN file.
  const unresolved=nativeFixture();assert((await unresolved.run()).error);
  assert.equal(unresolved.issued.length,0,'an unresolved getMediaUrlRedirect must NEVER be handed to Chrome');
  const absent=nativeFixture({final:null});assert((await absent.run()).error);assert.equal(absent.issued.length,0,'missing final URL fails closed');
  const auth=nativeFixture({status:401,mime:'text/html'});for(let n=0;n<5;n++)assert((await auth.run()).error);assert.equal(auth.issued.length,0);
  const valid=nativeFixture({final:signed,actual:'video/mp4'});assert((await valid.run()).success);assert.equal(valid.issued[0].url,signed);

  const rejected=nativeFixture({final:signed});assert((await rejected.run()).error);assert.equal(rejected.issued.length,1);
  for(let n=0;n<5;n++)assert((await rejected.run()).error);
  assert.equal(rejected.issued.length,1,'the same rejected native address cannot create more HTML entries');
  const restarted=nativeFixture({final:signed,session:rejected.session,items:rejected.items});assert((await restarted.run()).error);assert.equal(restarted.issued.length,0,'rejection survives service worker restart');
  const refreshed=nativeFixture({final:signed+'&refresh=2',actual:'video/mp4',session:rejected.session,items:rejected.items});assert((await refreshed.run()).success,'a genuinely refreshed validated video address can recover');

  const stale=nativeFixture({final:signed});const staleItem={id:99,url:signed,finalUrl:signed,mime:'text/html',filename:task.download_path,state:'complete'};
  stale.items.set(99,staleItem);const saved={lzxDownloads:{99:{...task,batch:'a'.repeat(32)}}},events=[];
  stale.context.local=async()=>saved;stale.context.chrome.storage.local={async set(value){Object.assign(saved,value);}};stale.context.send=value=>events.push(value);
  vm.runInContext(adapter.slice(adapter.indexOf('async function reportDownload('),adapter.indexOf('globalThis.__lzxDownloadBound =')),stale.context);
  await stale.context.reportDownload(99);assert.equal(events[0].data.state,'interrupted','old completed HTML cannot be reported as a video success after reconnect');

  const chosen=pageFixture([{tagName:'VIDEO',src:signed,readyState:1},{tagName:'A',href:resolver}]);
  assert.equal(await chosen.run(),signed,'a resolver anchor cannot hide the exact video direct URL');assert.equal(chosen.wakes.length,0);
  const aliases=pageFixture([{tagName:'VIDEO',src:signed,readyState:1},{tagName:'A',href:'https://flow.google.com/asb/known'},{tagName:'A',href:resolver}]);
  assert.equal(await aliases.run(),signed,'the signed original video wins over its ASB/lookup alternatives');
  const two=pageFixture([{tagName:'VIDEO',src:signed,readyState:1},{tagName:'VIDEO',src:signed.replace('fresh','fresh2'),readyState:1}]);
  assert.equal(await two.run(),'','genuinely ambiguous video URLs remain rejected');
  const foreign=pageFixture([{tagName:'VIDEO',src:signed.replace('/known','/foreign'),readyState:1},{tagName:'A',href:resolver}]);
  assert.notEqual(await foreign.run(),signed,'another media cannot replace this original result');
  const authenticated=pageFixture([{tagName:'A',href:resolver}],{network:true});assert.equal(await authenticated.run(),signed,'lookup uses page credentials and yields the original signed video, never its RPC URL');
  assert.equal(authenticated.wakes.length,1,'an RPC anchor alone is not a ready preview');
  const noAuth=pageFixture([{tagName:'A',href:resolver}],{network:true,status:401});assert.equal(await noAuth.run(),'');
  const wrongId=pageFixture([{tagName:'A',href:resolver}],{network:true,resolveUrl:signed.replace('/known','/foreign')});assert.equal(await wrongId.run(),'');
  console.log('Resolver/native boundary: unresolved endpoint, 401 HTML, missing URL, exact direct selection, persistent failed-URL quarantine and fresh-link recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
