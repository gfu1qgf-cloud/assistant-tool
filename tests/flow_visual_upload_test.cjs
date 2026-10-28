const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname,'..');
const read=file=>fs.readFileSync(path.join(root,'browser_extensions/flow_adapter',file),'utf8');
const context={};vm.runInNewContext(read('visual.js'),context);
const {compare,choose}=context.__lzxVisualUpload;
function pixels(seed,noise=0) {
  const data=new Uint8ClampedArray(4096);let v=seed;
  for(let i=0;i<1024;i++){v=(Math.imul(v,1664525)+1013904223)>>>0;
    for(let c=0;c<3;c++)data[i*4+c]=20+(v>>>(8*c))%210+(i%3-1)*noise;data[i*4+3]=255;}
  return data;
}
const original=pixels(123),exact=compare(original,original),near=compare(original,pixels(123,2)),different=compare(original,pixels(456));
assert.equal(exact.error,0);assert(exact.correlation>.999);assert(exact.edges>.99);
assert(choose([{media_id:'new',score:near},{media_id:'old',score:different}],['old']));
assert.equal(choose([{media_id:'a',score:exact},{media_id:'b',score:exact}],[]),null,'duplicate-looking images remain ambiguous');
assert.equal(choose([{media_id:'old',score:exact}],['old']),null,'historical images cannot win');
assert.equal(choose([{media_id:'different',score:different}],[]),null);
assert.equal(compare(new Uint8Array(4096),new Uint8Array(4096)),null,'blank placeholders cannot identify media');

const window={};window.top=window;let tiles=[];
const document={querySelectorAll:()=>tiles,elementFromPoint:()=>null};
vm.runInNewContext(read('reference.js'),{window,document,location:{href:'https://flow.google.com/project/visual'},URL,innerWidth:1200,innerHeight:800});
function tile({id='new-image',video=false,unknown=false,poster=false,play=false,old=false}={}) {
  const img={naturalWidth:200,naturalHeight:200,isConnected:true,src:'/image/'+id,
    getAttribute:()=>null,classList:{contains:()=>poster},getBoundingClientRect:()=>({left:100,top:100,right:300,bottom:300,width:200,height:200})};
  return {matches:()=>!unknown,closest:()=>video?{}:null,getAttribute:key=>key==='data-media-type'&&video?'video':null,
    querySelector:selector=>selector==='img'?img:selector.includes('video,')?play?{}:null:null,
    querySelectorAll:selector=>selector==='mat-icon'?[]:[img]};
}
tiles=[tile()];assert.equal(window.__lzxReferenceIdentity.visualCandidates().length,1);
for(const options of [{video:true},{unknown:true},{poster:true},{play:true}]) {
  tiles=[tile(options)];assert.equal(window.__lzxReferenceIdentity.visualCandidates().length,0,'video and untyped poster cards must never enter screenshot matching');
}
console.log('Visual fallback: conservative pixel/color/edge match, duplicate and historical rejection, flat placeholders, explicit image-only candidates and video exclusions passed.');
