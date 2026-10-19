const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/background.js'),'utf8');
const start=source.indexOf('const dragLanes = new Map();'),end=source.indexOf('// 2. 处理直接下载逻辑',start);
assert(start>=0&&end>start);
async function fixture(existing=false,foreign=false){
  const commands=[],detached=[];
  const chrome={debugger:{async attach(){if(existing)throw Error('Already attached');},
    async sendCommand(target,method,params){commands.push({method,params});if(foreign)throw Error('Debugger is not attached to this extension');},
    async detach(target){detached.push(target.tabId);}}};
  const context={chrome,console,setTimeout(fn){queueMicrotask(fn);},Map,Set};
  vm.createContext(context);vm.runInContext(source.slice(start,end)+'\nglobalThis.drag=dispatchNativeFileDrag;',context);
  let error;
  try{await context.drag(7,['C:/task/image.png'],100,100);}catch(e){error=e;}
  return {commands,detached,error};
}
(async()=>{
  const ordinary=await fixture();assert.deepEqual(ordinary.detached,[7]);
  assert.equal(ordinary.commands.at(-1).params.enabled,false,'temporary focus must be released');
  const stale=await fixture(true);assert.deepEqual(stale.detached,[7],'a proven own stale debugger must not stay attached after upload');
  const foreign=await fixture(true,true);assert(foreign.error);assert.equal(foreign.detached.length,0,'never detach another debugger');
  console.log('Flow upstream native upload: short debugger lifetime, stale owned session cleanup, foreign-session safety passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
