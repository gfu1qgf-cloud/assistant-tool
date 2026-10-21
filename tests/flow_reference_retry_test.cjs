const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/reference.js'),'utf8');
function fixture({picker=true,kind='flow-grid-tile-container',virtual=false,wrongTile=false}={}){
  const state={picker,uploads:0,drags:0,cleanups:0,mounted:'',images:[],missing:false};
  const location={href:'https://flow.google.com/project/reference-retry'};
  const image=id=>({src:'/image/'+id,getAttribute:()=>null,querySelectorAll:()=>[]});
  const trigger={open:true};const start={textContent:'Start',getAttribute:()=>'',
    querySelector:s=>s.includes('button')?trigger:state.mounted?image(state.mounted):null,
    querySelectorAll:s=>s==='button'?[trigger]:state.mounted?[image(state.mounted)]:[]};
  const bar={querySelectorAll:()=>[start]};
  const scroll={scrollTop:0,clientHeight:100,scrollHeight:300,dispatchEvent(){}};
  const panel={offsetWidth:300,offsetHeight:200,closest:()=>null,
    querySelector:s=>s.includes('viewport')?scroll:null,
    querySelectorAll:s=>s==='button'?[]:s.includes('viewport')?[scroll]:s.includes(kind)&&!state.missing&&(!virtual||scroll.scrollTop>=100)?
      state.images.map(img=>({getAttribute:()=>null,querySelectorAll:()=>[image(wrongTile?'unrelated-old-media':img.primaryMediaKey)],
        querySelector:()=>({select:img.primaryMediaKey})})):[]};
  const document={querySelector:()=>bar,querySelectorAll:()=>state.picker?[panel]:[]};
  const window={};window.top=window;
  vm.runInNewContext(source,{window,document,location,setTimeout,URL,Event});
  const api=window.__lzxReferenceIdentity;
  api.init({images:()=>state.images,async cleanup(){state.cleanups++;state.picker=false;scroll.scrollTop=0;},
    async upload(paths){state.uploads++;if(state.picker)return {failed:paths,errors:{file:'stale picker intercepts upload'}};
      if(!state.images.length){state.drags++;state.images=[{primaryMediaKey:'exact-new-media',displayName:paths[0].split('/').at(-1)}];}
      return {uploaded:paths,failed:[]};},
    click(node){if(node.open)state.picker=!state.picker;else if(node.select){state.mounted=node.select;state.picker=false;}}
  },key=>key==='page_controls_wait_seconds'?150:1);
  const upload_id='b'.repeat(32),filename='LZXREF'+upload_id+'.png';
  const reference={upload_id,filename,path:'C:/reference/'+filename,sha256:'c'.repeat(64)};
  const params={upload_id,reference,paths:[reference.path]};
  const upload=()=>api.upload(params,{account:'test@example.test',page_instance:'one'});
  return {state,api,upload,params};
}
(async()=>{
  const late={};late.top=late;late.__lzxFlowTransport={initReference(){late.__lzxReferenceIdentity.init({},()=>1);}};
  vm.runInNewContext(source,{window:late,setTimeout,URL});
  assert.equal(late.__lzxReferenceIdentity.ready(),true,'late script repair must reconnect to the existing page transport');
  const stale=fixture();await stale.upload();assert.equal(stale.state.mounted,'exact-new-media');
  const retry=fixture({picker:false});retry.state.missing=true;
  await assert.rejects(retry.upload(),/素材编号/);retry.state.missing=false;
  await retry.upload();assert.equal(retry.state.mounted,'exact-new-media');assert.equal(retry.state.drags,1);
  const alternate=fixture({picker:false,kind:'flow-media-tile'});await alternate.upload();assert.equal(alternate.state.mounted,'exact-new-media');
  const virtual=fixture({picker:false,virtual:true});await virtual.upload();assert.equal(virtual.state.mounted,'exact-new-media');
  const wrong=fixture({picker:false,wrongTile:true});
  await assert.rejects(wrong.upload());assert.equal(wrong.state.mounted,'');
  assert.equal(wrong.state.picker,false,'a failed attach must not leave a picker intercepting the next retry');
  console.log('Reference retry: stale picker cleanup, same-upload recovery, supported exact-ID tiles and virtualized picker search passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
