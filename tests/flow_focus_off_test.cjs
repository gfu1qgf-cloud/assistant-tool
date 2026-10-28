// Automatic quiet mode covers ordinary actions, reconnect and reload recovery.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../browser_extensions/flow_adapter/background.js'),'utf8');
const project='https://flow.google.com/project/ours';
function fixture() {
  const tabs=[],windows=[],events=[],reloads=[];
  const entry={version:'1.1.46',mode:'reload_only',project,tabId:7};
  const context={URL,console:{warn(){},log(){},error(){}},browserOptions:{ui_mutex:false,focus_before_action:false},
    config:{project},closingBrowser:false,commandsInFlight:0,applyBrowserOptions:async()=>{},ensureTab:async()=>7,
    sameProjectTab:(tab,url)=>tab.url===url,projectUrl:url=>url===project,
    send:message=>events.push(message),nativeForeground:async()=>{windows.push('native');return 'focused';},
    findProjectTab:async()=>({id:7,url:project}),
    chrome:{runtime:{getManifest:()=>({version:'1.1.46'})},
      tabs:{get:async()=>({id:7,url:project,windowId:1,active:false}),
        update:async(id,options)=>{tabs.push({id,options});return {id,url:project,windowId:1};},reload:async id=>reloads.push(id)},
      windows:{get:async()=>({state:'minimized',focused:false}),update:async(id,options)=>windows.push({id,options})},
      storage:{local:{get:async()=>({lzxReloadSetup:entry}),remove:async()=>{}}}}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('async function readyWorkTab('),source.indexOf('async function receive(')),context);
  vm.runInContext(source.slice(source.indexOf('async function receive('),source.indexOf('async function closeExhausted(')),context);
  vm.runInContext(source.slice(source.indexOf('async function restoreSetupTab('),source.indexOf('chrome.tabs?.onCreated?',source.indexOf('async function restoreSetupTab('))),context);
  return {context,tabs,windows,events,reloads};
}
(async()=>{
  const quiet=fixture();
  assert.equal(await quiet.context.readyWorkTab(7,{action:'prepare'}),'disabled');
  await quiet.context.receive({action:'open_project',request_id:'open',params:{url:project}});
  assert.equal(quiet.events.at(-1).success,true);assert.equal(quiet.tabs.length,0);assert.equal(quiet.windows.length,0);
  await quiet.context.restoreSetupTab();
  assert.equal(quiet.tabs.length,0,'reload-only recovery leaves URL and active state untouched');assert.deepEqual(quiet.reloads,[7]);assert.equal(quiet.windows.length,0);
  const manual=fixture();
  await manual.context.readyWorkTab(7,{action:'focus_window',foreground_token:'a'.repeat(32)});
  assert.equal(manual.tabs.length,1);assert(manual.windows.some(item=>item.options?.focused===true));
  assert(manual.windows.includes('native'));
  const enabled=fixture();enabled.context.browserOptions.focus_before_action=true;
  await enabled.context.receive({action:'open_project',request_id:'open',params:{url:project}});
  assert.equal(enabled.tabs.at(-1).options.active,true);assert.equal(enabled.windows.at(-1).options.focused,true);
  const passive=fixture();passive.context.browserOptions.focus_before_action=true;
  await passive.context.readyWorkTab(7,{action:'get_points',foreground_skip:true});
  assert.equal(passive.tabs.length,0);assert.equal(passive.windows.length,0);
  console.log('Focus OFF: ordinary UI, minimized windows, reconnect and reload stay quiet; explicit manual activation and enabled mode still work.');
})().catch(error=>{console.error(error);process.exitCode=1;});
