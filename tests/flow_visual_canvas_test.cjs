// Optional real raster integration: actual PNG decode, crop, cover and comparison.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let canvas;
try{canvas=require('@napi-rs/canvas');}catch{console.log('SKIP visual raster fixture: @napi-rs/canvas is not installed');process.exit(0);}
const {createCanvas,loadImage}=canvas;
const source=createCanvas(128,64),ctx=source.getContext('2d');
let seed=123;
for(let y=0;y<64;y+=4)for(let x=0;x<128;x+=4){seed=(Math.imul(seed,1664525)+1013904223)>>>0;
  ctx.fillStyle=`rgb(${20+seed%210},${20+(seed>>>8)%210},${20+(seed>>>16)%210})`;ctx.fillRect(x,y,4,4);}
const ref={thumbnail:source.toDataURL('image/png')};
const screen=createCanvas(800,600),view=screen.getContext('2d');view.fillStyle='gray';view.fillRect(0,0,800,600);
view.drawImage(source,40,30,256,128);
view.drawImage(source,32,0,64,64,400,30,128,128);
const context={fetch,OffscreenCanvas:class{constructor(w,h){return createCanvas(w,h);}},
  createImageBitmap:async blob=>{const image=await loadImage(Buffer.from(await blob.arrayBuffer()));image.close=()=>{};return image;}};
vm.runInNewContext(fs.readFileSync(path.join(process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname,'..'),
  'browser_extensions/flow_adapter/visual.js'),'utf8'),context);
(async()=>{
  const capture={data:screen.toBuffer('image/png').toString('base64'),width:400,height:300};
  const match=await context.__lzxVisualUpload.match(ref,[{media_id:'new-wide',x:20,y:15,width:128,height:64,fit:'fill'}],[],capture);
  assert.equal(match?.media_id,'new-wide','DPI-scaled screenshot crop should identify the exact reference');
  const cover=await context.__lzxVisualUpload.match(ref,[{media_id:'new-cover',x:200,y:15,width:64,height:64,fit:'cover'}],[],capture);
  assert.equal(cover?.media_id,'new-cover','object-fit cover uses the reference center crop');
  const historical=await context.__lzxVisualUpload.match(ref,[{media_id:'old',x:20,y:15,width:128,height:64,fit:'fill'}],['old'],capture);
  assert.equal(historical,null);
  console.log('Visual raster pipeline: real PNG decoding, screenshot DPI, image crop, object-fit cover, and historical exclusion passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
