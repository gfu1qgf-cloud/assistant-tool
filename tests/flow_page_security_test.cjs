const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'..');
const source=fs.readFileSync(path.join(root,'browser_extensions/flow_bridge/background.js'),'utf8');
const start=source.indexOf('chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {');
const end=source.indexOf('// ===== 浏览器唯一 ID =====',start);
const prelude=source.slice(0,source.indexOf('// background.js -'));
let listener,reads=0,writes=0;
const storage={lzxConfig:{token:'SYNTHETIC-PAIR-TOKEN'},settings:{duration:8},presets:[]};
const chrome={runtime:{id:'synthetic-extension',onMessage:{addListener(fn){listener=fn;}}},
  storage:{local:{get(keys,reply){reads++;const names=typeof keys==='string'?[keys]:Array.isArray(keys)?keys:Object.keys(keys || storage);
    reply(Object.fromEntries(names.filter(k=>k in storage).map(k=>[k,storage[k]])));},set(data,reply){writes++;Object.assign(storage,data);reply();}}}};
const context={chrome,console:{log(){},warn(){},error(){}},setTimeout,clearTimeout,URL};
vm.runInNewContext(prelude+source.slice(start,end),context);
const sender={id:chrome.runtime.id,url:'https://labs.google/fx/tools/flow/project/synthetic',tab:{id:7,url:'https://labs.google/fx/tools/flow/project/synthetic'},frameId:0};
function invoke(message,from=sender){let answer;listener(message,from,value=>answer=value);return answer;}
for(const keys of ['lzxConfig',['lzxConfig'],null,undefined,{}, {lzxConfig:{}},['settings','lzxDownloads']]) {
  // Empty object requests no keys and is safe. No wildcard/all-keys retrieval.
  const before=reads,result=invoke({action:'getStorage',keys});
  if(keys && !Array.isArray(keys) && typeof keys==='object' && !Object.keys(keys).length)continue;
  assert(result.error);assert.equal(reads,before);assert(!JSON.stringify(result).includes('SYNTHETIC-PAIR-TOKEN'));
}
for(const data of [{lzxConfig:{token:'SYNTHETIC-OVERWRITE'}},{lzxBatches:{}},{settings:{duration:6},lzxTiming:{}},null,[]]) {
  const before=writes;assert(invoke({action:'setStorage',data}).error);assert.equal(writes,before);
}
assert.equal(storage.lzxConfig.token,'SYNTHETIC-PAIR-TOKEN');
assert.equal(invoke({action:'getStorage',keys:['settings','presets']}).settings.duration,8);
assert(invoke({action:'setStorage',data:{settings:{duration:6},tasks_synthetic:[],taskRecord_synthetic:{},isRunning_synthetic:false}}).success);
assert.equal(storage.settings.duration,6);
for(const from of [{...sender,frameId:1},{...sender,id:'foreign-extension'},{...sender,url:'https://evil.test/'},{...sender,url:'http://labs.google/'},{...sender,url:'https://docs.google.com/document/d/other'}]) {
  assert(invoke({action:'getStorage',keys:['settings']},from).error);
}
assert(invoke({action:'closeBrowser'}).error);
assert.equal(invoke({action:'nativeFileDrag',paths:['C:/private/image.png']}).dispatched,false);
// Exercise real desktop exact-path authorization, independent of UI mutex state.
const adapter=fs.readFileSync(path.join(root,'browser_extensions/flow_adapter/background.js'),'utf8');
const begin=adapter.indexOf('const nativeUploads = new Map();'),finish=adapter.indexOf('const downloadParkProgress',begin);
const native={authenticated:true,ownedTab:7,connectionEpoch:2,config:{project:sender.url},timingMs:(_key,fallback)=>fallback*1000,Date};
vm.createContext(native);vm.runInContext(adapter.slice(begin,finish)+'\nglobalThis.grants=nativeUploads;',native);
const proof={path:'c:/synthetic/image.png',project:sender.url,epoch:2,at:Date.now()};native.grants.set(7,proof);
const guard=native.__lzxNativeUploadGuard(7,['C:\\synthetic\\image.png']);guard.check();
assert.throws(()=>native.__lzxNativeUploadGuard(7,['C:/private/secret.png']),/未读取文件/);
assert.throws(()=>native.__lzxNativeUploadGuard(8,['C:/synthetic/image.png']),/未读取文件/);
native.authenticated=false;assert.throws(()=>guard.check(),/未读取文件/);native.authenticated=true;
native.connectionEpoch++;assert.throws(()=>guard.check(),/未读取文件/);native.connectionEpoch--;
native.config.project='https://labs.google/another';assert.throws(()=>guard.check(),/未读取文件/);native.config.project=sender.url;
proof.at=Date.now()-3600001;assert.throws(()=>guard.check(),/未读取文件/);proof.at=Date.now();
native.grants.set(7,{...proof});assert.throws(()=>guard.check(),/未读取文件/);native.grants.set(7,proof);
native.grants.delete(7);assert.throws(()=>guard.check(),/未读取文件/);
for(const message of [null,undefined,[],3,'getStorage'])assert(invoke(message).error);
console.log('Page security: no host-storage read/write, preserved UI state, exact sender, no page-wide close, exact live file grant and revocation passed.');
