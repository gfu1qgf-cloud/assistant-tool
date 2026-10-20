// Execute the generated bridge's real event handlers without rendering a video.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const {installDownloadHelpers} = require('./flow_test_support.cjs');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'),'utf8');
function fixture() {
  const hooks = {}, downloads = [], id = 'owned-media';
  const binding = {schema:2,source:'generation_response',taskId:'task',submissionId:'submission',mediaIds:[],readyIds:[],urls:{}};
  const task = {taskId:'task',download_path:`FlowBridge/${'a'.repeat(32)}/result.mp4`,mediaBindings:[binding],status:['generating']};
  const state = {isRunning:true,tasks:[task],currentIndex:0,currentLineIndex:0};
  const context = {state, console:{log(){},warn(){}}, URL,
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    window:{__lzxFlowTransport:{},currentProcess:{rowIndex:0,lineIndex:0,submissionId:'submission'},
      addEventListener(type,handler){hooks[type]=handler;},dispatchEvent(event){hooks[event.type]?.(event);}},
    async saveState(){}, async triggerAutoDownload(url,mediaId){downloads.push({url,mediaId});},
    findMediaBinding(mediaId){return state.tasks.flatMap(task=>(task.mediaBindings||[]).filter(b=>b.mediaIds.includes(mediaId))
      .map(binding=>({task,binding,rowIndex:0,lineIndex:0}))).find(Boolean);},
    setupDomVideoWatcher(){}, wasVideoSeen(){return false;}, isStaticShowcaseAsset(){return false;}};
  vm.createContext(context); installDownloadHelpers(context,source);
  const identityStart = source.indexOf('  function mediaIdFromVideoUrl('), identityEnd = source.indexOf('  // 从视频缩略图',identityStart);
  vm.runInContext(source.slice(identityStart,identityEnd),context);
  const start = source.indexOf('    window.addEventListener("flow-task-media-bound"'), end = source.indexOf('    setupDomVideoWatcher();',start);
  vm.runInContext(source.slice(start,end),context);
  return {context,hooks,binding,task,state,downloads,id};
}
const url = id=>`https://flow-content.google/video/${id}?Expires=9999999999&Signature=valid-signature`;
const ready = async(f,id,source)=>{await f.hooks['flow-task-media-ready']({detail:{mediaId:id,url:url(id),source}});};
const bound = (f,ids)=>f.hooks['flow-task-media-bound']({detail:{submissionId:'submission',mediaIds:ids,source:'generation_response'}});
(async()=>{
  const f=fixture();
  await ready(f,f.id); assert.equal(f.downloads.length,0,'no ownership yet, no download');
  bound(f,[f.id]); await new Promise(r=>setImmediate(r));
  assert.equal(f.downloads.length,1,'late exact ownership must recover a ready receipt without a video element');
  assert.equal(f.downloads[0].mediaId,f.id);
  for(let n=0;n<25;n++) f.context.reconcileResultReceipts();
  assert.equal(f.downloads.length,1,'forced snapshots cannot duplicate the receipt download');
  const old=fixture(); await ready(old,'old-project-media'); bound(old,[old.id]); await new Promise(r=>setImmediate(r));
  assert.equal(old.downloads.length,0,'old project video cannot substitute for the generated media');
  const discovery=fixture(); await ready(discovery,discovery.id,'url_discovery'); bound(discovery,[discovery.id]);
  await new Promise(r=>setImmediate(r)); assert.equal(discovery.downloads.length,0,'URL discovery alone is not a ready receipt');
  const failed=fixture(); failed.binding.failedIds=[failed.id]; await ready(failed,failed.id); bound(failed,[failed.id]);
  await new Promise(r=>setImmediate(r)); assert.equal(failed.downloads.length,0,'explicit failed generation cannot become ready');
  const wrong=fixture(); await wrong.hooks['flow-task-media-ready']({detail:{mediaId:wrong.id,url:url('foreign-media')}});
  bound(wrong,[wrong.id]); await new Promise(r=>setImmediate(r)); assert.equal(wrong.downloads.length,0,'URL and media identity must agree');
  const duplicates=fixture(); duplicates.state.tasks.push({...duplicates.task,taskId:'other-task',mediaBindings:[{...duplicates.binding,taskId:'other-task',mediaIds:[duplicates.id]}]});
  await ready(duplicates,duplicates.id); // remove direct ready state to exercise reconciliation's ambiguity guard
  duplicates.binding.readyIds=[]; duplicates.binding.urls={}; duplicates.downloads.length=0;
  bound(duplicates,[duplicates.id]); await new Promise(r=>setImmediate(r)); assert.equal(duplicates.downloads.length,0,'ambiguous ownership cannot be replayed');
  assert(source.includes('while (managedReadyReceipts.size > 64)'),'receipt memory stays bounded');
  console.log('Flow ready receipts: out-of-order exact IDs recover without DOM or foreground; old, failed, discovered, mismatched and ambiguous results rejected; no duplicate replay.');
})().catch(error=>{console.error(error);process.exitCode=1;});
