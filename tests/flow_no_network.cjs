// Real network is never part of offline browser regressions. VM tests supply mocks.
const deny=()=>{throw new Error('Unmocked network prohibited in Flow browser tests');};
globalThis.fetch=deny;
require('node:net').Socket.prototype.connect=deny;
require('node:tls').connect=deny;
for(const name of ['node:http','node:https']) {
  const api=require(name);api.request=deny;api.get=deny;
}
