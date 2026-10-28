// Exercise the assembled uploader, not a copied implementation, with a virtual clock.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname,'..');
const read=file=>fs.readFileSync(path.join(root,file),'utf8');
const page=read('browser_extensions/flow_adapter/page.js'),built=read('browser_extensions/flow_bridge/injects/ui.js');
const start=built.indexOf('  async function uploadPathsToFlow('),end=built.indexOf('\n  // ----------------------------------------------------',start);
assert(start>0&&end>start);
const filename='LZXREF'+'a'.repeat(32)+'.png',file='C:/reference/'+filename;
function indicator(text,options={}) {
  return {tagName:options.tag || 'DIV',className:options.className || '',textContent:text,
    getAttribute:key=>key==='aria-valuenow'?options.progress ?? null:key==='aria-label'?text:null,
    getClientRects:()=>options.hidden?[]:[{}],
    closest(selector) {if(options.video&&selector.includes('flow-video-tile'))return this;return null;}};
}
function fixture({arrives=Infinity,placeholder=Infinity,nodes=()=>[],activity,unrelated=false,managed=true}={}) {
  let now=0,drags=0,overlay=false;
  const window={postMessage(){},addEventListener(){}};window.top=window;
  const document={documentElement:null,addEventListener(){},querySelector:()=>null,querySelectorAll:()=>nodes(now)};
  const ctx={window,document,Date:{now:()=>now},location:{origin:'https://flow.google.com',href:'https://flow.google.com/project/upload'},
    console:{log(){},warn(){}},setInterval(){},setTimeout(fn,ms){now+=ms;fn();},
    delay:async ms=>{now+=ms;},getFilenameFromPath:p=>p.split('/').at(-1),getProjectUploadEntries:()=>({}),
    isStrictImageMatch:(a,b)=>a===b,isRegisteredInFlow:()=>false,setOverlayHiddenForUpload:v=>overlay=v,
    snapshotImageKeys:()=>new Set(),findFlowDropPoint:()=>({x:100,y:100}),
    sendToExtension:async action=>{assert.equal(action,'nativeFileDrag');drags++;return {ok:true};},
    registerUploads(){},newTilesConfirmed:()=>unrelated,UPLOAD_BATCH_SIZE:1,
    getLoadedPageImages:()=>{
      const images=unrelated?[{displayName:'other.png',primaryMediaKey:'old-media'}]:[];
      if(now>=arrives)images.push({displayName:filename,primaryMediaKey:'exact-current-media'});
      else if(now>=placeholder)images.push({displayName:filename,primaryMediaKey:'img_placeholder'});
      return images;
    }};
  vm.createContext(ctx);vm.runInContext(page,ctx);
  const transport=window.__lzxFlowTransport;
  if(activity)transport.uploadActivity=()=>activity(now);
  if(!managed)delete window.__lzxFlowTransport;
  vm.runInContext('let uploadInFlight=false;\n'+built.slice(start,end),ctx);
  return {run:opts=>ctx.uploadPathsToFlow([file],opts||{timeoutMs:60000,maxWaitMs:180000}),
    stats:()=>({now,drags,overlay}),transport};
}
(async()=>{
  const slow=fixture({arrives:78000,nodes:()=>[indicator('Uploading image',{className:'upload-progress'})]});
  const cancelled=fixture();
  await assert.rejects(cancelled.run({timeoutMs:60000,maxWaitMs:180000,operationGuard(){
    if(cancelled.stats().now>3000)throw Error('cancelled by reset');
  }}),/cancelled by reset/);
  assert(cancelled.stats().now<7000,'cooperative cancellation must leave the upload poll within one short interval');
  assert.equal(cancelled.stats().overlay,false,'cancelled upload always removes temporary UI');
  const uploaded=await slow.run();assert.equal(uploaded.uploaded[0],file);assert.equal(slow.stats().drags,1);
  assert(slow.stats().now>=78000,'a slow upload outlives the old fixed 45 second boundary');
  const placeholder=fixture({placeholder:10000,arrives:85000,nodes:()=>[indicator('正在上传图片')]});
  assert.equal((await placeholder.run()).uploaded[0],file);
  assert(placeholder.stats().now>=85000,'a temporary image ID must not confirm completion');
  const quietPlaceholder=fixture({placeholder:10000,arrives:125000});
  assert.equal((await quietPlaceholder.run()).uploaded[0],file,'the exact pending image remains in processing even without a visible progress label');
  const progressing=fixture({arrives:110000,activity:now=>({active:false,stamp:String(Math.floor(now/35000))})});
  assert.equal((await progressing.run()).uploaded[0],file,'real progress resets only the idle clock');
  const stalled=fixture();const failure=await stalled.run();assert.equal(failure.failed[0],file);
  assert.match(failure.errors[file],/持续无进展/);assert(stalled.stats().now>=60000&&stalled.stats().now<70000);
  assert.equal(stalled.stats().drags,1);assert.equal(stalled.stats().overlay,false);
  const forever=fixture({nodes:()=>[indicator('上傳中',{className:'upload-progress'})]});
  const bounded=await forever.run();assert.match(bounded.errors[file],/最长等待/);
  assert(forever.stats().now>=180000&&forever.stats().now<185000);assert.equal(forever.stats().drags,1);
  const explicit=fixture({nodes:now=>now>15000?[indicator('图片上传失败')]:[]});
  assert.match((await explicit.run()).errors[file],/网页明确提示/);assert(explicit.stats().now<30000);
  const staleError=fixture({arrives:40000,nodes:()=>[indicator('图片上传失败')]});
  assert.equal((await staleError.run()).uploaded[0],file,'a failure toast present before this drag cannot fail the new upload');
  const old=fixture({unrelated:true});assert.equal((await old.run()).failed[0],file,'unrelated new tiles never confirm a managed image');
  const existing=fixture({arrives:0});assert.equal((await existing.run()).skipped[0],file);assert.equal(existing.stats().drags,0);
  const visual=fixture();let captures=0;
  const confirmed=await visual.run({timeoutMs:60000,maxWaitMs:180000,visualFallback:async()=>{
    captures++;return {displayName:filename,primaryMediaKey:'unique-visual-image'};}});
  assert.equal(confirmed.visual_matches[0].image.primaryMediaKey,'unique-visual-image');
  assert.equal(confirmed.uploaded[0],file);assert.equal(captures,1);assert.equal(visual.stats().drags,1);
  const noVisual=fixture();let attempts=0;
  await noVisual.run({timeoutMs:60000,maxWaitMs:180000,visualFallback:async()=>{attempts++;return null;}});
  assert.equal(attempts,3,'screenshot fallback is bounded and never loops indefinitely');
  const deferred=fixture({activity:()=>({active:true})});let realCaptures=0;
  const lateVisual=await deferred.run({timeoutMs:60000,maxWaitMs:180000,visualFallback:async()=>{
    if(deferred.stats().now<110000)return {pending:true};
    realCaptures++;return {displayName:filename,primaryMediaKey:'late-visual-image'};
  }});
  assert.equal(lateVisual.uploaded[0],file);assert.equal(realCaptures,1,'waiting for a visible completed image must not exhaust the screenshot budget');
  const ignored=fixture({nodes:()=>[indicator('Uploading image',{video:true}),indicator('正在上传图片',{hidden:true}),indicator('Generating video 45%')]});
  assert.equal(ignored.transport.uploadActivity([file]).active,false);
  await ignored.run();assert(ignored.stats().now<70000,'hidden and video-generation spinners do not extend the image wait');
  const legacy=fixture({managed:false});await legacy.run({timeoutMs:10000,maxWaitMs:180000});
  assert(legacy.stats().now<15000,'unmanaged upstream waiting retains its original bound');
  console.log('Upload waiting: slow progress, pending media IDs, idle timeout, hard cap, explicit failure, exact identity and one drag per attempt passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
