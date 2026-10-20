const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8').replace("import './background.js';",'');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture(timing={}) {
  let now=100000,id=0;
  const data={lzxConfig:{token:'x'.repeat(40),port:18189,profile:'Profile 23',project:'https://flow.google.com/project/fixture'},lzxBrowserId:'stable-browser',lzxTiming:timing};
  const sockets=[],sent=[],writes=[],commands=[],intervals=new Map(),timeouts=new Map(),hooks={};
  const event=name=>({addListener:fn=>hooks[name]=fn});
  class Socket {static OPEN=1;constructor(url){this.url=url;this.readyState=0;this.closes=0;sockets.push(this);}send(value){sent.push(JSON.parse(value));}close(){this.closes++;this.readyState=3;}}
  const chrome={runtime:{getManifest:()=>({version:'1.1.22'}),onMessage:event('message'),onStartup:event('startup'),onInstalled:event('installed')},
    storage:{onChanged:event('changed'),local:{get:async keys=>Object.fromEntries(keys.filter(key=>key in data).map(key=>[key,structuredClone(data[key])])),
      set:async value=>{writes.push(value);Object.assign(data,structuredClone(value));}}},
    tabs:{query:async()=>[],sendMessage:async(...args)=>commands.push(args)},
    alarms:{create(){},onAlarm:event('alarm')},downloads:{search:async()=>[],onChanged:event('downloads')},action:{onClicked:event('action')}};
  const context={chrome,WebSocket:Socket,crypto,URL,Date:{now:()=>now},console:{log(){},warn(){},error(){}},
    setInterval(fn,ms){const key=++id;intervals.set(key,{fn,ms});return key;},clearInterval(key){intervals.delete(key);},
    setTimeout(fn,ms){const key=++id;timeouts.set(key,{fn,ms});return key;},clearTimeout(key){timeouts.delete(key);}};
  vm.createContext(context);vm.runInContext(source,context);await flush();
  return {data,sockets,sent,writes,commands,intervals,timeouts,hooks,
    advance(ms){now+=ms;},
    check(){for(const timer of [...intervals.values()])if(timer.ms===1000)timer.fn();},
    async reconnect(){const [key,timer]=[...timeouts.entries()].at(-1);timeouts.delete(key);now+=timer.ms;timer.fn();await flush();},
    async open(socket=sockets.at(-1)){socket.readyState=1;socket.onopen();await flush();},
    async message(message,socket=sockets.at(-1)){socket.onmessage({data:JSON.stringify(message)});await flush();}};
}
(async()=>{
  const hung=await fixture();assert.equal(hung.sockets.length,1);
  hung.advance(14999);hung.check();assert.equal(hung.sockets[0].closes,0);
  hung.advance(1);hung.check();assert.equal(hung.sockets[0].closes,1,'CONNECTING without onclose cannot wedge reconnect forever');
  await hung.reconnect();assert.equal(hung.sockets.length,2);
  assert.equal(hung.writes.filter(value=>value.lzxBrowserId).length,0,'reconnect keeps the durable browser identity');
  await hung.open();assert.equal(hung.sent.at(-1).browser_id,'stable-browser');
  hung.advance(15000);hung.check();assert.equal(hung.sockets.at(-1).closes,1,'an open socket without desktop welcome must recover too');

  const live=await fixture();await live.open();await live.message({type:'welcome'});
  live.advance(80000);await live.message({type:'pong'});
  live.advance(89000);live.check();assert.equal(live.sockets[0].closes,0,'pong renews only the transport lease');
  live.advance(1000);live.check();assert.equal(live.sockets[0].closes,1,'a silently dead OPEN socket is retired');
  await live.reconnect();await live.open();await live.message({type:'welcome'});
  const current=live.sockets.at(-1),old=live.sockets[0];old.onclose();
  live.advance(15000);live.check();assert.equal(current.closes,0,'a stale socket close must not retire its replacement');
  assert(!live.sent.some(value=>value.action==='run_tasks'));
  assert.equal(live.commands.length,0,'transport recovery must not open, refresh, or generate on a page');

  const custom=await fixture({bridge_handshake_timeout_seconds:2,bridge_heartbeat_seconds:1,bridge_heartbeat_timeout_seconds:5});
  custom.advance(2000);custom.check();assert.equal(custom.sockets[0].closes,1,'saved desktop handshake timeout is honored');
  await custom.reconnect();await custom.open();await custom.message({type:'welcome'});
  custom.advance(5000);custom.check();assert.equal(custom.sockets.at(-1).closes,1,'saved desktop silence timeout is honored');
  assert.deepEqual(custom.data.lzxConfig,live.data.lzxConfig,'recovery never resets pairing or account data');
  console.log('PASS: hung connect, missing welcome, dead heartbeat, stale socket, durable identity and configurable watchdogs');
})().catch(error=>{console.error(error);process.exitCode=1;});
