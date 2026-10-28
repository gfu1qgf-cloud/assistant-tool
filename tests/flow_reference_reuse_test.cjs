// Exercise the real reference controller with exact synthetic media IDs only.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/lzx-reference.js'), 'utf8');
function fixture({contentNames=false,stableNames=false}={}) {
  const state = {uploads:0, cleanups:0, mounted:'old-unrelated', picker:false, images:[], nextFailure:false};
  const location = {href:'https://flow.google.com/project/offline-one'};
  const img = id => ({src:'/image/' + id, getAttribute:key => key === 'data-media-id' ? id : null, querySelectorAll:() => []});
  const empty = {textContent:'End', getAttribute:() => 'End', querySelector:() => null, querySelectorAll:() => []};
  const trigger = {pick:true}, remove = {remove:true, getAttribute:() => 'Remove', textContent:'Remove'};
  const start = {textContent:'Start', getAttribute:key => key === 'aria-label' ? 'Start' : null,
    querySelector:selector => selector.includes('button') ? trigger : state.mounted ? img(state.mounted) : null,
    querySelectorAll:selector => selector === 'button' ? state.mounted ? [remove, trigger] : [trigger] : state.mounted ? [img(state.mounted)] : []};
  const bar = {querySelectorAll:() => [start, empty]};
  const panel = {offsetWidth:400, offsetHeight:200, closest:() => null,
    querySelectorAll:selector => selector === 'button' ? [] : state.images.map(image => ({
      choose:image.primaryMediaKey,getAttribute:key => key === 'data-media-id' ? image.primaryMediaKey : null,
      querySelectorAll:() => [], querySelector:selector => selector==='button,img'?({choose:image.primaryMediaKey}):null}))};
  const tiles=()=>state.images.map(image=>({textContent:image.displayName,
    matches:()=>!image.video,closest:()=>image.video?{}:null,
    getAttribute:key=>key==='data-media-id'?image.primaryMediaKey:key==='title'?image.displayName:key==='data-media-type'?image.video?'video':'image':null,
    querySelector:selector=>selector==='img'?img(image.primaryMediaKey):null,
    querySelectorAll:selector=>selector==='mat-icon'?[]:[]}));
  const document = {querySelector:() => bar, querySelectorAll:selector => selector.startsWith('flow-')?tiles():state.picker ? [panel] : []};
  const window = {}; window.top = window;
  vm.runInNewContext(source, {window, document, location, setTimeout, URL});
  const api = window.__lzxReferenceIdentity;
  api.init({images:() => state.images,
    async upload(paths) {
      state.uploads++;
      if (state.nextFailure) return {failed:paths, errors:{file:'offline upload failure'}};
      const media = 'exact-upload-' + state.uploads;
      state.images.push({id:media, primaryMediaKey:media, displayName:paths[0].split(/[\\/]/).at(-1), src:'/image/' + media});
      return {uploaded:paths, failed:[]};
    },
    async cleanup() {state.cleanups++;state.picker=false;},
    click(node) {if (node.pick) state.picker=true; else if (node.remove) state.mounted='';
      else if (node.choose) {state.mounted=node.choose;state.picker=false;}}
  }, key => ['page_controls_wait_seconds','page_reference_reuse_wait_seconds'].includes(key) ? 100 : 1);
  const context = {account:'one@example.test', page_instance:'document-one'};
  const ref = (serial, hash='b'.repeat(64)) => {
    const upload_id = serial.toString(16).padStart(32, '0'), filename = stableNames?`IMG_${hash}.png`:contentNames ? `IMG_${hash}_${upload_id}.png` : `LZXREF${upload_id}.png`;
    return {upload_id, filename, path:'C:\\frozen\\' + filename, sha256:hash};
  };
  const upload = (serial, hash, extra={}, scope=context) => {
    const reference = ref(serial, hash);
    return api.upload({reference, paths:[reference.path], upload_id:reference.upload_id, ...extra}, scope);
  };
  const task = data => ({reference:data.reference, local_image_path:data.reference.path,
    download_path:`FlowBridge/${data.upload_id}/result.mp4`});
  return {state, location, api, context, ref, upload, task};
}
(async () => {
  {
    const f=fixture({stableNames:true}),first=await f.upload(1);
    f.api.invalidate();const second=await f.upload(2);
    assert.equal(second.reference.filename,first.reference.filename);
    assert.notEqual(second.reference.upload_id,first.reference.upload_id);
    assert.equal(second.reference.media_id,first.reference.media_id);assert.equal(f.state.uploads,1);
    assert.equal(second.reused,true,'a fresh page controller must reuse the visible fixed-hash image before dragging');
    assert.throws(()=>f.api.resolve({...f.task(second),reference:first.reference}),/凭据不一致/);
    f.api.invalidate();f.state.images.push({...f.state.images[0],id:'duplicate-image',primaryMediaKey:'duplicate-image'});
    const duplicate=await f.upload(3);assert.equal(duplicate.reused,true);assert.equal(f.state.uploads,1,'equivalent full-hash copies do not require another upload');
    const forced=await f.upload(4,undefined,{reference_reset:'explicit-reset'});
    assert.equal(forced.reused,false);assert.equal(f.state.uploads,2);
  }
  {
    const f=fixture({stableNames:true}),hash='b'.repeat(64);
    f.state.images=[{id:'previous-id',primaryMediaKey:'previous-id',displayName:`IMG_${hash}_${'a'.repeat(32)}.png`}];
    const reused=await f.upload(2);assert.equal(reused.reference.media_id,'previous-id');assert.equal(f.state.uploads,0,'old attempt suffix is ignored when matching the full content hash');
    const video=fixture({stableNames:true});video.state.images=[{id:'video-poster',primaryMediaKey:'video-poster',displayName:`IMG_${hash}.png`,video:true}];
    const uploaded=await video.upload(2);assert.notEqual(uploaded.reference.media_id,'video-poster');assert.equal(video.state.uploads,1,'video posters never count as existing reference images');
  }
  {
    const f=fixture({contentNames:true});assert.equal(f.api.sha256_names,true);
    const first=await f.upload(1);
    for(let serial=2;serial<=10;serial++) {
      const next=await f.upload(serial);
      assert.equal(next.reference.sha256,first.reference.sha256);
      assert(next.reference.filename.includes(first.reference.sha256));
      assert.notEqual(next.reference.upload_id,first.reference.upload_id);
      assert.equal(next.reference.media_id,first.reference.media_id);assert.equal(next.reused,true);
      f.api.assert(f.api.resolve(f.task(next)));
    }
    assert.equal(f.state.uploads,1,'permanent IDs must not cause one upload per clip');
    const before=f.state.uploads, wrong=f.ref(11);
    wrong.sha256='c'.repeat(64);
    await assert.rejects(f.api.upload({reference:wrong,paths:[wrong.path],upload_id:wrong.upload_id},f.context),/独立副本标识/);
    assert.equal(f.state.uploads,before,'a hash in the filename must match the actual proof');
    const second=await f.upload(12,'c'.repeat(64));assert.notEqual(second.reference.media_id,first.reference.media_id);
    const back=await f.upload(13);assert.equal(back.reference.media_id,first.reference.media_id);
    assert.equal(f.state.uploads,2,'switching A -> B -> A must recover the original exact A asset');
    f.state.mounted=second.reference.media_id;
    assert.throws(()=>f.api.assert(f.api.resolve(f.task(back))),/首帧已变化/);
  }
  {
    const f=fixture(); const first=await f.upload(1);
    for(let serial=2;serial<=10;serial++) {
      f.state.mounted='';f.state.images[0].displayName='generic title after repaint';
      const next=await f.upload(serial);
      assert.equal(next.reused,true);assert.equal(next.reference.media_id,first.reference.media_id);
      assert.equal(next.reference.upload_id,f.ref(serial).upload_id);
      assert.equal(next.reference.source_upload_id,first.upload_id);
      assert(f.api.verify(f.api.resolve(f.task(next))));
      assert.throws(()=>f.api.resolve({...f.task(next),reference:first.reference}),/凭据不一致/);
    }
    assert.equal(f.state.uploads,1,'ten clips from identical bytes must perform only one native upload');
    f.state.mounted='wrong-manual-image';const next=await f.upload(11);
    assert.equal(f.state.uploads,1,'an altered first frame should reattach the exact cached media ID');
    assert.equal(f.state.mounted,first.reference.media_id);f.api.assert(f.api.resolve(f.task(next)));
    const changed=await f.upload(12,'c'.repeat(64));
    assert.equal(f.state.uploads,2);assert.notEqual(changed.reference.media_id,first.reference.media_id);
  }
  {
    const f=fixture();await f.upload(1);
    const forced=await f.upload(2,undefined,{reference_reset:'manual-reset-one'});
    assert.equal(forced.reused,false);assert.equal(f.state.uploads,2);
    await f.upload(3,undefined,{reference_reset:'manual-reset-one'});assert.equal(f.state.uploads,2);
    await f.upload(4);assert.equal(f.state.uploads,2);
    await f.upload(5,undefined,{reference_reset:'manual-reset-two'});assert.equal(f.state.uploads,3);
  }
  {
    const f=fixture();const first=await f.upload(1);
    f.context.account='other@example.test';await f.upload(2);assert.equal(f.state.uploads,2);
    f.context.page_instance='document-two';await f.upload(3);assert.equal(f.state.uploads,2,'a reconnected page must reverify the original media ID instead of uploading again');
    f.location.href='https://flow.google.com/project/offline-two';
    assert.throws(()=>f.api.resolve(f.task(first)),/凭据不一致/);
    await f.upload(4);assert.equal(f.state.uploads,3);
    const refreshed=fixture();await refreshed.upload(5);assert.equal(refreshed.state.uploads,1);
  }
  {
    const f=fixture();await f.upload(1);f.state.images=[];f.state.mounted='';
    await assert.rejects(f.upload(2),/图片已确认上传.*不重复上传/);
    assert.equal(f.state.uploads,1,'a missing or virtualized cached card is not evidence for another drag');
    await assert.rejects(f.upload(2),/图片已确认上传.*不重复上传/);
    assert.equal(f.state.uploads,1);
    const restored=await f.upload(3,undefined,{reference_reset:'user-explicit-reset'});
    assert.equal(restored.reused,false);assert.equal(f.state.uploads,2);assert.equal(f.state.picker,false);
    assert(f.api.verify(f.api.resolve(f.task(restored))),'explicit reset can replace a deleted cached image');
  }
  {
    const f=fixture(),first=await f.upload(1),images=[...f.state.images];
    f.state.images=[];f.state.mounted='';await assert.rejects(f.upload(2),/图片已确认上传/);
    f.state.images=images;
    const recovered=await f.upload(2);
    assert.equal(recovered.reused,true);assert.equal(recovered.reference.source_upload_id,first.upload_id);
    f.state.mounted='';await f.upload(3);
    assert.equal(f.state.uploads,1,'a transient picker failure must not turn all later clips into new uploads');
  }
  {
    const f=fixture();await f.upload(1);f.state.images=[];f.state.mounted='';f.state.nextFailure=true;
    await assert.rejects(f.upload(2),/图片已确认上传/);
    assert.equal(f.state.uploads,1);
    await assert.rejects(f.upload(3,undefined,{reference_reset:'user-explicit-reset'}),/上传未确认/);
    assert.throws(()=>f.api.resolve(f.task({upload_id:f.ref(2).upload_id,reference:{...f.ref(2),media_id:'exact-upload-1'}})),/凭据不一致/);
  }
  {
    const f=fixture();await f.upload(1,undefined,{},{});await f.upload(2,undefined,{},{});
    assert.equal(f.state.uploads,2,'a caller without an account and page scope cannot reuse old assets');
    const g=fixture();g.context.guard=async()=>{g.api.invalidate();throw Object.assign(Error('account changed'),{code:'FLOW_ACCOUNT_CHANGED'});};
    await assert.rejects(g.upload(1),/account changed/);assert.equal(g.state.uploads,0);
  }
  {
    const f=fixture();
    for(let serial=1;serial<=65;serial++)await f.upload(serial,serial.toString(16).padStart(64,'0'));
    await f.upload(66,(1).toString(16).padStart(64,'0'));
    assert.equal(f.state.uploads,66,'the durable upload ledger must stay bounded at 64 records');
  }
  console.log('Flow reference reuse: one upload for ten clips, exact per-attempt proofs, changed image/account/page/project isolation, manual reset, removed-asset recovery and bounded cache passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
