// Execute the actual bounded foreground phase and scoped native cancellation.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const warm=source.slice(source.indexOf('async function warmDownloadPage('),source.indexOf('async function freshDownloadUrl('));
const park=source.slice(source.indexOf('async function parkNativeDownloads('),source.indexOf('async function retryBoundDownload('));
const project='https://flow.google.com/project/ours',batch='a'.repeat(32),id='known';
async function phase({mutex=true,denied=false,navigate=false,throws=false,lazy=false,foreign=false,image=false,duplicate=false,unidentified=false,wrapper=false}={}){
  let now=0,held=false,acquired=0,released=0,focused=0,hover=0,plays=0,pauses=0,injected=0;
  const kinds=[];
  const video={src:foreign?'https://flow-content.google/video/other':lazy?'':`https://flow-content.google/video/${id}`,paused:true,muted:false,play(){plays++;return Promise.resolve();},pause(){pauses++;}};
  const thumbnail={src:'https://flow-content.google/image/different-thumbnail',dispatchEvent(event){kinds.push(event.type);if(event.type==='pointerenter' && lazy)video.src=`https://flow-content.google/video/${id}`;}};
  const tile={parentElement:{closest(){return wrapper?{getAttribute:name=>name==='data-tile-id'?id:null}:null;}},
    closest(){return image?{}:null;},getAttribute(name){return !unidentified && !wrapper && name==='data-media-id'?id:null;},
    querySelectorAll(selector){return selector==='video,video source'?[video]:selector==='img'?[thumbnail]:[];},
    querySelector(selector){return selector==='video'?video:selector==='img'?thumbnail:null;},
    getBoundingClientRect(){return{left:0,top:0,width:100,height:100};},dispatchEvent(event){assert(held||!mutex);kinds.push(event.type);hover++;}};
  const context={crypto,URL,connectionEpoch:1,config:{project},browserOptions:{ui_mutex:mutex},
    Date:{now:()=>now},setTimeout,clearTimeout,
    sameProjectTab:t=>t.url===project,
    async acquireUiLease(tab,operation,token,attempt){acquired++;assert.equal(attempt,batch);assert.equal(operation,'retry_download');held=!denied;return{ok:!denied};},
    releaseUiLease(){released++;held=false;},async readyWorkTab(){focused++;},
    chrome:{tabs:{async get(){return{url:navigate?'https://example.test':project};}},scripting:{async executeScript({func,args}){
      injected++;assert(held||!mutex);if(throws)throw Error('injection failure');
      class PageEvent{constructor(type){this.type=type;}}
      const world={URL,Date:{now:()=>now},location:{href:project},window:{},MouseEvent:PageEvent,PointerEvent:PageEvent,
        document:{querySelectorAll:()=>duplicate?[tile,tile]:[tile]},setTimeout:fn=>{now+=100;fn();}};
      vm.createContext(world);world.args=args;await vm.runInContext(`(${func.toString()})(...args)`,world);return[];
    }}}};
  vm.createContext(context);vm.runInContext(warm,context);
  const run=context.warmDownloadPage(7,batch,id,{});
  if(denied||navigate||throws)await assert.rejects(run);else await run;
  assert.equal(released,1);assert(!held);assert.equal(acquired,mutex?1:0);
  if(!denied&&!navigate&&!throws){assert(now>=3500);
    if(foreign||image||duplicate||unidentified){assert.equal(hover,0);assert.equal(plays,0);}
    else{assert(hover>0);assert.equal(plays,1);assert(pauses>0);assert.equal(video.muted,false);assert(kinds.includes('pointerenter'));assert(kinds.includes('mouseenter'));}
    assert(!kinds.some(kind=>/click|down|up|wheel/.test(kind)),'preview must not click or scroll');
  }
  if(denied||navigate)assert.equal(injected,0);
  assert.equal(focused,mutex?0:1);
}
async function cancel(){
  const calls=[],events=[];const other='b'.repeat(32);
  const rows={1:{batch,mediaId:id,submissionId:'s',download_path:`FlowBridge/${batch}/result.mp4`},
    2:{batch:other,mediaId:id,submissionId:'s',download_path:`FlowBridge/${other}/result.mp4`},
    3:{batch,mediaId:id,submissionId:'s',download_path:`FlowBridge/${batch}/result.mp4`},
    4:{batch,mediaId:id,submissionId:'s',download_path:`FlowBridge/${batch}/result.mp4`},
    5:{batch,mediaId:id,submissionId:'s',download_path:`FlowBridge/${batch}/result.mp4`}};
  const context={downloadParkProgress:new Map(),local:async()=>({lzxDownloads:rows}),send:m=>events.push(m),chrome:{downloads:{
    async search({id}){return[{state:id===4?'complete':'in_progress',filename:id===3?'C:/Downloads/personal.mp4':`C:/Downloads/FlowBridge/${batch}/${id===5?'result (1).mp4':'result.mp4'}`}];},
    async cancel(id){calls.push(id);}}}};
  vm.createContext(context);vm.runInContext(park,context);
  await context.parkNativeDownloads({request_id:'park',params:{batch_id:batch}});
  assert.deepEqual(calls,[1,5]);assert.equal(events[0].data.cancelled,2);
  await assert.rejects(context.parkNativeDownloads({params:{batch_id:'bad'}}));
  calls.length=0;let bytes=10;
  context.local=async()=>({lzxDownloads:{1:rows[1]}});
  context.chrome.downloads.search=async()=>[{state:'in_progress',bytesReceived:bytes,filename:`C:/Downloads/FlowBridge/${batch}/result.mp4`}];
  await context.parkNativeDownloads({params:{batch_id:batch}});assert.equal(calls.length,0);
  bytes=20;await context.parkNativeDownloads({params:{batch_id:batch}});assert.equal(calls.length,0,'a progressing download must remain intact');
  await context.parkNativeDownloads({params:{batch_id:batch}});assert.deepEqual(calls,[1],'only stalled bytes authorize cancellation');
}
(async()=>{await phase();await phase({mutex:false});await phase({denied:true});await phase({navigate:true});await phase({throws:true});
  await phase({lazy:true});await phase({lazy:true,wrapper:true});await phase({lazy:true,mutex:false});await phase({foreign:true});await phase({image:true});await phase({duplicate:true});await phase({lazy:true,unidentified:true});await cancel();
  assert(!/dispatchMouseEvent|SetCursorPos|scrollIntoView|\.click\(/.test(warm),'no native mouse movement, clicking or scrolling');
  console.log('Bounded download UI lease, release on errors, exact video hover/play and managed-only cancellation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
