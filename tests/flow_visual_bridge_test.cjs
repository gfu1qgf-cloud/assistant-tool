const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname,'..');
const read=file=>fs.readFileSync(path.join(root,file),'utf8');
const background=read('browser_extensions/flow_adapter/background.js');
const start=background.indexOf("  if (message.action === 'lzxVisualUploadMatch')"),end=background.indexOf('  const ownPage =',start);
assert(start>0&&end>start);
function fixture({enabled=true,authorized=true}={}) {
  const project='https://flow.google.com/project/exact',tab={id:7,url:project},upload_id='a'.repeat(32);
  const entry={reference:{upload_id},project,epoch:1,at:Date.now(),count:0};let captures=0;
  const context={authenticated:authorized,browserOptions:{visual_upload_fallback:enabled},ownedTab:7,connectionEpoch:1,
    visualUploads:new Map([[7,entry]]),timingMs:()=>180000,
    chrome:{runtime:{id:'test-extension'},tabs:{get:async()=>tab}},sameProjectTab:(t,p)=>t.url===p,
    __lzxCaptureUpload:async id=>{assert.equal(id,7);captures++;return {width:800,height:600,data:'private-no-output'};},
    __lzxVisualUpload:{match:async()=>({media_id:'current-image',source:'local_screenshot'})}};
  vm.createContext(context);vm.runInContext('function handle(message,sender,reply){'+background.slice(start,end)+'}',context);
  const message={action:'lzxVisualUploadMatch',upload_id,before_ids:['historical-image'],viewport:{width:800,height:600},
    candidates:[{kind:'image',media_id:'current-image',x:50,y:50,width:100,height:100,fit:'cover'}]};
  const sender={id:'test-extension',frameId:0,tab:{id:7}};
  return {context,entry,tab,message,sender,captures:()=>captures,
    call:(m=message,s=sender)=>new Promise(resolve=>context.handle(m,s,resolve))};
}
async function capture({foreign=false,fail=false}={}) {
  const built=read('browser_extensions/flow_bridge/background.js');
  const laneStart=built.indexOf('const dragLanes = new Map();'),laneEnd=built.indexOf('async function dispatchNativeFileDrag(',laneStart);
  const fnStart=built.indexOf('globalThis.__lzxCaptureUpload ='),fnEnd=built.indexOf('// Host-authenticated recovery',fnStart);
  const commands=[],detached=[];
  const ctx={console,setTimeout,clearTimeout,chrome:{debugger:{
    attach:async()=>{if(foreign)throw Error('Already attached');},detach:async target=>detached.push(target.tabId),
    sendCommand:async(target,method)=>{assert.equal(target.tabId,7);commands.push(method);
      if(foreign)throw Error('not owned');if(fail)throw Error('page changed');
      return method==='Page.getLayoutMetrics'?{cssVisualViewport:{pageX:0,pageY:0,clientWidth:800,clientHeight:600}}:{data:'mock-png'};}}}};
  vm.createContext(ctx);vm.runInContext(built.slice(laneStart,laneEnd)+built.slice(fnStart,fnEnd),ctx);
  try {await ctx.__lzxCaptureUpload(7);}catch{}
  return {commands,detached};
}
(async()=>{
  const f=fixture();const reply=await f.call();assert.equal(reply.match.media_id,'current-image');assert.equal(f.captures(),1);
  assert.equal(JSON.stringify(reply).includes('private-no-output'),false,'screenshots never cross back into the web page');
  for(const alter of [g=>g.message.candidates[0].kind='video',g=>g.message.upload_id='b'.repeat(32),
    g=>delete g.message.viewport,g=>g.sender.frameId=1,g=>g.sender.tab.id=8,g=>g.tab.url='https://flow.google.com/project/other',
    g=>g.entry.epoch=0,g=>g.entry.count=3]) {
    const g=fixture();alter(g);assert.equal((await g.call()).ok,false);assert.equal(g.captures(),0);
  }
  const disabled=fixture({enabled:false});assert.equal((await disabled.call()).ok,false);assert.equal(disabled.captures(),0);
  const unauthorized=fixture({authorized:false});assert.equal((await unauthorized.call()).ok,false);
  const ordinary=await capture();assert.deepEqual(ordinary.commands,['Page.getLayoutMetrics','Page.captureScreenshot']);assert.deepEqual(ordinary.detached,[7]);
  assert.deepEqual((await capture({fail:true})).detached,[7]);assert.deepEqual((await capture({foreign:true})).detached,[]);
  console.log('Visual bridge: authenticated active upload and exact tab, disabled/video/stale rejection, screenshot privacy, bounded capture and owned-debugger cleanup passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
