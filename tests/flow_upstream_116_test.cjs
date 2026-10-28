// Upstream cleanup must not erase the exact reference retained by a managed pack.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'..');
const ui=fs.readFileSync(path.join(root,'browser_extensions/flow_bridge/injects/ui.js'),'utf8');
const begin=ui.indexOf('  async function clearFlowPromptBox('),end=ui.indexOf('\n  let isStopRequested',begin);
assert(begin>0&&end>begin);
async function check(managed){
  let cleanup=0,dom=0;
  const context={window:managed?{__lzxFlowTransport:{}}:{},console:{warn(){},log(){}},
    document:{querySelector(){dom++;throw Error('fixture stops standalone cleanup');}},
    cleanupPageOverlays:async()=>{cleanup++;throw Error('fixture stops standalone cleanup');}};
  vm.createContext(context);vm.runInContext(ui.slice(begin,end),context);
  await context.clearFlowPromptBox('fixture');
  assert.equal(cleanup,managed?0:1);assert.equal(dom,0);
}
(async()=>{await check(true);await check(false);
  assert(ui.includes('window.__lzxReferenceIdentity.attach(image, footageType)'));
  assert(ui.includes('window.__lzxReferenceIdentity.verify(image, footageType)'));
  console.log('Upstream 1.1.6 cleanup isolation and exact-frame verification passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
