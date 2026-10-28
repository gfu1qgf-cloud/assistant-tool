// Actual background collectors in isolated page worlds; no real browser/network.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=process.env.LZX_VIDEO_CATALOG_ROOT || path.join(__dirname,'..');
const source=fs.readFileSync(path.join(root,'browser_extensions/flow_adapter/background.js'),'utf8');
const code=source.slice(source.indexOf('async function listProjectVideos('),source.indexOf('async function retryBoundDownload('));
const project='https://flow.google.com/project/ours';
const video=(media,options={})=>{
  const image=options.image;
  const card={tagName:image?'FLOW-IMAGE-TILE':'FLOW-VIDEO-TILE',getAttribute:key=>key==='data-tile-id'?(options.cardId || media):'',
    querySelector:selector=>selector.includes('img')?null:selector==='video'?node:null,closest:()=>null};
  const node={tagName:'VIDEO',src:options.src || `https://flow-content.google/video/${media}?Signature=test`,duration:10,
    closest:selector=>selector.includes('flow-image-tile')?(image?card:null):card,querySelector:()=>null,getAttribute:()=>''};
  return node;
};
function fixture(){
  const tabs=new Map([[7,{id:7,url:project}],[8,{id:8,url:project+'/edit/second'}],[9,{id:9,url:'https://flow.google.com/project/foreign'}]]);
  const pages=new Map([[7,[video('first'),video('first'),video('image',{image:true}),video('wrong',{cardId:'different'})]], [8,[video('second')]]]);
  const reads=[];let navigate=false;
  const sameProjectTab=(tab,url)=>{try{const a=new URL(tab.pendingUrl || tab.url),b=new URL(url);return a.origin===b.origin && a.pathname.split('/edit/')[0]===b.pathname.split('/edit/')[0];}catch{return false;}};
  const context={URL,setTimeout,clearTimeout,config:{project},connectionEpoch:1,ownedTab:7,
    sameProjectTab,chrome:{tabs:{async query(){return [...tabs.values()];},async get(id){return tabs.get(id);}},
      scripting:{async executeScript({target,func,args}){
        reads.push(target.tabId);const world={URL,setTimeout,clearTimeout,location:{href:tabs.get(target.tabId).url},
          document:{querySelectorAll(){return pages.get(target.tabId) || [];},createElement(){throw Error('no readable preview');}}};
        vm.createContext(world);world.args=args;const result=await vm.runInContext(`(${func.toString()})(...args)`,world);
        if(navigate)tabs.get(target.tabId).url=project+'/edit/changed';
        return [{result}];
      }}}};
  vm.createContext(context);vm.runInContext(code,context);
  return {context,pages,tabs,reads,run:()=>context.listProjectVideos({params:{project}}),navigate(){navigate=true;}};
}
(async()=>{
  const one=fixture(),result=await one.run();
  assert.deepEqual(Array.from(result.videos,v=>v.media_id),['first']);
  assert.deepEqual(one.reads,[7]);assert.equal(one.context.ownedTab,7);assert.equal(result.partial,true);
  one.pages.get(7).unshift(video('z-newest'));const refreshed=await one.run();
  assert.deepEqual(Array.from(refreshed.videos,v=>v.media_id),['z-newest','first'],'refresh preserves Flow order, not lexical IDs');
  const active=fixture();active.tabs.set(10,{id:10,url:project,active:true});active.pages.set(10,[video('z-new'),video('a-older')]);
  assert.deepEqual(Array.from((await active.run()).videos,v=>v.media_id),['z-new','a-older']);assert.deepEqual(active.reads,[10]);
  const detail=fixture();detail.tabs.delete(7);assert.deepEqual(Array.from((await detail.run()).videos,v=>v.media_id),['second']);
  const leaving=fixture();leaving.navigate();await assert.rejects(leaving.run(),/未能读取/);
  const none=fixture();none.tabs.clear();await assert.rejects(none.run(),/页面尚未打开/);
  const wrong=fixture();await assert.rejects(wrong.context.listProjectVideos({params:{project:'https://flow.google.com/project/foreign'}}),/不一致/);
  const empty=fixture();empty.pages.clear();assert.equal((await empty.run()).videos.length,0);
  // Reuse the actual original-project restore fixture with a user correction.
  const fixtureSource=fs.readFileSync(path.join(__dirname,'flow_video_page_target_test.cjs'),'utf8');
  const sandbox={require,console,__dirname,process,URL,Response,AbortController,setTimeout,clearTimeout};vm.createContext(sandbox);
  vm.runInContext(fixtureSource.slice(0,fixtureSource.indexOf('(async()=>'))+';globalThis.make=fixture;',sandbox);
  const chosen=sandbox.make({detail:false,nodes:false,network:true});
  const restore=chosen.message.params.restore;delete restore.video_page;
  chosen.context.browserId='browser';chosen.context.local=async()=>({});
  restore.bindings[0].source='manual_selection';restore.bindings[0].submissionId='manual_'+'f'.repeat(32);
  restore.manual_selection={schema:1,confirmed:true,attempt:chosen.message.params.batch_id,browser:'browser',
    media_id:restore.media_id,task_id:restore.task_id,submission_id:restore.bindings[0].submissionId,project};
  await chosen.run();assert.equal(chosen.native.length,1);assert.equal(chosen.native[0].identity.submissionId,restore.bindings[0].submissionId);
  assert.equal(chosen.context.ownedTab,7);
  const invalid=sandbox.make();invalid.context.browserId='browser';invalid.message.params.restore.manual_selection={...restore.manual_selection,confirmed:false};
  await assert.rejects(invalid.run(),/手动选择凭据/);assert.equal(invalid.native.length,0);
  const automatic=sandbox.make();automatic.context.local=async()=>({});await assert.rejects(automatic.run(),/没有原提交批次/);
  console.log('Video catalog: current DOM refresh, video-only IDs, duplicate filtering, exact project/tab epoch, explicit manual restore without generation receipts, no tab activation/owner change passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
