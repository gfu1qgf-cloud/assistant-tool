// Run the real additive upstream seam: several independent submissions before
// any generation/download completion, with late receipt and failure isolation.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/injects/ui.js'),'utf8');
const {installDownloadHelpers}=require('./flow_test_support.cjs');
function fixture() {
  let now=0, submits=0;
  const state={tasks:[],isRunning:false};
  const window={__lzxFlowTransport:{sitePaused:()=>false}};
  const context={state,window,batchPromise:null,Date:{now:()=>now},
    async saveState(){},updateLineStatus(row,line,status){state.tasks[row].status=[status];},
    importTaskList(items){for(const item of items)state.tasks.push({...item,taskId:'task-'+state.tasks.length,status:[],downloaded:[],downloadIds:[],mediaBindings:[]});return items.length;},
    async submitTaskToPage(p){
      submits++;const task=state.tasks[p.rowIndex];
      assert.equal(task.download_path,p.download_path);
      task.mediaBindings=[{schema:2,source:'generation_response',taskId:task.taskId,submissionId:'s'+p.rowIndex,mediaIds:['m'+p.rowIndex],readyIds:[]}];
    },async delay(ms){now+=ms;}};
  vm.createContext(context);
  const begin=source.indexOf('  async function submitPackedTask('),end=source.indexOf('  async function runBatch(',begin);
  vm.runInContext(source.slice(begin,end),context);
  const message=char=>({params:{packed:true,clear_existing:false,tasks:[{download_path:`FlowBridge/${char.repeat(32)}/result.mp4`,text:'prompt',image:{primaryMediaKey:'same-image'}}]}});
  return {context,state,message,submits:()=>submits};
}
(async()=>{
  const f=fixture();
  for(const char of ['a','b','c','d']) {
    const result=await f.context.submitPackedTask(f.message(char));
    assert.equal(result.packed,true);assert.equal(f.state.isRunning,false);
  }
  assert.equal(f.submits(),4);assert.equal(f.state.tasks.length,4);
  assert(f.state.tasks.every(t=>t.status[0]==='generating'&&!t.downloaded[0]));
  assert.equal(new Set(f.state.tasks.flatMap(t=>t.mediaBindings[0].mediaIds)).size,4);
  await assert.rejects(f.context.submitPackedTask(f.message('a')),/分包提交条件变化/);
  assert.equal(f.submits(),4,'an already imported attempt must never click twice');
  const downloads=[];
  Object.assign(f.context,{console:{log(){},warn(){},error(){}},URL,
    document:{querySelectorAll:()=>[]},getStorageKey:()=> 'project',
    mediaIdFromVideoUrl:url=>new URL(url).pathname.split('/').at(-1),
    wasVideoSeen:()=>false,isStaticShowcaseAsset:()=>false,
    async sendToExtension(action,data){assert.equal(action,'download');downloads.push(data);return {success:true,downloadId:100+downloads.length};}});
  installDownloadHelpers(f.context,source);
  const find=source.indexOf('  function findMediaBinding('),findEnd=source.indexOf('  // 定位属于当前任务行',find);
  vm.runInContext(source.slice(find,findEnd),f.context);
  const dl=source.indexOf('  async function triggerAutoDownload('),dlEnd=source.indexOf('  // 模拟真实用户鼠标悬停',dl);
  vm.runInContext(source.slice(dl,dlEnd),f.context);
  for(const index of [2,0,3,1]) {
    const task=f.state.tasks[index],binding=task.mediaBindings[0],media=binding.mediaIds[0];
    binding.downloadPath=task.download_path;binding.readyIds=[media];
    const url=`https://flow-content.google/video/${media}?Expires=9999999999&Signature=valid-signature`;
    assert.equal(await f.context.triggerAutoDownload(url,media,binding),true);
    assert.equal(downloads.at(-1).task.download_path,task.download_path);
    assert.equal(downloads.at(-1).task.submissionId,binding.submissionId);
  }
  assert.equal(new Set(downloads.map(d=>d.task.download_path)).size,4,'out-of-order results must download to their own attempts');
  const timeout=fixture();
  timeout.context.submitTaskToPage=async()=>{};
  await assert.rejects(timeout.context.submitPackedTask(timeout.message('e')),e=>e.code==='FLOW_BATCH_UNCONFIRMED');
  assert.equal(timeout.state.isRunning,false);
  assert.equal(timeout.state.tasks.length,1,'unknown paid results stay mapped for late receipts');
  // Existing result routing continues to use a distinct per-attempt path.
  const failedStart=source.indexOf('        failed: (taskId, submissionId, reason) => {');
  const failedEnd=source.indexOf('        account: async () => {',failedStart);
  const piece=source.slice(failedStart,failedEnd).trim().replace(/,\s*$/,'');
  let stopped=0;
  f.context.stopBatch=()=>{stopped++;};
  const api=vm.runInContext('({'+piece+'})',f.context);
  assert(api.failed(f.state.tasks[0].taskId,'s0','confirmed failed'));
  assert.equal(stopped,0,'one failure cannot stop another packed submission');
  assert.equal(f.state.tasks[1].status[0],'generating');
  console.log('Credit pack bridge: 4 in-flight results, independent paths, no duplicate send, bounded missing receipt and isolated failure passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
