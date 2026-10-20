const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
(async () => {
  const hooks = {}, messages = []; let alerts = [], paid = 0;
  const window = {postMessage: value => messages.push(value.data), addEventListener: (name, callback) => hooks[name] = callback};
  window.top = window;
  const context = {window, location: {origin:'https://labs.google', href:'https://labs.google/project/one'},
    document: {documentElement: {}, body: {textContent:'prompt: account disabled'}, querySelector: () => null,
      querySelectorAll: selector => selector.includes('[role="alert"]') ? alerts.map(textContent => ({textContent})) : []},
    MutationObserver: class {observe() {}}, setInterval: callback => hooks.tick = callback};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8'),context);
  window.__lzxFlowTransport.attach(async () => paid++, () => ({tasks: []}), {});
  hooks.tick(); assert.equal(messages.filter(m => m.type === 'account_issue').length, 0, 'prompt text must not classify the account');
  alerts = ['Your Google account has been disabled'];
  hooks.tick(); hooks.tick();
  assert.equal(messages.filter(m => m.type === 'account_issue').length, 1, 'same alert is emitted once');
  assert.equal(messages.find(m => m.type === 'account_issue').code, 'FLOW_ACCOUNT_DISABLED');
  await hooks.message({source:window, origin:context.location.origin, data:{sender:'lzx-flow-extension',
    message:{action:'run_tasks',request_id:'disabled',params:{}}}});
  assert.equal(paid, 0);
  assert.equal(messages.find(m => m.request_id === 'disabled').code,'FLOW_ACCOUNT_DISABLED');
  alerts = ['Request timed out, try again']; hooks.tick();
  assert.equal(messages.filter(m => m.type === 'account_issue').length, 1, 'a timeout is not a ban');
  alerts = ['Please sign in to continue']; hooks.tick();
  assert.equal(messages.filter(m => m.type === 'account_issue').at(-1).code,'FLOW_ACCOUNT_SIGNED_OUT');
  console.log('Flow account isolation: explicit alerts only, no prompt/timeout false ban and no paid click while disabled passed.');
})().catch(error => {console.error(error);process.exitCode=1;});
