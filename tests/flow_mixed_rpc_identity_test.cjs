const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/injects/flow-proxy.js'),'utf8').replace(/\r\n/g,'\n');
const rpc=(id,payload)=>['wrb.fr',id,JSON.stringify(payload)];
const payload=id=>[null,null,[[`group-${id}`,null,null,[null,null,null,null,id],`version-${id}`]],[[id,`version-${id}`,`group-${id}`]]];
function fixture(){const events=[],process={submissionId:'new-submission'};
  const window={currentProcess:process,__lzxFlowTransport:{matchesSubmission:p=>p===process},dispatchEvent:e=>events.push(e)};
  const context={URL,URLSearchParams,window,console:{log(){},warn(){}},CustomEvent:class{constructor(type,opts){this.type=type;this.detail=opts.detail;}}};
  vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function unescapeBoQ('),source.indexOf('  function setupFetchHook(')),context);
  return {context,process,bound:()=>events.filter(e=>e.type==='flow-task-media-bound').flatMap(e=>Array.from(e.detail.mediaIds))};}
{
  const f=fixture();
  f.context.receiveGenerationData([rpc('jwpduf',payload('old-video')),rpc('eb1hJf',payload('new-video'))],f.process,true);
  assert.deepEqual(f.bound(),['new-video'],'a batched project-history frame cannot steal ownership before the real creation frame');
}
{
  const f=fixture();
  f.context.receiveGenerationData([rpc('ListMedia',{media:[{name:'old-video',mediaMetadata:{mediaStatus:{mediaGenerationStatus:'MEDIA_GENERATION_STATUS_SUCCESSFUL'}}}]}),
    rpc('eb1hJf',{media:[{name:'new-video',mediaMetadata:{mediaStatus:{mediaGenerationStatus:'MEDIA_GENERATION_STATUS_PENDING'}}}]})],f.process,true);
  assert.deepEqual(f.bound(),['new-video'],'only the creation RPC payload can establish IDs; completed history is not the new result');
}
{
  const f=fixture();f.context.receiveGenerationData([rpc('jwpduf',payload('old-video'))],f.process,true);
  assert.deepEqual(f.bound(),[],'a shape-compatible listing is not a creation response');
  const body=new URLSearchParams({'f.req':JSON.stringify([[['jwpduf',JSON.stringify({prompt:'text containing eb1hJf and StreamGenerateContent'}),null,'generic']]])}).toString();
  assert.equal(f.context.classifyRequest('https://flow.google.com/_/LabsFlowUi/data/batchexecute?rpcids=jwpduf',body).generate,false,
    'a prompt mentioning an operation ID cannot turn project history into a generation response');
  assert.equal(f.context.classifyRequest('https://flow.google.com/_/LabsFlowUi/data/batchexecute?rpcids=eb1hJf',body).generate,true);
}
{
  const f=fixture();f.context.receiveGenerationData({media:[{name:'old-video'},{name:'new-video'}]},f.process,true);
  assert.deepEqual(f.bound(),[],'an ambiguous creation payload cannot authorize choosing a historical result');
}
// Exercise the actual main batch wait loop, not only its separate DOM watcher.
(async()=>{
  const ui=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/injects/ui.js'),'utf8').replace(/\r\n/g,'\n');
  const binding={source:'generation_response',mediaIds:['new-video'],readyIds:[],urls:{}};
  const oldUrl='https://flow-content.google/video/old-video?Expires=9999999999&Signature=old';
  const context={window:{__lzxFlowTransport:{}},document:{querySelectorAll:()=>[{getAttribute:()=>oldUrl,closest:()=>({})}]},
    targetBatchContainer:null,binding,boundMediaIds:binding.mediaIds,discoveredVideoUrlsById:new Map(),console:{log(){}},
    isVideoGenerated:false,targetVideoUrl:null,isStaticShowcaseAsset:()=>false,isValidFlowVideoUrl:()=>true,
    mediaIdFromVideoUrl:value=>new URL(value).pathname.split('/').at(-1),wasVideoSeen:()=>false,saveState:async()=>{}};
  const start=ui.indexOf('          // 4. 校验全页面 DOM 视频'),end=ui.indexOf('          // 5. 视频生成就绪后',start);
  assert(start>=0&&end>start);vm.createContext(context);
  await vm.runInContext('(async()=>{'+ui.slice(start,end)+'})()',context);
  assert.deepEqual(binding.mediaIds,['new-video'],'a lazily loaded old video cannot be appended to an already trusted new binding');
  assert.deepEqual(binding.readyIds,[]);assert.equal(context.isVideoGenerated,false);assert.equal(context.targetVideoUrl,null);
  const newVideo={readyState:0,getAttribute:()=>oldUrl.replace('old-video','new-video'),closest:()=>({})};
  context.document.querySelectorAll=()=>[newVideo];
  await vm.runInContext('(async()=>{'+ui.slice(start,end)+'})()',context);
  assert.equal(context.isVideoGenerated,false,'setting a signed URL alone does not prove new video completion');
  newVideo.readyState=4;
  await vm.runInContext('(async()=>{'+ui.slice(start,end)+'})()',context);
  assert.equal(context.isVideoGenerated,true,'the exact new bound video may become ready');
  assert.deepEqual(binding.readyIds,['new-video']);
  console.log('Flow result ownership: mixed RPCs, old history, operation text and lazy old videos in the main batch loop cannot steal a new task; exact new result still completes.');
})().catch(error=>{console.error(error);process.exitCode=1;});
