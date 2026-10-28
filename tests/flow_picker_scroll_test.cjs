// Real reference controller, with a delayed virtual list rather than static cards.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(process.env.LZX_REFERENCE_SOURCE || path.join(__dirname,'../browser_extensions/flow_adapter/reference.js'),'utf8');
function fixture({lag=0,targetPage=2,missing=false,nested=false,aliases=false,video=false,grow=false,stuck=false,ambiguous=false,
  revealDelay=0,wrongViewport=false,horizontal=false,temporaryMissing=false,waitMs=2000,unknownCss=false,detached=false}={}) {
  let clock=1000,position=0,rendered=0,due=0,pending=0,expanded=false,expandAt=0,revealAt=0,thumbnailTop=0;
  const state={picker:false,mounted:'',uploads:0,images:[],moves:[],parentMoves:[],clicks:0,reveals:0,thumbnailMoves:0};
  const wanted='exact-reference',upload_id='a'.repeat(32),filename='LZXREF'+upload_id+'.png';
  const reference={upload_id,filename,path:'C:/refs/'+filename,sha256:'b'.repeat(64)};
  const rect={left:0,top:0,right:100,bottom:100,width:100,height:100};
  const image=id=>({tagName:'IMG',src:'/image/'+id,isConnected:true,getAttribute:()=>null,querySelectorAll:()=>[]});
  const refresh=()=>{if(due && clock>=due){rendered=pending;due=0;}if(grow && expandAt && clock>=expandAt){expanded=true;expandAt=0;}};
  const tiles=()=>{
    refresh();const temporarilyGone=temporaryMissing&&revealAt&&clock<revealAt;
    const id=!missing&&!temporarilyGone&&rendered===targetPage?wanted:'other-'+rendered;
    const img=image(ambiguous?'different-reference':id);return [{choose:id,page:rendered,isPickerRow:true,
      get parentElement(){return wrongViewport?thumbnail:viewport;},
      getBoundingClientRect:()=>revealDelay&&(!revealAt||clock<revealAt)?
        horizontal?{...rect,left:200,right:300}:{...rect,top:200,bottom:300}:rect,
      scrollIntoView(){state.reveals++;if(!revealAt)revealAt=clock+revealDelay;},
      tagName:video?'FLOW-VIDEO-TILE':'FLOW-IMAGE-TILE',textContent:'image',isConnected:true,
      matches:s=>s==='flow-image-tile'&&!video,
      closest:s=>video && s.includes('flow-video-tile')?{}:null,
      getAttribute:key=>key==='data-media-id'?(aliases?'projects/p/media/'+id:id):key==='data-media-type'?(video?'video':'image'):null,
      querySelector:s=>s==='img'?img:s.includes('video')&&video?{}:s.includes('button')?{choose:id,page:rendered}:null,
      querySelectorAll:s=>s==='mat-icon'?[]:[img]}];
  };
  const thumbnail={isConnected:true,clientHeight:80,scrollHeight:160,
    get parentElement(){return viewport;},
    get scrollTop(){return thumbnailTop;},set scrollTop(value){thumbnailTop=value;state.thumbnailMoves++;},
    contains:()=>false,dispatchEvent(){},querySelectorAll:()=>[image('other-0')]};
  const viewport={get isConnected(){return !(detached&&state.reveals);},clientHeight:100,get scrollHeight(){refresh();return expanded?500:300;},
    get scrollTop(){return position;},set scrollTop(value){state.moves.push(value);if(stuck)return;position=value;pending=Math.round(value/80);due=clock+lag;if(!lag){rendered=pending;due=0;}if(grow && value>=200&&!expanded&&!expandAt)expandAt=clock+60;},
    dispatchEvent(){},querySelectorAll:()=>tiles(),getBoundingClientRect:()=>rect,contains:node=>node===thumbnail||node?.isPickerRow===true};
  const parent={isConnected:true,clientHeight:200,scrollHeight:800,scrollTop:0,contains:node=>node===viewport,
    querySelectorAll:()=>tiles().concat(tiles()),dispatchEvent(){state.parentMoves.push(this.scrollTop);}};
  const panel={offsetWidth:300,offsetHeight:200,isConnected:true,textContent:'选择画面图像 图片 上传的内容',
    getAttribute:key=>key==='aria-label'?'选择画面图像':null,closest:()=>null,contains:node=>node===viewport||node===parent||node===thumbnail,
    querySelectorAll:s=>s==='button'?[]:s.includes('viewport')||s.includes('listbox')?(nested?[parent,viewport]:[viewport]):tiles()};
  const trigger={open:true};const start={textContent:'Start',getAttribute:()=>'',
    querySelector:s=>s.includes('button')?trigger:state.mounted?image(state.mounted):null,
    querySelectorAll:s=>s==='button'?[trigger]:state.mounted?[image(state.mounted)]:[]};
  const bar={querySelectorAll:()=>[start]};
  const document={querySelector:()=>bar,querySelectorAll:s=>s.startsWith('flow-')?[]:state.picker?[panel]:[]};
  const location={href:'https://flow.google.com/project/picker-test'};
  const window={__lzxFlowTransport:{assertUi(){},async closeAccountPanel(){}}};window.top=window;
  class Event {constructor(type){this.type=type;}}
  const context={window,document,location,URL,Event,Date:class extends Date {static now(){return clock;}},
    getComputedStyle:unknownCss?undefined:node=>({overflowY:node===thumbnail||node===panel?'hidden':node===viewport||node===parent?'auto':'visible'}),
    setTimeout:fn=>queueMicrotask(()=>{clock+=10;refresh();fn();})};
  vm.runInNewContext(source,context);const api=window.__lzxReferenceIdentity;
  api.init({images:()=>state.images,cleanup:async()=>{state.picker=false;},
    upload:async()=>{state.uploads++;state.images=[{id:wanted,primaryMediaKey:wanted,displayName:filename}];return {uploaded:[reference.path],failed:[]};},
    click(node){if(node.open)state.picker=true;else if(node.choose){assert.equal(node.page,rendered,'never click a stale virtual card');state.clicks++;state.mounted=node.choose;state.picker=false;}}},
    (key,fallback)=>key==='page_controls_wait_seconds'?waitMs:key==='page_control_poll_seconds'?10:fallback*1000);
  const upload=()=>api.upload({upload_id,reference,paths:[reference.path]},{account:'fixture@example.test',page_instance:'one'});
  return {api,state,upload,elapsed:()=>clock-1000,viewport};
}
const cases={
  async delayed_virtual_render(){const f=fixture({lag:60});await f.upload();assert.equal(f.state.mounted,'exact-reference');assert.equal(f.state.uploads,1);},
  async nested_scroll_containers(){const f=fixture({nested:true});await f.upload();assert.equal(f.state.parentMoves.length,0,'scroll only the actual inner material viewport');assert.equal(f.state.mounted,'exact-reference');},
  async bottom_terminates_early(){const f=fixture({missing:true});await assert.rejects(f.upload(),/底部|扫描完成/);assert(f.elapsed()<1200,'do not consume the full picker deadline at an unchanged bottom');assert.equal(f.state.clicks,0);},
  async canonical_id_aliases(){const f=fixture({aliases:true,targetPage:0});await f.upload();assert.equal(f.state.mounted,'exact-reference');},
  async loaded_bottom_can_grow(){const f=fixture({grow:true,targetPage:4});await f.upload();assert.equal(f.state.mounted,'exact-reference');},
  async video_poster_never_selected(){const f=fixture({video:true,targetPage:0});await assert.rejects(f.upload());assert.equal(f.state.clicks,0);},
  async failure_retry_never_reuploads(){const f=fixture({missing:true});await assert.rejects(f.upload());await assert.rejects(f.upload());assert.equal(f.state.uploads,1);}
  ,async nonresponsive_scroll_stops(){const f=fixture({stuck:true});await assert.rejects(f.upload(),/未响应滚动/);assert(f.elapsed()<1000);assert.equal(f.state.clicks,0);}
  ,async genuinely_different_ids_stay_ambiguous(){const f=fixture({ambiguous:true,targetPage:0});await assert.rejects(f.upload());assert.equal(f.state.clicks,0);}
  ,async delayed_offscreen_reveal_waits_for_render(){const f=fixture({targetPage:0,revealDelay:600});await f.upload();assert.equal(f.state.mounted,'exact-reference');assert(f.elapsed()>=600);assert.equal(f.state.reveals,1,'do not restart virtual rendering every poll');assert.equal(f.state.uploads,1);}
  ,async cropped_thumbnail_is_not_the_material_viewport(){const f=fixture({wrongViewport:true});await f.upload();assert.equal(f.state.mounted,'exact-reference');assert(f.state.moves.length>0);assert.equal(f.state.thumbnailMoves,0);assert.equal(f.state.uploads,1);}
  ,async horizontal_offscreen_reveal_waits_for_render(){const f=fixture({targetPage:0,revealDelay:600,horizontal:true});await f.upload();assert.equal(f.state.mounted,'exact-reference');assert(f.elapsed()>=600);assert.equal(f.state.reveals,1);}
  ,async virtual_target_may_temporarily_leave_dom(){const f=fixture({targetPage:0,revealDelay:600,temporaryMissing:true});await f.upload();assert.equal(f.state.mounted,'exact-reference');assert.equal(f.state.moves.length,0,'keep waiting for the reveal instead of scanning past it');assert.equal(f.state.reveals,1);}
  ,async reveal_timeout_is_bounded_and_does_not_reupload(){const f=fixture({targetPage:0,revealDelay:10000});await assert.rejects(f.upload(),/素材卡片未能显示/);assert(f.elapsed()<=2100);assert.equal(f.state.reveals,1);assert.equal(f.state.clicks,0);await assert.rejects(f.upload());assert.equal(f.state.uploads,1);}
  ,async reveal_limit_does_not_consume_the_whole_45_second_phase(){const f=fixture({targetPage:0,revealDelay:10000,waitMs:45000});await assert.rejects(f.upload(),/素材卡片未能显示/);assert(f.elapsed()>=5000&&f.elapsed()<5100);assert.equal(f.state.reveals,1);assert.equal(f.state.clicks,0);}
  ,async semantic_viewport_wins_when_css_is_not_available(){const f=fixture({wrongViewport:true,unknownCss:true});await f.upload();assert.equal(f.state.mounted,'exact-reference');assert.equal(f.state.thumbnailMoves,0);}
  ,async removed_viewport_does_not_restart_the_reveal_deadline(){const f=fixture({targetPage:0,revealDelay:10000,waitMs:45000,temporaryMissing:true,detached:true});await assert.rejects(f.upload(),/素材卡片未能显示/);assert(f.elapsed()>=5000&&f.elapsed()<5100);assert.equal(f.state.clicks,0);assert.equal(f.state.uploads,1);}
};
(async()=>{const failed=[];for(const [name,run] of Object.entries(cases)){try{await run();console.log('PASS '+name);}catch(error){failed.push(name);console.error('FAIL '+name+': '+error.message);}}if(failed.length)process.exitCode=1;})().catch(error=>{console.error(error);process.exitCode=1;});
