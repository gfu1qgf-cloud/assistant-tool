// Integrate the shipped page dispatcher, reference identity and generated uploader.
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const fixtureCode=fs.readFileSync(path.join(__dirname,'flow_passive_ui_wait_test.cjs'),'utf8').split('(async()=>{')[0];
const env={require,__dirname,console,queueMicrotask,process,URL};vm.createContext(env);vm.runInContext(fixtureCode,env);
const built=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/injects/ui.js'),'utf8');
const first=built.indexOf('  async function uploadPathsToFlow('),last=built.indexOf('\n  function ',first+10);
const referenceCode=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/reference.js'),'utf8');
async function fixture({cancel=false,visual=false}={}) {
  const f=env.fixture(),ctx=f.context;let drags=0,mounted='',picker=false,overlay=false,interleaved=false,captures=0;
  let cancelNext=cancel;
  const upload_id='a'.repeat(32),sha256='b'.repeat(64),filename='IMG_'+sha256+'.png',file='C:/refs/'+filename;
  const reference={upload_id,sha256,filename,path:file};
  const image=id=>({src:'/image/'+id,naturalWidth:100,naturalHeight:100,
    getBoundingClientRect:()=>({left:50,top:50,right:150,bottom:150,width:100,height:100}),
    getAttribute:()=>null,querySelectorAll:()=>[]});
  const trigger={open:true},start={textContent:'开始',getAttribute:()=>null,
    querySelector:s=>s.includes('button')?trigger:mounted?image(mounted):null,
    querySelectorAll:s=>s==='button'?[trigger]:mounted?[image(mounted)]:[]};
  const bar={querySelectorAll:()=>[start]};
  const tile={tagName:'FLOW-IMAGE-TILE',textContent:visual?'素材图片':filename,
    getAttribute:k=>k==='data-media-id'?'exact-upload':k==='title'&&!visual?filename:null,
    closest:()=>null,matches:()=>true,querySelector:s=>s==='img'?image('exact-upload'):null,querySelectorAll:s=>s==='mat-icon'?[]:[image('exact-upload')]};
  const panel={offsetWidth:400,offsetHeight:200,closest:()=>null,
    querySelectorAll:s=>s==='button'?[]:[tile]};
  ctx.document.querySelector=s=>s.includes('flow-ingredient-bar')?bar:null;
  ctx.document.querySelectorAll=s=>s.startsWith('flow-account-panel')?[]:
    s.startsWith('flow-grid-tile-container')||s.startsWith('flow-image-tile')?f.clock()>(visual?10000:31000)?[tile]:[]:
    s.startsWith('.cdk-overlay-pane')&&picker?[panel]:[];
  Object.assign(ctx,{innerWidth:800,innerHeight:600,uploadInFlight:false,UPLOAD_BATCH_SIZE:1,getFilenameFromPath:p=>p.split('/').at(-1),
    getProjectUploadEntries:()=>({}),isStrictImageMatch:(a,b)=>a===b,isRegisteredInFlow:()=>false,
    getLoadedPageImages:()=>f.clock()>(visual?10000:31000)?[{id:'exact-upload',primaryMediaKey:'exact-upload',displayName:visual?'name not exposed':filename}]:[],
    setOverlayHiddenForUpload:value=>{overlay=value;},snapshotImageKeys:()=>new Set(),registerUploads(){},newTilesConfirmed:()=>false,
    findFlowDropPoint:()=>{f.api.assertUi();return{x:100,y:100};},
    sendToExtension:async action=>{assert.equal(action,'nativeFileDrag');f.api.assertUi();drags++;return{ok:true};},
    delay:async ms=>{
      if(ms===2000||ms===1500)assert(!f.api.uiMutexActive(),'network delays must not occupy UI');
      await f.wait(ms);
      if(!interleaved&&ms===2000) {
        interleaved=true;const count=f.requests();
        await f.receive({action:'get_points',request_id:'d'.repeat(32),params:{}});
        assert.equal(f.requests(),count,'a concurrent page command is rejected before it can take the upload lease');
      }
      if(cancelNext&&ms===2000){cancelNext=false;await f.receive({action:'cancel_operation',target_request_id:'c'.repeat(32)});}
    }});
  vm.runInContext(built.slice(first,last),ctx);
  vm.runInContext(referenceCode,ctx);
  const helpers={images:()=>ctx.getLoadedPageImages(),account:async()=>({email:'fixture@example.test'}),
    cleanup:async()=>{f.api.assertUi();picker=false;},upload:ctx.uploadPathsToFlow,
    click(node){f.api.assertUi();if(node.open)picker=true;else if(node===tile){mounted='exact-upload';picker=false;}},
    visual:async()=>{assert(f.api.uiMutexActive());captures++;return{ok:true,match:{media_id:'exact-upload'}};}};
  f.api.attach(async()=>{},()=>({tasks:[]}),helpers);
  await f.receive({action:'configure_browser_options',values:{ui_mutex:true}});
  const run=(request='c'.repeat(32))=>f.receive({action:'upload_images',request_id:request,params:{upload_id,reference,paths:[file],
    ...(visual?{visual_reference:{upload_id}}:{})}});
  return{...f,run,stats:()=>({drags,mounted,overlay,interleaved,captures})};
}
(async()=>{
  const normal=await fixture();await normal.run();
  const reply=normal.events.find(e=>e.action==='upload_images_response');assert.equal(reply.success,true,reply.error);
  assert.equal(normal.stats().drags,1);assert.equal(normal.stats().mounted,'exact-upload');assert(!normal.stats().overlay);
  const releases=normal.events.filter(e=>e.type==='ui_mutex_command'&&e.release);
  assert(releases.some(e=>e.passive_wait));assert.equal(normal.requests(),2,'one drag lease and one mount lease, not a lease per poll');
  assert(normal.events.some(e=>e.code==='FLOW_BATCH_BUSY'));
  const cancelled=await fixture({cancel:true});await cancelled.run();
  const failed=cancelled.events.find(e=>e.action==='upload_images_response');assert.equal(failed.code,'FLOW_OPERATION_CANCELLED');
  assert.equal(cancelled.stats().drags,1);assert(!cancelled.stats().overlay);assert(!cancelled.api.uiMutexActive());
  assert.equal(cancelled.requests(),1,'cancelled passive work never reacquires or replays the drag');
  await cancelled.wait(40000);await cancelled.run('e'.repeat(32));
  assert.equal(cancelled.events.filter(e=>e.action==='upload_images_response').at(-1).success,true);
  assert.equal(cancelled.stats().drags,1,'a later retry reuses the original completed upload');
  const visual=await fixture({visual:true});await visual.run();
  const confirmed=visual.events.find(e=>e.action==='upload_images_response');assert.equal(confirmed.success,true,confirmed.error);
  assert.equal(visual.stats().captures,1);assert.equal(visual.stats().drags,1);
  assert.equal(visual.requests(),3,'only screenshot and mounting reacquire the lock');
  assert.equal(confirmed.data.reference.verification_method,'local_screenshot');
  console.log('PASS actual page→reference→uploader: background polling, exact mount/screenshot leases, concurrent rejection, cancellation and retry without duplicate drag');
})().catch(e=>{console.error(e);process.exitCode=1;});
