// Exercise the real reference controller with exact synthetic media IDs only.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/lzx-reference.js'), 'utf8');
function fixture() {
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
      getAttribute:key => key === 'data-media-id' ? image.primaryMediaKey : null,
      querySelectorAll:() => [], querySelector:() => ({choose:image.primaryMediaKey})}))};
  const document = {querySelector:() => bar, querySelectorAll:() => state.picker ? [panel] : []};
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
  }, key => key === 'page_controls_wait_seconds' ? 15 : 1);
  const context = {account:'one@example.test', page_instance:'document-one'};
  const ref = (serial, hash='b'.repeat(64)) => {
    const upload_id = serial.toString(16).padStart(32, '0'), filename = `LZXREF${upload_id}.png`;
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
    const f=fixture(); const first=await f.upload(1);
    for(let serial=2;serial<=10;serial++) {
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
    f.context.page_instance='document-two';await f.upload(3);assert.equal(f.state.uploads,3);
    f.location.href='https://flow.google.com/project/offline-two';
    assert.throws(()=>f.api.resolve(f.task(first)),/凭据不一致/);
    await f.upload(4);assert.equal(f.state.uploads,4);
    const refreshed=fixture();await refreshed.upload(5);assert.equal(refreshed.state.uploads,1);
  }
  {
    const f=fixture();await f.upload(1);f.state.images=[];f.state.mounted='';
    const restored=await f.upload(2);
    assert.equal(restored.reused,false);assert.equal(f.state.uploads,2);assert.equal(f.state.picker,false);
    assert(f.api.verify(f.api.resolve(f.task(restored))),'a deleted cached image must get a newly verified upload');
  }
  {
    const f=fixture();await f.upload(1);f.state.images=[];f.state.mounted='';f.state.nextFailure=true;
    await assert.rejects(f.upload(2),/上传未确认/);
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
    for(let serial=1;serial<=17;serial++)await f.upload(serial,serial.toString(16).padStart(64,'0'));
    await f.upload(18,(1).toString(16).padStart(64,'0'));
    assert.equal(f.state.uploads,18,'the per-page asset cache must stay bounded');
  }
  console.log('Flow reference reuse: one upload for ten clips, exact per-attempt proofs, changed image/account/page/project isolation, manual reset, removed-asset recovery and bounded cache passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
