const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
// Reuse the concrete DOM fixture and execute the shipped reference controller.
let fixtureCode=fs.readFileSync(path.join(__dirname,'flow_reference_reuse_test.cjs'),'utf8').split('(async () => {')[0];
fixtureCode=fixtureCode.replace('api.init({images:', 'const helpers={images:')
  .replace("  }, key => ['page_controls_wait_seconds','page_reference_reuse_wait_seconds'].includes(key) ? 100 : 1);",
    "  }; state.helpers=helpers; api.init(helpers,key => ['page_controls_wait_seconds','page_reference_reuse_wait_seconds'].includes(key) ? 100 : 1);")
  .replace('{window, document, location, setTimeout, URL}',
    '{window, document, location, setTimeout, URL, localStorage:{getItem:key=>state.persistent?.get(key) || null,setItem:(key,value)=>{state.persistent ||= new Map();state.persistent.set(key,value);}}}')
  .replace('return {state, location, api, context, ref, upload, task};','return {state, window, location, api, context, ref, upload, task};');
const env={require,__dirname,console,setTimeout,URL};vm.createContext(env);vm.runInContext(fixtureCode,env);
function observedFixture() {
  const f=env.fixture({stableNames:true});let reveal=false,dropAllowed=true;
  f.state.helpers.upload=async(paths,options)=>{
    if(!options.verifyOnly) {
      await options.beforeDispatch();
      if(!dropAllowed)return {failed:paths,not_dispatched:paths,errors:{file:'debugger attach failed before drop'}};
      f.state.uploads++;
      f.state.images.push({id:'uploaded-'+f.state.uploads,primaryMediaKey:'uploaded-'+f.state.uploads,displayName:'素材图片',src:'/image/uploaded-'+f.state.uploads});
    }
    if(reveal) {
      for(const image of f.state.images)image.displayName=f.ref(1).filename;
      return {skipped:paths,failed:[]};
    }
    return {failed:paths,errors:{file:'拖放已发出，素材名称尚未显示'}};
  };
  return {...f,reveal(){reveal=true;},rejectBeforeDrop(){dropAllowed=false;},allowDrop(){dropAllowed=true;}};
}
(async()=>{
  function legacyIntent(f) {
    const key='lzx_reference_upload_intents_v1',saved=JSON.parse(f.state.persistent.get(key));
    for(const value of Object.values(saved)){delete value.dispatch_tracking;value.at=Date.now()-3600000;}
    f.state.persistent.set(key,JSON.stringify(saved));
  }
  const poisoned=observedFixture(),upload=poisoned.state.helpers.upload;
  poisoned.state.helpers.upload=async(_paths,options)=>{await options.beforeDispatch();throw Error('old script interrupted after reservation');};
  await assert.rejects(poisoned.upload(1));legacyIntent(poisoned);
  poisoned.state.helpers.upload=upload;
  await assert.rejects(poisoned.upload(2));assert.equal(poisoned.state.uploads,1,'an old unsent reservation gets one bounded migration upload');
  await assert.rejects(poisoned.upload(3));assert.equal(poisoned.state.uploads,1,'migration cannot repeatedly drag the same image');
  const ambiguous=observedFixture();await assert.rejects(ambiguous.upload(1));legacyIntent(ambiguous);
  await assert.rejects(ambiguous.upload(2));assert.equal(ambiguous.state.uploads,1,'any new image ID blocks speculative replacement of an old uncertain drag');
  const active=observedFixture(),original=active.state.helpers.upload;
  active.state.helpers.upload=async(_paths,options)=>{await options.beforeDispatch();throw Error('old pending upload');};
  await assert.rejects(active.upload(1));legacyIntent(active);active.state.helpers.upload=original;
  active.window.__lzxFlowTransport={uploadActivity:()=>({active:true})};
  await assert.rejects(active.upload(2));assert.equal(active.state.uploads,0,'active upload processing blocks migration re-drag');
  active.window.__lzxFlowTransport.uploadActivity=()=>({active:false});
  await assert.rejects(active.upload(3));assert.equal(active.state.uploads,1,'idle legacy recovery gets one replacement');
  const modern=observedFixture(),modernUpload=modern.state.helpers.upload;
  modern.state.helpers.upload=async(_paths,options)=>{await options.beforeDispatch();throw Error('native delivery uncertain');};
  await assert.rejects(modern.upload(1));
  const modernKey='lzx_reference_upload_intents_v1',modernSaved=JSON.parse(modern.state.persistent.get(modernKey));
  for(const value of Object.values(modernSaved))value.at=Date.now()-3600000;
  modern.state.persistent.set(modernKey,JSON.stringify(modernSaved));modern.state.helpers.upload=modernUpload;
  await assert.rejects(modern.upload(2));assert.equal(modern.state.uploads,0,'new-version unknown delivery never uses legacy migration');
  const f=observedFixture();
  for(let retry=0;retry<4;retry++)await assert.rejects(f.upload(1),/上传未确认|未重复拖放/);
  assert.equal(f.state.uploads,1,'unknown completion cannot produce one new image per retry');
  assert.equal(f.state.images.length,1);
  f.reveal();const recovered=await f.upload(1);assert.equal(recovered.reference.media_id,'uploaded-1');
  assert.equal(f.state.uploads,1,'late exact filename recovers the original upload');
  f.location.href+='/edit/video-result';const edit=await f.upload(2);
  assert.equal(edit.reference.media_id,recovered.reference.media_id);assert.equal(f.state.uploads,1,'same-project edit navigation retains identity');

  const other=observedFixture();other.state.persistent=f.state.persistent;
  other.state.images=f.state.images.map(image=>({...image,displayName:'title hidden after reload'}));
  other.context.page_instance='new-document';
  const restored=await other.upload(3);assert.equal(restored.reference.media_id,'uploaded-1');assert.equal(other.state.uploads,0,'a new document reattaches the durable exact media ID');
  const pending=observedFixture();await assert.rejects(pending.upload(1));
  const nextTab=observedFixture();nextTab.state.persistent=pending.state.persistent;
  nextTab.context.page_instance='second-tab';await assert.rejects(nextTab.upload(2));
  assert.equal(nextTab.state.uploads,0,'a second same-project tab cannot redrag an unconfirmed image');

  const noDrop=observedFixture();noDrop.rejectBeforeDrop();await assert.rejects(noDrop.upload(1));
  noDrop.allowDrop();await assert.rejects(noDrop.upload(1));assert.equal(noDrop.state.uploads,1,'a proven failure before drop releases the latch');
  for(const reason of ['reset before native message','lease expired before native message']) {
    const aborted=observedFixture(),original=aborted.state.helpers.upload;
    aborted.state.helpers.upload=async(paths,options)=>{await options.beforeDispatch();
      throw Object.assign(Error(reason),{uploadDispatched:false});};
    await assert.rejects(aborted.upload(1),new RegExp(reason));
    aborted.state.helpers.upload=original;
    await assert.rejects(aborted.upload(2));
    assert.equal(aborted.state.uploads,1,'a pre-dispatch interruption must permit a real later upload');
    await assert.rejects(aborted.upload(3));assert.equal(aborted.state.uploads,1,'unknown completion remains latched');
  }
  const reset=observedFixture();await assert.rejects(reset.upload(1));
  await assert.rejects(reset.upload(2,undefined,{reference_reset:'explicit-replacement'}));
  await assert.rejects(reset.upload(2,undefined,{reference_reset:'explicit-replacement'}));
  assert.equal(reset.state.uploads,2,'an explicit reset permits one replacement, not one copy per retry');
  const mixed=env.fixture({stableNames:true});
  mixed.state.helpers.upload=async(paths,options)=>{await options.beforeDispatch();mixed.state.uploads++;
    mixed.state.images=[{id:'a-video',primaryMediaKey:'a-video',displayName:mixed.ref(1).filename,video:true},
      {id:'b-image',primaryMediaKey:'b-image',displayName:mixed.ref(1).filename}];return {uploaded:paths,failed:[]};};
  assert.equal((await mixed.upload(1)).reference.media_id,'b-image','duplicate filename recovery must select an explicit image, never a video poster');
  const clockCode=fs.readFileSync(path.join(__dirname,'flow_upload_wait_test.cjs'),'utf8');
  const clockEnv={require,__dirname,process,console};vm.createContext(clockEnv);
  vm.runInContext(clockCode.slice(0,clockCode.indexOf('(async()=>{')),clockEnv);
  const actual=clockEnv.fixture({arrives:15000});let dispatchChecks=0;
  const first=await actual.run({timeoutMs:6000,maxWaitMs:6000,beforeDispatch:()=>dispatchChecks++});
  assert.equal(first.failed.length,1);assert.equal(actual.stats().drags,1);assert.equal(dispatchChecks,1);
  const later=await actual.run({timeoutMs:15000,maxWaitMs:15000,verifyOnly:true,knownBeforeIds:[],beforeDispatch:()=>dispatchChecks++});
  assert.equal(later.uploaded.length,1);assert.equal(actual.stats().drags,1);assert.equal(dispatchChecks,1,'the real uploader checks late completion without issuing another native drop');
  const cancelled=clockEnv.fixture();let reserved=false;
  await assert.rejects(cancelled.run({beforeDispatch(){reserved=true;},operationGuard(){
    if(reserved)throw Error('reset immediately before native message');
  }}),failure=>failure.uploadDispatched===false);
  assert.equal(cancelled.stats().drags,0,'reset between intent reservation and send cannot upload');
  assert.equal(cancelled.stats().overlay,false);
  const expired=clockEnv.fixture();expired.transport.assertUi=()=>{throw Error('expired lease');};
  await assert.rejects(expired.run({beforeDispatch(){}}),failure=>failure.uploadDispatched===false);
  assert.equal(expired.stats().drags,0,'expired UI lease is explicitly a no-drop failure');

  const bg=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/background.js'),'utf8');
  const dragCode=bg.slice(bg.indexOf('const dragLanes ='),bg.indexOf('async function dispatchNativeClick('));
  const dragContext={Date,console:{log(){},warn(){}},setTimeout:fn=>fn(),chrome:{debugger:{async attach(){throw Error('no current window');}}}};
  vm.createContext(dragContext);vm.runInContext(dragCode,dragContext);
  await assert.rejects(dragContext.dispatchNativeFileDrag(7,['C:/ref/image.png'],1,1),error=>error.uploadDispatched===false);
  dragContext.chrome.debugger={async attach(){},async detach(){},async sendCommand(_target,method,params){if(method==='Input.dispatchDragEvent' && params.type==='drop')throw Error('connection lost after drop started');}};
  await assert.rejects(dragContext.dispatchNativeFileDrag(7,['C:/ref/image.png'],1,1),error=>error.uploadDispatched===true,'an uncertain drop must retain the duplicate guard');
  console.log('Upload intents: unknown completion, late identity, same-project edit, document/tab handoff, proven no-drop failure and explicit reset passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
