// Execute the built detector and adapter against isolated page fixtures only.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const project='https://flow.google.com/project/ours';
const bridge=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_bridge/injects/ui.js'),'utf8');
const check=bridge.slice(bridge.indexOf('  function checkForUnusualActivity()'),bridge.indexOf('  function playAlarmSound()'));
let alerts=[];const messages=[],hooks={};
const window={postMessage(value){messages.push(value.data);},addEventListener(name,fn){hooks[name]=fn;}};window.top=window;
const context={window,location:{origin:'https://flow.google.com',href:project},
  document:{body:{textContent:'Google Flow 网页提示我们发现了一些异常活动。A prompt about unusual activity.'},documentElement:{},
    querySelector(){return null;},querySelectorAll(){return alerts;}},
  getComputedStyle(node){return {display:'block',visibility:'visible',opacity:'1',...node.css};},
  MutationObserver:class{observe(){}},setInterval(fn){hooks.tick=fn;},console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/page.js'),'utf8'),context);
vm.runInContext(check,context);
const api=window.__lzxFlowTransport;
function alert(text,options={}){
  const node={textContent:text,isConnected:true,getClientRects(){return options.rectHidden?[]:[{}];},
    closest(selector){return (selector.includes('#flow-')&&(options.plugin||options.editable)) ||
      (selector.includes('[hidden]')&&options.hiddenAncestor) || null;},
    getAttribute(){return null;},css:options.css,parentElement:options.parent};
  if(options.nestedPrompt)node.cloneNode=()=>{
    const copy={textContent:text,querySelectorAll(){return [{remove(){copy.textContent=options.siteText||'';}}];}};return copy;
  };
  return node;
}
assert.equal(context.checkForUnusualActivity(),false,'own hidden warning and prompt text are not a site warning');
assert.equal(api.sitePaused(),false);assert.equal(messages.filter(v=>v.type==='site_pause').length,0);
for(const options of [{plugin:true},{editable:true},{rectHidden:true},{hiddenAncestor:true},
    {css:{visibility:'hidden'}},{css:{opacity:'0'}},{nestedPrompt:true},{parent:{css:{opacity:'0'}}}]){
  alerts=[alert('We detected unusual activity',options)];
  assert.equal(context.checkForUnusualActivity(),false,JSON.stringify(options));
  assert.equal(api.sitePaused(),false,'second check must reject a false warning too');
}
const hiddenChild=alert('unusual activity',{css:{display:'none'}});
const container=alert('unusual activity');
container.querySelectorAll=()=>[hiddenChild];
container.cloneNode=()=>{const copy={textContent:'unusual activity',querySelectorAll(){return [{remove(){copy.textContent='';}}];}};return copy;};
alerts=[container];assert.equal(context.checkForUnusualActivity(),false,'hidden descendants do not contribute warning text');
for(const text of ['We detected unusual activity','我们发现了一些异常活动','偵測到不尋常的活動']){
  alerts=[alert(text)];assert.equal(context.checkForUnusualActivity(),true);
  assert.equal(api.sitePaused(),true);const event=messages.at(-1);
  assert.equal(event.source,'visible_site_alert');assert.equal(event.code,'FLOW_SITE_UNUSUAL_ACTIVITY');
  assert.equal(event.url,project);assert.equal(typeof event.page_instance,'string');
}
alerts=[];assert.equal(api.sitePaused(),false,'an alert removed before dispatch cannot pause the queue');
console.log('Flow site alerts: hidden self-alarm, prompt and invisible nodes ignored; real visible warnings retain safety pause.');
