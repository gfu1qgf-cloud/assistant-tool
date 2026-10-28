// Exact material row selection with preview-only child buttons and DOM replacement.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(process.env.LZX_REFERENCE_SOURCE || path.join(__dirname,'../browser_extensions/flow_adapter/reference.js'),'utf8');
function fixture({delayed=0,rerender=false,aria=false,previewFirst=false,wrong=false,expire=false}={}) {
  let clock=1000;const wanted='exact-image';
  const state={open:false,mounted:'',selected:false,confirmed:false,uploads:0,rowClicks:0,badClicks:0,confirmedAt:0};
  const id='a'.repeat(32),reference={upload_id:id,sha256:'b'.repeat(64),filename:'LZXREF'+id+'.png'};reference.path='C:/refs/'+reference.filename;
  const img={tagName:'IMG',src:'/image/'+wanted,getAttribute:()=>null,querySelectorAll:()=>[]};
  const child={previewOnly:true};
  const row={tagName:'DIV',textContent:'IMG_abcdef...',isConnected:true,row:true,
    closest:()=>null,getAttribute:k=>k==='data-media-id'?wanted:null,querySelectorAll:()=>[img],querySelector:s=>s==='img'?img:s.includes('button')?child:null};
  const preview={tagName:'IMG',src:'/image/'+wanted,previewOnly:true,getAttribute:()=>null,querySelectorAll:()=>[],closest:()=>null};
  const viewport={clientHeight:100,scrollHeight:100,scrollTop:0,isConnected:true,contains:node=>node===row,
    querySelectorAll:()=>[row],getBoundingClientRect:()=>({top:0,bottom:100})};
  const button={confirm:true,isConnected:true,textContent:aria?'add':'插入',
    get disabled(){return rerender && clock<1070;},getAttribute:k=>k==='aria-label'&&aria?'Use as start frame':null};
  const panel=()=>({offsetWidth:400,offsetHeight:200,isConnected:true,textContent:'选择画面图像',
    closest:()=>null,getAttribute:()=>null,contains:node=>node===viewport||node===row||node===preview,
    querySelectorAll:s=>s==='button'?(state.selected&&(!rerender || clock>=1050)?[button]:[]):
      s.includes('viewport')?[viewport]:previewFirst?[preview,row]:[row]});
  const trigger={open:true},start={textContent:'开始',getAttribute:()=>null,
    querySelector:s=>s.includes('button')?trigger:state.mounted?{src:'/image/'+state.mounted,querySelectorAll:()=>[],getAttribute:()=>null}:null,
    querySelectorAll:s=>s==='button'?[trigger]:state.mounted?[{src:'/image/'+state.mounted,getAttribute:()=>null,querySelectorAll:()=>[]}]:[]};
  const bar={querySelectorAll:()=>[start]};
  const document={querySelector:()=>bar,querySelectorAll:s=>s.startsWith('flow-')?[]:state.open?[panel()]:[]};
  const window={__lzxFlowTransport:{assertUi(){if(expire&&state.selected)throw Error('lease expired');},async closeAccountPanel(){}}};window.top=window;
  const tick=()=>{clock+=10;if(state.confirmed&&clock>=state.confirmedAt+delayed){state.mounted=wrong?'another-image':wanted;state.open=false;}};
  vm.runInNewContext(source,{window,document,URL,Event:class{},location:{href:'https://flow.google.com/project/selection'},
    Date:class extends Date{static now(){return clock;}},setTimeout:fn=>queueMicrotask(()=>{tick();fn();})});
  const api=window.__lzxReferenceIdentity;api.init({images:()=>state.uploads?[{id:wanted,primaryMediaKey:wanted,displayName:reference.filename}]:[],
    cleanup:async()=>{state.open=false;},upload:async()=>{state.uploads++;return{uploaded:[reference.path],failed:[]};},
    click(node){if(node.open)state.open=true;else if(node.row){state.rowClicks++;state.selected=true;}
      else if(node.confirm){assert(!node.disabled);state.confirmed=true;state.confirmedAt=clock;}
      else if(node.previewOnly)state.badClicks++;}},(k,f)=>k==='page_controls_wait_seconds'?1000:k==='page_control_poll_seconds'?10:f*1000);
  return{state,upload:()=>api.upload({upload_id:id,reference,paths:[reference.path]},{account:'fixture@example.test',page_instance:'one'})};
}
(async()=>{
  for(const options of [{},{delayed:300},{rerender:true},{aria:true},{previewFirst:true}]) {
    const f=fixture(options);await f.upload();assert.equal(f.state.mounted,'exact-image');assert.equal(f.state.rowClicks,1);
    assert.equal(f.state.badClicks,0);assert.equal(f.state.uploads,1);
  }
  const wrong=fixture({wrong:true});await assert.rejects(wrong.upload(),/挂载未确认/);assert.equal(wrong.state.uploads,1);
  await assert.rejects(wrong.upload());assert.equal(wrong.state.uploads,1,'keep confirmed upload for mounting retry');
  const expired=fixture({expire:true});await assert.rejects(expired.upload(),/lease expired/);assert(!expired.state.confirmed);
  console.log('PASS exact row, delayed mount, re-rendered/disabled footer, icon aria, preview exclusion, wrong-ID and expired-lock guards');
})().catch(e=>{console.error(e);process.exitCode=1;});
