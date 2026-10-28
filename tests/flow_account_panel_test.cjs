const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.FLOW_TEST_SOURCE_ROOT || path.join(__dirname,'..');
const source=fs.readFileSync(path.join(root,'browser_extensions/flow_adapter/page.js'),'utf8');
function fixture({stuck=false,escape=false,hidden=false}={}) {
  let opened=true,now=0,closes=0,escapes=0;
  const bad=label=>({textContent:label,click(){throw Error('must never click '+label);}});
  const close={textContent:'close',click(){closes++;if(!stuck)opened=false;}};
  const panel={get isConnected(){return opened;},getClientRects:()=>hidden?[]:[{}],
    querySelector:()=>escape?null:close,querySelectorAll:()=>[bad('升级'),bad('登出所有帳戶'),...(!escape?[close]:[])],
    dispatchEvent(event){assert.equal(event.key,'Escape');escapes++;if(!stuck)opened=false;}};
  const window={postMessage(){},addEventListener(){}};window.top=window;
  const document={documentElement:null,addEventListener(){},dispatchEvent(){},
    querySelector:selector=>selector==='flow-account-panel button.close-button'?null:opened?panel:null,
    querySelectorAll:()=>opened?[panel]:[]};
  const ctx={window,document,location:{origin:'https://flow.google.com',href:'https://flow.google.com/project/panel'},
    Date:{now:()=>now},setInterval(){},setTimeout(fn,ms){now+=ms;fn();},KeyboardEvent:class{constructor(_type,attrs){Object.assign(this,attrs);}}};
  vm.runInNewContext(source,ctx);
  return {close:window.__lzxFlowTransport.closeAccountPanel,stats:()=>({opened,now,closes,escapes})};
}
(async()=>{
  const normal=fixture();await normal.close();assert.equal(normal.stats().opened,false);assert.equal(normal.stats().closes,1);
  await normal.close();assert.equal(normal.stats().closes,1,'closed menus are left alone');
  const escape=fixture({escape:true});await escape.close();assert.equal(escape.stats().opened,false);assert.equal(escape.stats().escapes,1);
  const hidden=fixture({hidden:true});await hidden.close();assert.equal(hidden.stats().closes,0);
  const stuck=fixture({stuck:true});await assert.rejects(stuck.close(),error=>error.code==='FLOW_ACCOUNT_PANEL_BLOCKED');
  assert(stuck.stats().now>=3000&&stuck.stats().now<=3200);assert.equal(stuck.stats().closes,3);
  console.log('Account panel: scoped close only, Escape fallback, hidden menu exclusion and bounded blocked-upload failure passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
