const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname,'..'),
  'browser_extensions/flow_adapter/reference.js'),'utf8');
const filename='LZXREF'+'a'.repeat(32)+'.png',other='LZXREF'+'b'.repeat(32)+'.png';
const image=id=>({src:'/image/'+id,getAttribute:()=>null,querySelectorAll:()=>[]});
function tile({names=[filename],ids=['current-media'],video=false}={}) {
  const attrs=names.map(value=>({getAttribute:key=>key==='title'?value:null}));
  const imgs=ids.map(image);
  return {textContent:'Image asset',getAttribute:key=>key==='aria-label'?'Image asset':null,
    closest:()=>video?{}:null,querySelector:selector=>selector==='flow-video-tile'?null:imgs[0],
    querySelectorAll:selector=>selector.includes('[title]')?attrs:imgs};
}
let tiles=[];
const window={};window.top=window;
vm.runInNewContext(source,{window,document:{querySelectorAll:()=>tiles},location:{href:'https://flow.google.com/project/identity'},URL});
const api=window.__lzxReferenceIdentity;
tiles=[tile()];let found=api.images([{displayName:'Image asset',primaryMediaKey:'current-media'}]);
assert(found.some(item=>item.displayName===filename.toLowerCase()&&item.primaryMediaKey==='current-media'));
assert.equal(api.images(found).length,found.length,'the identity scan is idempotent');
tiles=[tile({names:[filename,filename.slice(0,-4)]})];assert.equal(api.images([]).length,2);
const stable='IMG_'+'c'.repeat(64)+'_'+'a'.repeat(32)+'.png';
tiles=[tile({names:[stable]})];assert.equal(api.images([])[0].displayName,stable.toLowerCase());
tiles=[tile({names:[stable,'IMG_'+'d'.repeat(64)+'_'+'a'.repeat(32)+'.png']})];
assert.equal(api.images([]).length,0,'one container cannot prove two permanent image IDs');
for(const options of [{names:[filename,other]},{ids:['one','two']},{ids:['img_placeholder']},{video:true},{names:['renamed original.png']}]) {
  tiles=[tile(options)];assert.equal(api.images([]).length,0,'ambiguous, unrelated, temporary and video cards cannot prove an upload');
}
console.log('Upload identity: exact unique filename recovered behind generic labels, one media ID, ambiguity rejection and no thumbnail/count guessing passed.');
