const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  let summary = 'Omni 1.1 Flash', time = 100000, email = 'fixture@example.test', observe;
  const messages = [], hooks = {};
  const window = {postMessage(value) {messages.push(value.data);}, addEventListener(name, callback) {hooks[name] = callback;}};
  window.top = window;
  const context = {window, Date: {now() {return time;}}, location: {origin: 'https://labs.google', href: 'https://labs.google/project/fixture'},
    document: {documentElement: {}, querySelector(selector) {
      if (selector === 'button.settings-trigger-button') return {textContent: summary};
      if (selector === 'flow-account-panel') return email ? {textContent: email,
        querySelectorAll(){return [{textContent:email,children:[]}];}} : null;
      return null;
    }, querySelectorAll() {return [];}}, MutationObserver: class {constructor(callback) {observe = callback;} observe() {}}, setInterval() {}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_adapter/page.js'), 'utf8'), context);
  const transport = window.__lzxFlowTransport;
  const settings = {videoModel: 'omni_1_1_flash', aspectRatio: 'ASPECT_RATIO_9_16', resolution: 'VIDEO_RESOLUTION_720P', count: 1, duration: 8};
  function node(text, checked = true) {
    return {textContent: text, classList: {contains() {return checked;}}, getAttribute() {return null;}, querySelector() {return null;}};
  }
  function group(label, ...nodes) {return {getAttribute() {return label;}, querySelectorAll() {return nodes;}};}
  function panel(model = 'Omni 1.1 Flash', count = 'x1', resolution = '720p', aspect = 'crop_9_16 9:16') {
    const groups = [group('Aspect ratio', node(aspect), node('16:9', false)),
      group('Resolution', node(resolution), node('360p', false)), group('Outputs', node(count), node('x2', false)),
      group('Duration', node('8s'), node('10s', false))];
    return {querySelector(selector) {return selector === '.model-select-trigger-content' ? node(model) : null;}, querySelectorAll() {return groups;}, groups};
  }
  assert.throws(() => transport.verifySettings(settings), /比例.*分辨率.*x1/, 'model-only summary is insufficient by itself');
  const selected = panel();
  assert.equal(transport.verifySettings(settings, selected), true, 'selected fields outside compact summary must be accepted');
  for (const [view, error] of [[panel('Veo 3.1 Fast'), /Omni/], [panel(undefined, 'x2'), /x1/],
      [panel(undefined, undefined, '360p'), /分辨率/], [panel(undefined, undefined, undefined, '16:9'), /比例/]]) {
    assert.throws(() => transport.verifySettings(settings, view), error, 'selected wrong option must never fall back to summary');
  }
  const ambiguous = panel(); ambiguous.groups[1] = group('Resolution', node('720p'), node('360p'));
  assert.throws(() => transport.verifySettings(settings, ambiguous), /分辨率/);
  summary = 'Omni 1.1 Flash 720p crop_9_16 × 1 8s';
  assert.equal(transport.verifySettings(settings), true, 'multiplication sign count is a supported display variant');
  summary = 'Omni 1.1 Flash 720p 9:16 x10 8s';
  assert.throws(() => transport.verifySettings(settings), /x1/);

  const invoke = (id, params = settings) => hooks.message({source: window, origin: context.location.origin,
    data: {sender: 'lzx-flow-extension', message: {action: 'prepare', request_id: id, params}}});
  summary = 'Omni 1.1 Flash';
  transport.attach(async () => {}, () => ({url: context.location.href, tasks: []}), {
    async prepare(params) {transport.verifySettings(params, selected);}
  });
  await invoke('open-panel-proof');
  assert.equal(messages.find(value => value.request_id === 'open-panel-proof').success, true,
    'same invocation may use selected proof after settings panel closes');
  const instance = messages.find(value => value.request_id === 'open-panel-proof').data.page_instance;
  assert.equal(typeof instance, 'string'); assert(instance.length > 5);
  assert(messages.some(value => value.type === 'page_status' && value.page_instance === instance),
    'the prepare receipt and document status must carry the same page identity');
  assert(messages.some(value => value.type === 'snapshot' && value.data.page_instance === instance),
    'a read-only snapshot carries the same document identity');
  transport.attach(async () => {}, () => ({tasks: []}), {async prepare() {}});
  await invoke('no-old-proof');
  assert.equal(messages.find(value => value.request_id === 'no-old-proof').success, false,
    'proof from a previous request must not authorize new preparation');
  transport.attach(async () => {}, () => ({tasks: []}), {
    async prepare(params) {transport.verifySettings(params, selected); time += 6000;}
  });
  await invoke('expired-proof');
  assert.equal(messages.find(value => value.request_id === 'expired-proof').success, false);
  transport.attach(async () => {}, () => ({tasks: []}), {
    async prepare(params) {transport.verifySettings(params, selected); context.location.href += '-changed';}
  });
  await invoke('wrong-project-proof');
  assert.equal(messages.find(value => value.request_id === 'wrong-project-proof').success, false);

  // One explicit query binds the session. Preparing and submitting must not
  // repeatedly open the account menu or query its credit balance.
  const fresh = async value => {email = value; observe(); email = ''; observe();};
  await fresh('fresh@example.test');
  transport.send({action: 'get_points_response', data: {remaining_points: 50, email: 'fresh@example.test'}});
  summary = 'Omni 1.1 Flash 720p 9:16 x1 8s';
  transport.attach(async () => {}, () => ({tasks: []}), {async prepare() {}, async account() {throw Error('known session must not probe');}});
  await invoke('closed-account-panel');
  assert.equal(messages.find(value => value.request_id === 'closed-account-panel').data.email, 'fresh@example.test');
  let runs = 0, accountProbes = 0;
  window.__lzxReferenceIdentity={init(){},ready:()=>true,resolve(task){assert.equal(task.reference.upload_id,'a'.repeat(32));}};
  transport.attach(async message => {if (message.action === 'run_tasks') runs++;}, () => ({tasks: []}), {
    async account() {accountProbes++; await fresh('fresh@example.test');}
  });
  const run = id => hooks.message({source: window, origin: context.location.origin,
    data: {sender: 'lzx-flow-extension', message: {action: 'run_tasks', request_id: id,
      params: {expected_account: 'fresh@example.test', batch_id:'a'.repeat(32), tasks:[{reference:{upload_id:'a'.repeat(32)}}]}}}});
  await run('fresh-submit'); assert.equal(runs, 1);
  await run('same-session'); assert.equal(runs, 2);
  assert.equal(accountProbes, 0, 'known session must not reopen the credit/account menu');
  await fresh('changed@example.test'); await run('changed-account');
  assert.equal(runs, 2, 'passively observed account changes must not receive the paid request');
  await hooks.message({source: window, origin: context.location.origin,
    data: {sender: 'lzx-flow-extension', message: {action: 'get_points', request_id: 'explicit-refresh'}}});
  transport.send({action: 'get_points_response', data: {}});
  transport.attach(async () => {}, () => ({tasks: []}), {async account() {accountProbes++;}});
  await run('failed-explicit-refresh');
  assert.equal(messages.find(value => value.request_id === 'failed-explicit-refresh').success, false,
    'an explicit failed identity refresh must invalidate the old session');

  await fresh('fresh@example.test');
  transport.send({action:'get_points_response',data:{email:'fresh@example.test',remaining_points:50}});
  let referenceCalls=0, invalidations=0, referenceScope;
  window.__lzxReferenceIdentity.invalidate=()=>invalidations++;
  window.__lzxReferenceIdentity.upload=async (_params,scope)=>{
    referenceCalls++;referenceScope=scope;await scope.guard();return {reference:{verified:true},reused:true};
  };
  const upload=(id,expected='fresh@example.test')=>hooks.message({source:window,origin:context.location.origin,
    data:{sender:'lzx-flow-extension',message:{action:'upload_images',request_id:id,params:{expected_account:expected}}}});
  await upload('reuse-scope');
  assert.equal(messages.find(value=>value.request_id==='reuse-scope').success,true);
  assert.equal(referenceScope.account,'fresh@example.test');assert(referenceScope.page_instance);
  await upload('wrong-reference-account','other@example.test');
  assert.equal(referenceCalls,1,'wrong-account requests must not upload or reuse any asset');
  assert.equal(messages.find(value=>value.request_id==='wrong-reference-account').success,false);
  window.__lzxReferenceIdentity.upload=async (_params,scope)=>{await fresh('changed@example.test');await scope.guard();};
  await upload('changed-during-reference');
  assert.equal(messages.find(value=>value.request_id==='changed-during-reference').success,false);
  assert.equal(invalidations,2,'both wrong-account cases must discard old asset proofs');
  window.__lzxReferenceIdentity.ready=()=>false;
  await upload('uninitialized-reference');
  assert.equal(messages.find(value=>value.request_id==='uninitialized-reference').code,'FLOW_REFERENCE_SCRIPT_NOT_READY');

  const built = fs.readFileSync(path.join(__dirname, '../browser_extensions/flow_bridge/injects/ui.js'), 'utf8');
  const check = built.indexOf('await window.__lzxFlowTransport.waitForSettings(settings)');
  const close = built.indexOf('// 4. 关闭设置面板');
  assert.ok(check > 0 && check < close, 'host must verify actual selected controls before closing their panel');
  assert.ok(!built.includes('if (window.__lzxFlowTransport && !window.__lzxFlowTransport.activeEmail()) pts = null;'),
    'bridge must not discard upstream credit readings based on its narrower DOM reader');
  assert.ok(built.replace(/\r\n/g, '\n').includes('account: async () => {\n          cachedEmail = ""; cachedPoints = null; lastProbeAt = 0;'));
  console.log('Flow settings/account: selected fields, session reuse without menu polling, explicit refresh and account-change guards passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
