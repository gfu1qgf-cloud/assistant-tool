const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const start=source.indexOf('const foregroundReplies = new Map();'),end=source.indexOf('async function nativeForeground(',start);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
  const messages=[],focus=[],revoked=[],leases=new Map();
  const c={uiLeases:leases,browserOptions:{ui_mutex:true,focus_before_action:true},authenticated:true,connectionEpoch:1,
    config:{project:'https://flow.google.com/project/ours'},send:m=>messages.push(m),sameProjectTab:(tab,url)=>tab.url===url,
    timingMs:(_key,fallback)=>fallback*1000,setTimeout,clearTimeout,
    chrome:{tabs:{async get(){return {id:7,url:'https://flow.google.com/project/ours'};},async sendMessage(id,m){revoked.push(m);}}},
    async readyWorkTab(id,m){focus.push(m);return 'focused';}};
  vm.createContext(c);vm.runInContext(source.slice(start,end),c);
  const token='a'.repeat(32);let deadline=Date.now()/1000+30;
  const permit=c.acquireUiLease(7,'submit',token,'b'.repeat(32));await flush();
  await c.uiMutexResult({token,state:'granted',renewal:true,expires_at:deadline});assert((await permit).renewal);
  await c.uiMutexResult({token,state:'granted',renew_seq:1});
  assert.equal(leases.get(token).expires_at,deadline);assert(leases.get(token).held);
  await c.uiMutexResult({token,state:'granted',renew_seq:2,expires_at:deadline+30});
  assert.equal(leases.get(token).expires_at,deadline+30);assert.equal(focus.length,1,'renew never activates the window again');
  assert.equal(typeof c.renewUiLease,'undefined','there is no independent request, promise or timeout channel');
  assert(leases.get(token).held);assert.equal(leases.get(token).expires_at,deadline+30);
  const guard=c.__lzxUiNativeGuard(7,token);assert.equal(leases.get(token).nativeActive,1);
  await c.uiMutexResult({token,state:'stopping'});
  assert(leases.get(token).revoking);assert(!messages.some(m=>m.type==='ui_mutex_release'));
  assert.throws(()=>guard.check(),/权限已结束/);
  c.releaseUiLease(token,false,true);
  assert(leases.has(token),'a page stop cannot acknowledge an in-flight native command');
  guard.done();assert(!leases.has(token));assert.equal(messages.at(-1).type,'ui_mutex_release');assert(messages.at(-1).stopped);
  const count=messages.length;await c.uiMutexResult({token,state:'granted',renew_seq:4,expires_at:deadline+999});assert.equal(messages.length,count);
  // A focus call already in flight must finish before stop acknowledgement.
  let complete;c.readyWorkTab=()=>new Promise(r=>complete=r);
  const next='c'.repeat(32),pending=c.acquireUiLease(7,'prepare',next);await flush();
  const granting=c.uiMutexResult({token:next,state:'granted',renewal:true,expires_at:deadline});await flush();
  await c.uiMutexResult({token:next,state:'stopping'});assert(leases.has(next));
  complete('focused');await granting;assert(!(await pending).ok);assert(!leases.has(next));
  assert(messages.at(-1).stopped);
  console.log('Renewal bridge: optional failure/timeout, no refocus, native quiescence, delayed focus stop and stale replies passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
