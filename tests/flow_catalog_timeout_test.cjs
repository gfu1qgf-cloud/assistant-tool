const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const quick=p=>Promise.race([p,new Promise((_,reject)=>setTimeout(()=>reject(Error('catalog blocked')),100))]);
(async()=>{
  const sent=[];const c={browserOptions:{},commandsInFlight:0,closingBrowser:false,
    applyBrowserOptions:()=>new Promise(()=>{}),send:m=>sent.push(m),
    listProjectVideos:async()=>({videos:[{media_id:'ours'}]})};
  vm.createContext(c);vm.runInContext(source.slice(source.indexOf('async function receive('),source.indexOf('async function closeExhausted(')),c);
  await quick(c.receive({action:'list_project_videos',request_id:'readonly'}));
  assert.equal(sent[0].action,'list_project_videos_response');assert(sent[0].success);
  // Execute the real catalog reader with a page whose asynchronous timers and
  // thumbnail network never resolve. Loaded video metadata must still return.
  let fetches=0;const project='https://flow.google.com/project/ours';
  const card={tagName:'FLOW-VIDEO-TILE',getAttribute:k=>k==='data-tile-id'?'video-one':'',closest:()=>null,querySelector:()=>null};
  const video={tagName:'VIDEO',poster:'https://flow-content.google/image/poster',src:'https://flow-content.google/video/video-one',duration:10,
    closest:s=>s.includes('#flow-batch-overlay')?null:card,querySelector:()=>null,getAttribute:()=>null};
  const reader={config:{project},connectionEpoch:1,ownedTab:7,URL,setTimeout,clearTimeout,
    sameProjectTab:(tab,url)=>tab.url===url,
    chrome:{tabs:{query:async()=>[{id:7,url:project}],get:async()=>({id:7,url:project})},scripting:{async executeScript({func,args}){
      const page={URL,location:{href:project},AbortController,setTimeout(){return 1;},clearTimeout(){},
        fetch:()=>{fetches++;return new Promise(()=>{});},createImageBitmap:()=>new Promise(()=>{}),
        document:{querySelectorAll:()=>[video],createElement:()=>({getContext:()=>({}),toDataURL:()=>''})}};
      vm.createContext(page);page.args=args;return [{result:await vm.runInContext(`(${func.toString()})(...args)`,page)}];
    }}}};
  vm.createContext(reader);vm.runInContext(source.slice(source.indexOf('async function listProjectVideos('),source.indexOf('async function parkNativeDownloads(')),reader);
  const result=await quick(reader.listProjectVideos({params:{project}}));
  assert.equal(result.videos[0].media_id,'video-one');assert.equal(fetches,0);
  // The deadline includes Chrome's first tabs.query, not just executeScript.
  reader.chrome.tabs.query=()=>new Promise(()=>{});
  reader.setTimeout=(fn)=>setTimeout(fn,10);
  await assert.rejects(quick(reader.listProjectVideos({params:{project}})),/标签页查询|查询.*超时/);
  console.log('Catalog deadline: settings-sync bypass, metadata without async thumbnail, and bounded tabs.query passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
