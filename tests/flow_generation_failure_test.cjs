const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8');
const events=[],hooks={},polls=[],failures=[];
const project='https://flow.google.com/project/failure',task={taskId:'task',download_path:'FlowBridge/'+'a'.repeat(32)+'/result.mp4',
  mediaBindings:[{schema:2,source:'generation_response',taskId:'task',submissionId:'submission',mediaIds:['new-media'],readyIds:[]}]};
const window={postMessage:value=>events.push(value.data),addEventListener:(name,fn)=>hooks[name]=fn};window.top=window;
let cards=[];const context={window,location:{href:project,origin:'https://flow.google.com'},Date,console,
  document:{documentElement:null,readyState:'complete',addEventListener(){},querySelector:()=>null,
    querySelectorAll:selector=>selector.includes('flow-video-tile')?cards:[]},
  setInterval:fn=>{polls.push(fn);return polls.length;},clearInterval(){}};
vm.createContext(context);vm.runInContext(source,context);
window.__lzxFlowTransport.attach(async()=>{},()=>({url:project,running:false,tasks:[task]}),{initialized:()=>true,failed:(...args)=>failures.push(args)});
hooks['flow-task-media-failed']({detail:{mediaId:'old-media',source:'server_status'}});
assert.equal(failures.length,0,'a historical failed video must not retry this job');
cards=[{textContent:'Failed Sorry, this video failed to generate. You will be refunded.',getAttribute:key=>key==='data-media-id'?'old-media':null}];
polls.at(-1)();assert.equal(failures.length,0);
cards[0].getAttribute=key=>key==='data-media-id'?'new-media':null;
polls.at(-1)();polls.at(-1)();
assert.equal(failures.length,1);assert.equal(failures[0][0],'task');assert.equal(failures[0][1],'submission');
const failed=events.filter(event=>event?.type==='generation_failed');assert.equal(failed.length,1);
assert.equal(failed[0].mediaId,'new-media');assert.equal(failed[0].failure_source,'refund_card');
assert.deepEqual(Array.from(task.mediaBindings[0].failedIds),['new-media']);
console.log('Flow failure recovery: exact bound media only, failed-and-refunded cards, historic failure rejection, original controller stopped once and no direct paid replay passed.');
