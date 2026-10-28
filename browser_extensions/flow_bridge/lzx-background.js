// Local screenshot comparison. Only a unique, newly observed image ID may win.
(() => {
  const texture = pixels => {
    const gray=[];
    for(let i=0;i<pixels.length;i+=4)gray.push((pixels[i]+pixels[i+1]+pixels[i+2])/3);
    const mean=gray.reduce((a,b)=>a+b,0)/gray.length;
    return {gray,mean,std:Math.sqrt(gray.reduce((sum,v)=>sum+(v-mean)**2,0)/gray.length)};
  };
  function compare(a,b) {
    if(a.length!==4096 || b.length!==4096)return null;
    const x=texture(a),y=texture(b);
    if(x.std<12 || y.std<12)return null; // Flat/blank thumbnails are not identity.
    let difference=0,covariance=0,edges=0,agree=0;
    for(let i=0;i<x.gray.length;i++) {
      covariance+=(x.gray[i]-x.mean)*(y.gray[i]-y.mean);
      for(let c=0;c<3;c++)difference+=Math.abs(a[i*4+c]-b[i*4+c]);
      if(i%32<31) {
        const dx=x.gray[i+1]-x.gray[i],dy=y.gray[i+1]-y.gray[i];
        if(Math.abs(dx)>5) {edges++;if(dx*dy>0)agree++;}
      }
    }
    return {error:difference/(x.gray.length*3*255),correlation:covariance/(x.gray.length*x.std*y.std),
      edges:edges>=20?agree/edges:0};
  }
  function choose(scored,before) {
    const ordered=scored.filter(item=>item.score).sort((a,b)=>a.score.error-b.score.error);
    const best=ordered[0],second=ordered[1];
    if(!best || before.includes(best.media_id) || best.score.error>.055 || best.score.correlation<.96 || best.score.edges<.9 ||
       second && second.score.error-best.score.error<.025)return null;
    return {media_id:best.media_id,source:'local_screenshot',score:best.score};
  }
  async function match(reference,candidates,before,capture) {
    const source=await createImageBitmap(await (await fetch(reference.thumbnail)).blob());
    let screen;
    try {
      screen=await createImageBitmap(await (await fetch('data:image/png;base64,'+capture.data)).blob());
      const canvas=new OffscreenCanvas(32,32),ctx=canvas.getContext('2d',{willReadFrequently:true});
      const sx=screen.width/capture.width,sy=screen.height/capture.height,scored=[];
      for(const item of candidates) {
        let left=0,top=0,width=source.width,height=source.height;
        if(item.fit==='cover') {
          const aspect=item.width/item.height;
          if(width/height>aspect) {left=(width-height*aspect)/2;width=height*aspect;}
          else {top=(height-width/aspect)/2;height=width/aspect;}
        }
        ctx.clearRect(0,0,32,32);ctx.drawImage(source,left,top,width,height,0,0,32,32);
        const expected=ctx.getImageData(0,0,32,32).data;
        ctx.clearRect(0,0,32,32);ctx.drawImage(screen,item.x*sx,item.y*sy,item.width*sx,item.height*sy,0,0,32,32);
        scored.push({media_id:item.media_id,score:compare(expected,ctx.getImageData(0,0,32,32).data)});
      }
      return choose(scored,before);
    } finally {source.close();screen?.close();}
  }
  globalThis.__lzxVisualUpload={compare,choose,match};
})();

import './background.js';

let socket, heartbeat, connecting = false, retryAt = 0, retryDelay = 1000, connectionEpoch = 0, authenticated = false, booting = true;
let ownedTab = null, activeBatch = '', config = {}, browserId = '', opening, resumingSetup;
let closingBrowser = false, commandsInFlight = 0;
let absenceReportedAt = 0;
let connectionWatchdog, socketStartedAt = 0, lastServerAt = 0;
let timingValues = {};
let browserOptions = {focus_before_action:false,visual_upload_fallback:false,ui_mutex:false};
const uiLeases = new Map();
const visualUploads = new Map();
const nativeUploads = new Map();
const uploadPath = value => typeof value==='string'?value.replace(/\\/g,'/').toLowerCase():'';
globalThis.__lzxNativeUploadGuard=(tabId,paths)=>{
  const entry=nativeUploads.get(tabId);
  const check=()=>{
    if(!authenticated || tabId!==ownedTab || !entry || nativeUploads.get(tabId)!==entry ||
        entry.epoch!==connectionEpoch || entry.project!==config.project ||
        Date.now()-entry.at>Math.min(3600000,8*(timingMs('page_upload_max_seconds',180)+2*timingMs('page_controls_wait_seconds',45)+180000)) ||
        !Array.isArray(paths) || !paths.length || !paths.every(path=>uploadPath(path)===entry.path))
      throw Object.assign(Error('本地图片不属于主程序当前授权的上传，未读取文件'),{uploadDispatched:false});
  };
  check();return {check};
};
const downloadParkProgress = new Map();
async function applyBrowserOptions(raw, persist = true) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || typeof raw.focus_before_action !== 'boolean') return;
  const next = {focus_before_action:raw.focus_before_action,visual_upload_fallback:raw.visual_upload_fallback === true,ui_mutex:raw.ui_mutex === true};
  if (JSON.stringify(next) === JSON.stringify(browserOptions)) return;
  const mutexChanged=next.ui_mutex!==browserOptions.ui_mutex;
  browserOptions = next;
  if (!next.ui_mutex) cancelUiLeases('disabled');
  if (mutexChanged && ownedTab != null) {
    try {await chrome.tabs.sendMessage(ownedTab,{action:'lzxBrowserOptions',values:next},{frameId:0});} catch {}
  }
  if (persist) {
    try {await chrome.storage.local.set({lzxBrowserOptions:next});}
    catch (error) {console.warn('[AssistantFlow] 窗口操作设置未缓存：', error.message || error);}
  }
}
const timingMs = (key, fallback) => Math.max(1, Math.min(86400000,
  (typeof timingValues[key] === 'number' && Number.isFinite(timingValues[key]) && timingValues[key] > 0 ? timingValues[key] : fallback) * 1000));
function reconnectAlarm() {chrome.alarms.create?.('lzxReconnect', {periodInMinutes:Math.max(.5, timingMs('bridge_reconnect_alarm_seconds',30)/60000)});}
async function applyTiming(raw, persist = true) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
  const next = Object.fromEntries(Object.entries(raw).filter(([key,item]) => /^(bridge_|page_|setup_)/.test(key) &&
    typeof item === 'number' && Number.isFinite(item) && item > 0 && item <= 86400));
  if (JSON.stringify(next) === JSON.stringify(timingValues)) return;
  timingValues = next; retryDelay = timingMs('bridge_reconnect_initial_seconds',1); reconnectAlarm();
  if (authenticated) {
    clearInterval(heartbeat);
    heartbeat = setInterval(() => send({type:'ping'}), timingMs('bridge_heartbeat_seconds',20));
  }
  if (persist) await chrome.storage.local.set({lzxTiming:timingValues});
  for (const tab of await chrome.tabs.query({})) {
    if (projectUrl(tab.pendingUrl || tab.url)) {
      try {await chrome.tabs.sendMessage(tab.id, {action:'lzxTiming', values:timingValues}, {frameId:0});} catch {}
    }
  }
}
const setupClaims = new Map();
const configSignature = value => JSON.stringify(['token', 'port', 'profile', 'project', 'setup_id'].map(key => value?.[key] ?? null));
const projectUrl = value => {
  try { const url = new URL(value); return url.protocol === 'https:' &&
    ['labs.google', 'flow.google.com'].includes(url.hostname) && /\/project\//.test(url.pathname); }
  catch { return false; }
};
function sameProjectTab(tab, url) {
  // During Chrome startup url may still be chrome://newtab/. A pending
  // navigation is authoritative, including when the user is leaving a project.
  const current = tab?.pendingUrl || tab?.url;
  const projectPath=value=>new URL(value).pathname.replace(/\/$/,'').replace(/\/edit\/[A-Za-z0-9_-]+$/,'');
  return projectUrl(current) && new URL(current).hostname === new URL(url).hostname && projectPath(current) === projectPath(url);
}
async function findProjectTab(url) {
  const matches = (await chrome.tabs.query({})).filter(tab => sameProjectTab(tab, url));
  // Do not move an in-flight batch to another same-project tab just because
  // the user has brought a newer tab to the foreground.
  return matches.find(tab => tab.id === ownedTab) || matches.find(tab => tab.active) || matches[0];
}
async function local() { return chrome.storage.local.get(['lzxConfig', 'lzxBrowserId', 'lzxOwnedTab', 'lzxBatch', 'lzxOutbox', 'lzxDownloads', 'lzxBatches', 'lzxTiming', 'lzxBrowserOptions']); }
function send(message) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message)); }
async function ensureTab() {
  if (opening) return opening;
  opening = openTab();
  try { return await opening; } finally { opening = null; }
}
async function openTab() {
  const url = config.project;
  const epoch = connectionEpoch;
  if (!projectUrl(url)) throw new Error('本配置尚未保存 Flow 项目地址，请在软件的连接向导中手动添加；未选择其他网页。');
  // The desktop launcher owns navigation. The extension only adopts the
  // exact saved project; it must never create an inactive/background tab.
  const tab = await findProjectTab(url);
  if (epoch !== connectionEpoch) throw new Error('连接配置正在更新，已取消旧开页请求。');
  if (!tab) throw new Error('等待主程序打开本配置的 Flow 项目；扩展不会自行新建标签页。');
  ownedTab = tab.id;
  await chrome.storage.local.set({lzxOwnedTab: ownedTab});
  return ownedTab;
}
const pageBootstraps = new Map();
const foregroundReplies = new Map();
function cancelUiLeases(state) {
  for (const [token,entry] of uiLeases) {
    if(state==='disabled' && entry.renewal && (entry.held || entry.granting)) {
      revokeUiLease(token,state);continue;
    }
    send({type:'ui_mutex_cancel',token});
    entry.resolve?.({ok:state==='disabled',state});
    clearTimeout(entry.timer);
    chrome.tabs.sendMessage(entry.tabId,{action:'lzxUiRevoked',token,state},{frameId:0}).catch(()=>{});
  }
  if(state!=='disabled')uiLeases.clear();
  else for(const [token,entry] of uiLeases)if(!entry.revoking)uiLeases.delete(token);
}
function releaseUiLease(token,passiveWait=false,stopped=false) {
  const entry=uiLeases.get(token);
  if (!entry) return;
  if(entry.renewal && (entry.held || entry.granting)) {
    if(!stopped) {revokeUiLease(token,'expired');return;}
    entry.releasing=true;
    if(entry.nativeActive || entry.granting) {entry.pendingRelease={passiveWait};return;}
  }
  clearTimeout(entry.timer);uiLeases.delete(token);
  send({type:'ui_mutex_release',token,...(passiveWait && entry.held &&
    ['prepare','upload_images','submit'].includes(entry.operation)?{passive_wait:true}:{}),
    ...(stopped?{stopped:true}:{})});
}
function revokeUiLease(token,state='expired') {
  const entry=uiLeases.get(token);if(!entry)return;
  entry.revoking=true;clearTimeout(entry.timer);
  entry.resolve?.({ok:false,state:'expired'});
  chrome.tabs.sendMessage(entry.tabId,{action:'lzxUiRevoked',token,state,stopping:true},{frameId:0}).catch(()=>{});
  if(!entry.held && !entry.granting)releaseUiLease(token,false,true);
}
function armUiExpiry(token,entry) {
  clearTimeout(entry.timer);
  entry.timer=setTimeout(()=>{if(uiLeases.get(token)===entry)revokeUiLease(token);},
    Math.max(1,entry.expires_at*1000-Date.now()));
}
// Native drag/click promises can outlive their page bridge callback. Include
// them in the stop acknowledgement, and fence every later CDP input command.
globalThis.__lzxUiNativeGuard=(tabId,token)=>{
  if(!browserOptions.ui_mutex)return null;
  const entry=uiLeases.get(token);
  const check=()=>{
    if(!entry?.held || entry.tabId!==tabId || uiLeases.get(token)!==entry || entry.revoking || entry.releasing ||
        !authenticated || entry.epoch!==connectionEpoch || Date.now()>=entry.expires_at*1000)
      throw Object.assign(Error('前台 UI 操作权限已结束，停止旧原生操作'),{uploadDispatched:false});
  };
  check();entry.nativeActive=(entry.nativeActive || 0)+1;
  return {check,done(){entry.nativeActive--;if(entry.pendingRelease && !entry.nativeActive && !entry.granting)
    releaseUiLease(token,entry.pendingRelease.passiveWait,true);}};
};
async function acquireUiLease(tabId,operation,token,batchId='') {
  if (!browserOptions.ui_mutex) return {ok:true,state:'disabled'};
  if (!authenticated || !sameProjectTab(await chrome.tabs.get(tabId),config.project)) throw Error('原项目连接尚未确认，未执行页面操作。');
  if (uiLeases.has(token)) throw Error('页面操作互斥申请重复，未重复执行。');
  const entry={tabId,epoch:connectionEpoch,operation};
  const result=new Promise(resolve=>{
    entry.resolve=resolve;
    entry.timer=setTimeout(()=>{
      if(uiLeases.get(token)!==entry)return;
      releaseUiLease(token);resolve({ok:false,state:'expired'});
    },8*(timingMs('page_upload_max_seconds',180)+2*timingMs('page_controls_wait_seconds',45)+180000));
  });
  uiLeases.set(token,entry);
  send({type:'ui_mutex_request',token,operation,batch_id:batchId,url:config.project});
  return await result;
}
async function uiMutexResult(message) {
  const entry=uiLeases.get(message.token);
  if(!entry)return;
  if(Number.isInteger(message.renew_seq)) {
    if(entry.held && entry.renewal && !entry.revoking && !entry.releasing && entry.epoch===connectionEpoch &&
        Date.now()<entry.expires_at*1000 && Number.isFinite(message.expires_at) && message.expires_at>entry.expires_at) {
      entry.expires_at=message.expires_at;armUiExpiry(message.token,entry);
      chrome.tabs.sendMessage(entry.tabId,{action:'lzxCommand',message:{action:'ui_mutex_result',
        token:message.token,renew_seq:message.renew_seq,expires_at:entry.expires_at}},{frameId:0}).catch(()=>{});
    }
    return;
  }
  if(message.state==='stopping') {revokeUiLease(message.token);return;}
  if(message.state==='granted' && !entry.granting && !entry.held && !entry.revoking) {
    entry.renewal=message.renewal===true;entry.expires_at=message.expires_at;
    entry.granting=true;
    try {
      if(entry.epoch!==connectionEpoch || !sameProjectTab(await chrome.tabs.get(entry.tabId),config.project))throw Error('原项目已变化');
      entry.focus=browserOptions.focus_before_action || entry.operation==='focus_window' ?
        await readyWorkTab(entry.tabId,{action:'focus_window',request_id:message.token,foreground_token:message.token}) : 'disabled';
      if(uiLeases.get(message.token)!==entry || entry.revoking)return;
      entry.held=true;clearTimeout(entry.timer);
      if(entry.renewal)armUiExpiry(message.token,entry);
      entry.resolve({ok:true,state:'granted',focus:entry.focus,expires_at:message.expires_at,renewal:entry.renewal});
    } catch(error) {
      releaseUiLease(message.token);entry.resolve({ok:false,state:'error',error:String(error.message || error)});
    } finally {
      entry.granting=false;
      if(entry.revoking && !entry.held)releaseUiLease(message.token,false,true);
      else if(entry.pendingRelease && !entry.nativeActive)releaseUiLease(message.token,entry.pendingRelease.passiveWait,true);
    }
  } else if(message.state!=='granted') {
    if(entry.renewal && entry.held) {revokeUiLease(message.token,message.state);return;}
    releaseUiLease(message.token);
    entry.resolve({ok:message.state==='disabled',state:message.state});
    try{await chrome.tabs.sendMessage(entry.tabId,{action:'lzxUiRevoked',token:message.token,state:message.state},{frameId:0});}catch{}
  }
}
async function nativeForeground(tabId, message) {
  const token = message.foreground_token;
  const marker = `[AssistantFlowFocus ${token}]`;
  let proof;
  try {
    proof = (await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'MAIN',
      func:(project, marker)=> {
        const expected=new URL(project), current=new URL(location.href);
        const projectPath=url=>url.pathname.replace(/\/$/,'').replace(/\/edit\/[A-Za-z0-9_-]+$/,'');
        if (current.origin!==expected.origin || projectPath(current)!==projectPath(expected)) return null;
        const previous=document.title;
        document.title=marker;
        // Also restore after an interrupted extension worker; never replace a
        // subsequent title written by the page itself.
        setTimeout(()=>{if(document.title===marker)document.title=previous;},2500);
        return {previous};
      },args:[config.project,marker]}))[0];
    if (!proof?.documentId || !proof.result) throw Error('原项目文档未确认');
    const tab = await chrome.tabs.get(tabId);
    return await new Promise(resolve=>{
      const timer=setTimeout(()=>{
        foregroundReplies.delete(token);
        send({type:'foreground_cancel',request_id:message.request_id,token});
        resolve('timeout');
      },2000);
      foregroundReplies.set(token,{request:message.request_id,resolve:state=>{clearTimeout(timer);resolve(state);}});
      send({type:'foreground_request',request_id:message.request_id,token,window_id:tab.windowId,tab_id:tabId});
    });
  } catch (error) {
    console.warn('[AssistantFlow] Windows 窗口激活未完成：',error.message || error);
    send({type:'foreground_request',request_id:message.request_id,token,unavailable:true});
  } finally {
    foregroundReplies.delete(token);
    if (proof?.documentId && proof.result) {
      try {await chrome.scripting.executeScript({target:{tabId,documentIds:[proof.documentId]},world:'MAIN',
        func:(marker, previous)=>{if(document.title===marker)document.title=previous;},args:[marker,proof.result.previous]});} catch {}
    }
  }
}
async function bootstrapProject(expectedTab = null) {
  if (!projectUrl(config.project) || !chrome.scripting?.executeScript) return {supported:false};
  const tabId = await ensureTab();
  if (expectedTab != null && expectedTab !== tabId) return {skipped:true};
  if (pageBootstraps.has(tabId)) return pageBootstraps.get(tabId);
  const epoch = connectionEpoch, project = config.project;
  const work = (async () => {
    const tab = await chrome.tabs.get(tabId);
    if (!sameProjectTab(tab, project)) throw Error('项目地址已变化，未补装其他页面。');
    if (tab.pendingUrl) return {waiting:true};
    let health;
    try {health = await chrome.tabs.sendMessage(tabId,{action:'lzxPageHealth'},{frameId:0});} catch {}
    if (health?.healthy === true && health.reference_ready !== false) return {healthy:true};
    const proof = (await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'MAIN',
      injectImmediately:true,func:()=>({url:location.href,legacy_ui:!!window.__flowAccountBridgeLoaded && !window.__lzxFlowUI})}))[0];
    if (!proof?.documentId || !sameProjectTab({url:proof.result?.url}, project) || epoch !== connectionEpoch) {
      throw Error('项目文档已变化，取消旧页面的脚本恢复。');
    }
    // Pin every operation to the probed document: a navigation between awaits
    // must never install a project controller into another page.
    const target = {tabId,documentIds:[proof.documentId]};
    if (!health || health.native_bridge === false) await chrome.scripting.executeScript({target,world:'ISOLATED',injectImmediately:true,
      files:proof.result.legacy_ui ? ['lzx-content.js'] : ['content-isolated.js','lzx-content.js']});
    if (epoch !== connectionEpoch) throw Error('连接配置已变化，取消脚本恢复。');
    await chrome.scripting.executeScript({target,world:'MAIN',injectImmediately:true,
      files:['lzx-reference.js','lzx-page.js','injects/flow-proxy.js','injects/ui.js']});
    await chrome.scripting.insertCSS({target,files:['injects/ui.css']});
    return {restored:true};
  })();
  pageBootstraps.set(tabId,work);
  try {return await work;} finally {pageBootstraps.delete(tabId);}
}
async function forward(message) {
  const usesPageUi = ['get_points', 'prepare', 'upload_images', 'run_tasks', 'retry_download'].includes(message.action);
  let foregroundAttempted = false;
  const attempts = Math.max(1, Math.ceil(timingMs('bridge_forward_wait_seconds',45) / timingMs('bridge_forward_retry_seconds',1)));
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const tabId = await ensureTab();
      if (attempt === 0 && message.action === 'upload_images' && message.params?.recover_reference === true) {
        await bootstrapProject(tabId);
      }
      if (usesPageUi) {
        await readyWorkTab(tabId, {...message,foreground_skip:foregroundAttempted});
        foregroundAttempted = true;
      }
      if (message.action === 'upload_images') {
        const proof=message.params?.reference;
        if(authenticated && proof?.upload_id===message.params?.upload_id && /^[a-f0-9]{32}$/.test(proof?.upload_id || '') &&
            typeof proof.path==='string' && Array.isArray(message.params?.paths) && message.params.paths.length===1 &&
            uploadPath(message.params.paths[0])===uploadPath(proof.path)) {
          const previous=nativeUploads.get(tabId);
          if(previous?.request!==message.request_id)nativeUploads.set(tabId,{path:uploadPath(proof.path),
            request:message.request_id,project:config.project,epoch:connectionEpoch,at:Date.now()});
        } else nativeUploads.delete(tabId);
        const reference=message.params?.visual_reference;
        if (browserOptions.visual_upload_fallback && reference?.upload_id===message.params?.upload_id &&
            reference.sha256===message.params?.reference?.sha256 && /^[a-f0-9]{32}$/.test(reference.upload_id) &&
            /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(reference.thumbnail || '') && reference.thumbnail.length<350000) {
          const previous=visualUploads.get(tabId);
          if(previous?.request!==message.request_id)visualUploads.set(tabId,{reference,request:message.request_id,project:config.project,
            at:Date.now(),count:0,epoch:connectionEpoch});
        } else visualUploads.delete(tabId);
      }
      const response = await chrome.tabs.sendMessage(tabId, {action: 'lzxCommand', message:{...message,timing:timingValues}}, {frameId: 0});
      if (response?.accepted) return;
    } catch {}
    if (attempt === 0) {try {await bootstrapProject();} catch {}}
    await new Promise(resolve => setTimeout(resolve, timingMs('bridge_forward_retry_seconds',1)));
  }
  throw Object.assign(new Error('主程序打开的 Flow 项目页尚未就绪，请检查登录／项目页面；扩展没有另外创建标签页。'),
    {code: 'FLOW_PAGE_NOT_READY'});
}
async function readyWorkTab(tabId, message = {}) {
  if (browserOptions.ui_mutex && message.action !== 'focus_window') return;
  const tab = await chrome.tabs.get(tabId);
  if (!sameProjectTab(tab, config.project)) throw Error('原 Flow 项目已切换，未操作其他页面。');
  if (message.foreground_skip || message.action!=='focus_window' && browserOptions.focus_before_action!==true) return 'disabled';
  // Upstream drives visible page controls. Match that normal manual path,
  // rather than adding a persistent debugger or overriding page visibility.
  if (!tab.active && typeof chrome.tabs.update === 'function') await chrome.tabs.update(tabId, {active: true});
  if (Number.isInteger(tab.windowId) && tab.windowId >= 0 && typeof chrome.windows?.get === 'function') {
    try {
      const window = await chrome.windows.get(tab.windowId);
      const focus = (message.action === 'focus_window' || browserOptions.focus_before_action === true) && !message.foreground_skip;
      if (window.state === 'minimized') await chrome.windows.update(tab.windowId, {state:'normal', focused:focus});
      else if (focus && window.focused !== true) await chrome.windows.update(tab.windowId, {focused:true});
    } catch (error) {
      // Optional foreground activation must not reject or replay a paid action.
      console.warn('[AssistantFlow] 窗口激活未完成，继续原网页操作：', error.message || error);
    }
  }
  if (!message.foreground_skip && (message.action === 'focus_window' || browserOptions.focus_before_action === true) && /^[a-f0-9]{32}$/.test(message.foreground_token || '')) {
    return await nativeForeground(tabId, message);
  }
}
async function receive(message) {
  if(message.type==='cancel_operation') {
    if(nativeUploads.get(ownedTab)?.request===message.target_request_id)nativeUploads.delete(ownedTab);
    if(Number.isInteger(ownedTab) && /^[a-f0-9]{32}$/.test(message.target_request_id || '')) {
      try {
        const tab=await chrome.tabs.get(ownedTab);
        if(sameProjectTab(tab,config.project))await chrome.tabs.sendMessage(ownedTab,
          {action:'lzxCommand',message:{action:'cancel_operation',target_request_id:message.target_request_id}},{frameId:0});
      }catch{}
    }
    return;
  }
  if (message.type === 'ui_mutex_result') {await uiMutexResult(message);return;}
  if (message.type === 'foreground_result') {
    const pending = foregroundReplies.get(message.token);
    if (pending?.request === message.request_id) {foregroundReplies.delete(message.token);pending.resolve(message.state);}
    return;
  }
  if (message.type === 'welcome') {
    authenticated = true;
    await applyBrowserOptions(message.browser_options || {focus_before_action:false});
    await applyTiming(message.timing);
    await serialize(async () => {
      const saved = await local();
      for (const event of Object.values(saved.lzxOutbox || {})) send(event);
      // Chrome or the worker can stop between completion and journaling its
      // event. Reconcile known downloads without starting a new download.
      for (const [id, binding] of Object.entries(saved.lzxDownloads || {})) {
        if (binding.reported !== 'complete') await reportDownload(Number(id));
      }
    });
    try {await bootstrapProject();} catch (error) {
      console.warn('[AssistantFlow] 页面脚本恢复等待：',error.message);
      if(String(error.message || '').startsWith('等待主程序打开本配置'))send({type:'project_missing',project:config.project});
    }
    return;
  }
  if (message.type === 'timing_settings') {await applyBrowserOptions(message.browser_options); await applyTiming(message.timing); return;}
  if (message.type === 'ack') {
    await serialize(async () => {
      const saved = await local();
      const outbox = saved.lzxOutbox || {};
      const event = outbox[message.event_id];
      const downloads = saved.lzxDownloads || {};
      const binding = event?.type === 'download' && downloads[event.data?.downloadId];
      if (binding && binding.batch === event.batch_id && binding.submissionId === event.data.submissionId) {
        binding.reported = event.data.state;
      }
      delete outbox[message.event_id];
      await chrome.storage.local.set({lzxOutbox: outbox, ...(binding ? {lzxDownloads: downloads} : {})});
    });
    return;
  }
  if (message.type === 'pong') return;
  if (!['get_points', 'prepare', 'upload_images', 'run_tasks', 'snapshot', 'retry_download', 'skip_download', 'open_project', 'refresh_project', 'repair_project', 'close_exhausted', 'close_browser', 'focus_window', 'list_project_videos', 'reuse_project_tab'].includes(message.action)) return;
  await applyBrowserOptions(message.browser_options);
  const tracked = !['snapshot', 'close_exhausted', 'close_browser'].includes(message.action);
  if (tracked) commandsInFlight++;
  let commandLease;
  try {
    if (closingBrowser) throw Object.assign(Error('此浏览器正在核对关闭，暂不接收新操作。'), {code:'FLOW_CLOSE_BUSY'});
    if(browserOptions.ui_mutex && ['focus_window','repair_project','open_project'].includes(message.action)) {
      commandLease=crypto.randomUUID().replaceAll('-','');
      const permit=await acquireUiLease(await ensureTab(),message.action,commandLease);
      if(!permit.ok)throw Error('前台 UI 互斥未取得操作权限：'+permit.state);
    }
    if (message.action === 'focus_window') {
      const state = await readyWorkTab(await ensureTab(),message);
      send({action:'focus_window_response',request_id:message.request_id,success:true,data:{state:state || 'Chrome 已请求聚焦，等待 Windows 前台校验'}});
      return;
    }
    if (message.action === 'close_exhausted' || message.action === 'close_browser') {
      await closeExhausted(message);
      return;
    }
    if (message.action === 'reuse_project_tab') {
      const data=await reuseProjectTab(message);
      send({action:'reuse_project_tab_response',request_id:message.request_id,success:true,data});
      return;
    }
    if (message.action === 'repair_project') {
      const result = await repairProject(message);
      send({action:'repair_project_response',request_id:message.request_id,success:true,data:result});
      return;
    }
    if (message.action === 'refresh_project') {
      throw Error('提交生成后禁止自动刷新项目页；仅保留原任务并只读核对，不重新生成。');
    }
    if (message.action === 'open_project') {
      const url = message.params?.url;
      if (url && url !== config.project) throw new Error('不能用打开项目命令替换此浏览器保存的项目。');
      const tabId = await ensureTab();
      const focus=browserOptions.focus_before_action===true;
      const tab = focus ? await chrome.tabs.update(tabId, {active: true}) || await chrome.tabs.get(tabId) : await chrome.tabs.get(tabId);
      if (focus && Number.isInteger(tab.windowId) && tab.windowId >= 0) {
        try {await chrome.windows.update(tab.windowId, {focused: true});}
        catch (error) {if (!/no current window|no window with id|invalid window id/i.test(String(error.message || error))) throw error;}
      }
      send({action: 'open_project_response', request_id: message.request_id, success: true});
      return;
    }
    if (message.action === 'list_project_videos') {
      const result=await listProjectVideos(message);
      send({action:'list_project_videos_response',request_id:message.request_id,success:true,data:result});
      return;
    }
    if (message.action === 'retry_download' && message.params?.restore) {
      await retryBoundDownload(message);
      return;
    }
    if(message.action==='skip_download' && message.params?.park_native_downloads===true) {
      await parkNativeDownloads(message);
      return;
    }
    const packedOriginal = ['skip_download','retry_download'].includes(message.action) &&
      (await local()).lzxBatches?.[message.params?.batch_id]?.packed === true;
    if (message.action === 'skip_download' && message.params?.batch_id !== activeBatch && !packedOriginal) {
      send({action:'skip_download_response', request_id:message.request_id, success:true, data:{stale:true}});
      return;
    }
    if (message.action === 'retry_download' && message.params?.batch_id !== activeBatch && !packedOriginal) {
      throw new Error('原任务不再是此浏览器的当前批次，保留结果等待原项目恢复。');
    }
    if (message.action === 'run_tasks') {
      const batch = message.params?.batch_id;
      if (!/^[a-f0-9]{32}$/.test(batch || '') || message.params.tasks?.length !== 1) throw new Error('任务批次格式无效');
      const claimed = await serialize(async () => {
        const saved = await local();
        const batches = saved.lzxBatches || {};
        if (batches[batch]) return false;
        batches[batch] = {at: Date.now(), request_id: message.request_id, state: 'sent', packed: message.params.packed === true};
        // Atomically claim before forwarding; concurrent replays must also
        // observe this durable claim, including while storage is awaiting I/O.
        await chrome.storage.local.set({lzxBatch: batch, lzxBatches: batches});
        activeBatch = batch;
        return true;
      });
      if (typeof claimed !== 'boolean') throw new Error('无法保存批次去重记录，未提交生成。');
      if (!claimed) {
        const entry = (await local()).lzxBatches?.[batch];
        send({action: 'run_tasks_response', request_id: message.request_id, success: entry?.state === 'accepted',
          data: {resumed: entry?.state === 'accepted'},
          ...(entry?.state === 'accepted' ? {} : {code: entry?.code || 'FLOW_BATCH_UNCONFIRMED',
            error: entry?.error || '此批次已有发送记录，但尚未确认网页接收；保留原任务核对，不再次生成。'})});
        await forward({action: 'snapshot'});
        return;
      }
    }
    await forward(message);
  } catch (error) {
    send({action: message.action + '_response', request_id: message.request_id, success: false,
      error: String(error.message || error), code: error.code || ''});
  } finally {
    if(commandLease)releaseUiLease(commandLease,false,true);
    if (tracked) commandsInFlight--;
  }
}
async function closeExhausted(message) {
  const args = message.params || {}, epoch = connectionEpoch;
  if (args.released_batches !== undefined && (!Array.isArray(args.released_batches) ||
      args.released_batches.length > 512 || args.released_batches.some(id => typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)))) {
    throw Error('旧任务释放凭据格式无效，保留浏览器。');
  }
  const released = new Set(args.released_batches || []);
  const backup = args.reason === 'high_credit_backup' && typeof args.account === 'string' && !!args.account.trim();
  const low = (!args.reason || args.reason==='auto_low_credit') && Number.isFinite(args.balance) && args.balance >= 0 && args.balance < 10 &&
    Number.isFinite(args.until) && args.until * 1000 > Date.now();
  const validReason = message.action === 'close_browser' ?
    (args.reason === 'manual_runtime' || args.reason === 'auto_page_fault' || args.reason === 'auto_low_credit' && low) : (backup || low);
  if (args.idle !== true || args.profile !== config.profile || args.url !== config.project ||
      !validReason) {
    throw Error('缺少本配置的关闭授权或原项目确认，保留浏览器。');
  }
  const busy = text => Object.assign(Error(text), {code:'FLOW_CLOSE_BUSY'});
  const projectOnly = tabs => {
    const other = tabs.filter(tab => !sameProjectTab(tab, config.project));
    if (other.length) throw busy(`此配置还有 ${other.length} 个非本项目标签页，可能正在使用，已保留浏览器；关闭这些标签页后再试。`);
  };
  if (commandsInFlight || closingBrowser) throw busy('原浏览器仍在处理操作，稍后再核对关闭。');
  closingBrowser = true;
  let closing = false;
  try {
    const tabId = await ensureTab();
    const saved = await local();
    const downloads = Object.values(saved.lzxDownloads || {});
    if (activeBatch && !released.has(activeBatch) && !saved.lzxBatches?.[activeBatch]) {
      throw busy('当前批次的持久记录缺失，保留浏览器。');
    }
    const groups = new Map();
    for (const item of downloads) {
      if (released.has(item.batch)) continue;
      if (!item.batch || !item.taskId || !item.submissionId || !item.mediaId || !item.download_path) {
        throw busy('下载归属记录不完整，保留浏览器。');
      }
      const key = JSON.stringify([item.batch, item.taskId, item.submissionId, item.mediaId, item.download_path]);
      groups.set(key, [...(groups.get(key) || []), item]);
    }
    const finished = items => items.some(item => item.reported === 'complete');
    if (Object.values(saved.lzxOutbox || {}).some(event => event?.type === 'download' && !released.has(event.batch_id))) {
      throw busy('下载完成事件尚未获主程序确认，保留浏览器。');
    }
    for (const [batch, entry] of Object.entries(saved.lzxBatches || {})) {
      if (released.has(batch)) continue; // Host retains the explicit delete/reset receipt; keep this replay guard.
      if (entry?.state === 'rejected') continue;
      const results = [...groups.values()].filter(items => items[0].batch === batch);
      // Releases before 1.1.17 journaled the claim as a timestamp. Such a
      // claim alone proves nothing; exact acknowledged downloads can settle it.
      const legacy = typeof entry === 'number' && Number.isFinite(entry) && entry > 0;
      if ((!legacy && entry?.state !== 'accepted') || !results.length || results.some(items => !finished(items))) {
        throw busy('此配置仍有未确认提交或未完成下载，保留浏览器。');
      }
    }
    // An interrupted download can have a later successful retry, but only
    // completion of the exact same bound media can settle that old record.
    if ([...groups.values()].some(items => !finished(items)) ||
        (await chrome.downloads.search({state:'in_progress'})).length) {
      throw busy('此浏览器仍有下载未完成，保留浏览器。');
    }
    // A background journal alone cannot prove a page is idle. Check every
    // Flow project tab in this profile, including a second manually used tab.
    const tabs = await chrome.tabs.query({});
    projectOnly(tabs);
    if (!tabs.some(tab => tab.id === tabId && sameProjectTab(tab, config.project))) {
      throw busy('原项目已切换，保留浏览器。');
    }
    for (const tab of tabs.filter(tab => projectUrl(tab.pendingUrl || tab.url))) {
      let health;
      try {health = await chrome.tabs.sendMessage(tab.id, {action:'lzxPageHealth'}, {frameId:0});} catch {}
      if ((health?.healthy !== true && args.reason !== 'auto_page_fault') || health?.running !== false || health?.native_busy !== false) {
        throw busy('Flow 页面仍在执行或未确认空闲，保留浏览器。');
      }
    }
    const windows = await chrome.windows.getAll({windowTypes:['normal','popup']});
    if (!windows.length || windows.some(window => !Number.isInteger(window.id) || window.id < 0)) {
      throw Error('无法确认本配置的浏览器窗口，保留浏览器。');
    }
    if ((await chrome.downloads.search({state:'in_progress'})).length || epoch !== connectionEpoch ||
        !sameProjectTab(await chrome.tabs.get(tabId), config.project)) {
      throw busy('下载、项目或配置状态已经变化，稍后重新核对。');
    }
    // Health/download checks yield to Chrome. The user may open or navigate
    // another tab while they run; never close that newly opened personal page.
    const finalTabs = await chrome.tabs.query({});
    projectOnly(finalTabs);
    const signature = tab => JSON.stringify([tab.id, tab.windowId, tab.url, tab.pendingUrl]);
    const checkedTabs = new Set(tabs.map(signature));
    if (finalTabs.length !== tabs.length || finalTabs.some(tab => !checkedTabs.has(signature(tab)))) {
      throw busy('标签页状态已变化，已保留浏览器；稍后重新核对。');
    }
    // chrome.windows is scoped to this Chrome profile. Never terminate a
    // Chrome process, which can also own another account's browser windows.
    send({action:message.action+'_response',request_id:message.request_id,success:true,data:{closing:true}});
    const removals = await Promise.allSettled(windows.map(window => chrome.windows.remove(window.id)));
    if (removals.some(result => result.status === 'rejected')) throw Error('部分浏览器窗口未关闭，请检查此配置后重试。');
    if ((await chrome.windows.getAll({windowTypes:['normal','popup']})).length) throw Error('浏览器仍有窗口未关闭，请检查此配置。');
    send({action:message.action+'_response',request_id:message.request_id,success:true,data:{closed:true}});
    closing = true;
    socket?.close();
  } finally {
    if (!closing) closingBrowser = false;
  }
}
const pageRepairs = new Map();
async function reuseProjectTab(message) {
  const args=message.params || {}, project=config.project, epoch=connectionEpoch;
  if(args.idle!==true || !sameProjectTab({url:args.url},project) ||
      !Number.isFinite(args.expires_at) || args.expires_at*1000<=Date.now() ||
      browserOptions.ui_mutex && args.host_ui_lease!==true)throw Error('缺少原项目空闲／UI权限确认，未改变标签页。');
  const target=new URL(args.url);
  if(target.username || target.password || target.port && target.port!=='443')throw Error('项目恢复地址无效。');
  const tabs=await chrome.tabs.query({});
  let tab=await findProjectTab(project);
  const safe=tab=>{
    if(tab.pendingUrl)return false;
    try {
      const url=new URL(tab.url),origin=new URL(project).origin;
      return ['chrome://newtab/','about:blank'].includes(tab.url) ||
        url.origin===origin && ['/', '/fx/tools/flow','/fx/tools/flow/'].includes(url.pathname) ||
        url.protocol==='chrome-extension:' && url.hostname===chrome.runtime.id && url.pathname==='/options.html';
    }catch{return false;}
  };
  if(!tab)tab=tabs.find(t=>t.id===ownedTab && safe(t)) || tabs.find(t=>t.active && safe(t)) || tabs.find(safe);
  if(!tab)throw Error('未找到可复用的Flow、空白或本扩展设置页，已保留其他标签；未新建项目分页。');
  const tabId=tab.id, previous=tab.url, pending=tab.pendingUrl;
  if(pending) {
    if(!sameProjectTab(tab,project))throw Error('原标签页正在转向其他地址，未修改。');
    return {reused:true,tab_id:tabId,loading:true};
  }
  let health;
  try {health=await chrome.tabs.sendMessage(tabId,{action:'lzxPageHealth'},{frameId:0});}catch{}
  if(health?.running===true || health?.native_busy===true)throw Error('原页面仍在执行，未刷新或新建分页。');
  if(sameProjectTab(tab,project) && health?.healthy!==true) {
    const saved=await local(),entry=saved.lzxBatches?.[activeBatch];
    const downloads=Object.values(saved.lzxDownloads || {}).filter(item=>item.batch===activeBatch);
    if(entry && entry.state!=='rejected' && !(downloads.length && downloads.every(item=>item.reported==='complete')))
      throw Error('原页面仍有未确认的提交，保留原页和任务，不新建分页。');
  }
  const current=await chrome.tabs.get(tabId);
  if(!authenticated || args.expires_at*1000<=Date.now() || epoch!==connectionEpoch || project!==config.project || current.url!==previous || current.pendingUrl ||
      !(sameProjectTab(current,project) || safe(current)))throw Error('原标签页或连接已变化，停止旧恢复请求。');
  // A plain open adopts the current project/detail URL. Only an approved
  // setup ticket or a safe non-project surface needs in-place navigation.
  if(!sameProjectTab(current,project) || target.hash) {
    await chrome.tabs.update(tabId,{url:target.href,...(browserOptions.focus_before_action===true?{active:true}:{})});
  }
  ownedTab=tabId;await chrome.storage.local.set({lzxOwnedTab:tabId});
  return {reused:true,tab_id:tabId};
}

async function repairProject(message) {
  if (message.params?.idle !== true || message.params?.url !== config.project) throw Error('缺少本机软件的空闲确认，未刷新项目。');
  const tabId = await ensureTab();
  if (pageRepairs.has(tabId)) return pageRepairs.get(tabId);
  const work = (async () => {
    let health;
    try {health = await chrome.tabs.sendMessage(tabId,{action:'lzxPageHealth'},{frameId:0});} catch {}
    if (health?.running === true || health?.native_busy === true) throw Object.assign(Error('原网页队列或图片操作仍在执行，保留原页面。'),{code:'FLOW_PAGE_REPAIR_BUSY'});
    if (health?.healthy === true && message.params.force_reload !== true) return {reloaded:false,healthy:true};
    if (chrome.scripting?.executeScript) {
      await bootstrapProject(tabId);
      try {health = await chrome.tabs.sendMessage(tabId,{action:'lzxPageHealth'},{frameId:0});} catch {}
      if (health?.healthy === true && message.params.force_reload !== true) return {reloaded:false,healthy:true,restored:true};
    }
    const saved = await local();
    const entry = saved.lzxBatches?.[activeBatch];
    const downloads = Object.values(saved.lzxDownloads || {}).filter(item=>item.batch===activeBatch);
    if (entry && entry.state !== 'rejected' && !(downloads.length && downloads.every(item=>item.reported==='complete'))) {
      throw Object.assign(Error('网页仍保存未确认的提交记录，保留原任务，不自动刷新。'),{code:'FLOW_PAGE_REPAIR_BUSY'});
    }
    const {lzxPageRepair: previous} = await chrome.storage.local.get(['lzxPageRepair']);
    if (previous?.project === config.project && Date.now()-previous.at < timingMs('page_repair_gap_seconds',30)) {
      throw Error('本项目刚执行过页面恢复，等待加载完成后再检查。');
    }
    const tab = await chrome.tabs.get(tabId);
    if (!sameProjectTab(tab,config.project)) throw Error('项目地址已变化，未刷新其他网页。');
    if (tab.pendingUrl || (await chrome.downloads.search({state:'in_progress'})).length) {
      throw Object.assign(Error('原项目正在加载或浏览器仍有下载，未执行 F5。'),{code:'FLOW_PAGE_REPAIR_BUSY'});
    }
    await chrome.storage.local.set({lzxPageRepair:{project:config.project,at:Date.now(),tabId}});
    await chrome.tabs.reload(tabId);
    return {reloaded:true};
  })();
  pageRepairs.set(tabId,work);
  try {return await work;} finally {pageRepairs.delete(tabId);}
}
// Read only loaded video material; never scroll, hover, select a tab or touch
// the generation owner. Virtualized history is explicitly a partial inventory.
async function listProjectVideos(message) {
  const project=config.project, epoch=connectionEpoch;
  if(!project || !sameProjectTab({url:message.params?.project},project))throw Error('视频列表与原项目不一致。');
  const allTabs=(await chrome.tabs.query({})).filter(tab=>Number.isInteger(tab.id) && !tab.pendingUrl && sameProjectTab(tab,project));
  if(!allTabs.length)throw Error('原项目页面尚未打开，请打开后刷新视频列表。');
  const listTabs=allTabs.filter(tab=>!/\/edit\/[A-Za-z0-9_-]+\/?$/.test(new URL(tab.url).pathname));
  const pool=listTabs.length?listTabs:allTabs;
  // Keep one page's actual Flow order. Combining tabs would interleave old
  // lists, different sort choices and detail pages into a fabricated order.
  const tabs=[pool.find(tab=>tab.active) || pool.find(tab=>tab.id===ownedTab) || pool[0]];
  let timer;
  const read=async tab=>{
    const tabUrl=tab.url;
    try {
      const result=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},world:'MAIN',
        func:async project=>{
          const same=()=>{
            try {const a=new URL(location.href),b=new URL(project),base=p=>p.replace(/\/$/,'').replace(/\/edit\/[A-Za-z0-9_-]+$/,'');
              return a.origin===b.origin && base(a.pathname)===base(b.pathname);}catch{return false;}
          };
          if(!same())return {videos:[]};
          const id=value=>/^[A-Za-z0-9_-]{1,256}$/.test(value || '')?value:'';
          const mediaFrom=value=>{
            try {
              const u=new URL(value,location.href);
              if(u.protocol!=='https:' || u.username || u.password || u.port && u.port!=='443')return '';
              if(u.hostname==='flow-content.google')return id(u.pathname.match(/^\/video\/([A-Za-z0-9_-]+)$/)?.[1]);
              if(u.hostname==='flow.google.com')return id(u.pathname.match(/^\/asb\/([A-Za-z0-9_-]+)(?:=[A-Za-z0-9_,.-]+)?$/)?.[1]);
              if(u.hostname==='labs.google' && u.pathname==='/fx/api/trpc/media.getMediaUrlRedirect')return id((u.searchParams.get('name') || '').split('/').at(-1));
            }catch{}return '';
          };
          const found=new Map(),sources=new Map();
          const nodes=[...document.querySelectorAll('flow-video-tile,[data-media-type="video"],[data-type="video"],video')].slice(0,1200);
          for(const node of nodes) {
            if(node.closest?.('#flow-batch-overlay,flow-image-tile,[data-media-type="image"],[data-type="image"]'))continue;
            const video=node.tagName==='VIDEO'?node:node.querySelector('video');
            const card=node.closest?.('flow-video-tile,[data-media-type="video"],[data-type="video"],flow-grid-tile-container,[data-tile-id]') || node;
            const source=video?.currentSrc || video?.src || video?.querySelector('source')?.src || '';
            const fromSource=mediaFrom(source);
            const fromCard=id(card.getAttribute?.('data-tile-id') || card.getAttribute?.('data-media-id') ||
              video?.getAttribute?.('data-media-id') || card.querySelector?.('[data-media-id]')?.getAttribute('data-media-id'));
            if(fromSource && fromCard && fromSource!==fromCard)continue;
            let media=fromSource || fromCard;
            // An edit route alone might display an image. Require an actual
            // video element before considering this exact detail-page ID.
            if(!media && video)media=id(new URL(location.href).pathname.match(/\/edit\/([A-Za-z0-9_-]+)\/?$/)?.[1]);
            if(!media || found.has(media))continue;
            const group=card.closest?.('.batch-container,.virtual-item-container,[data-prompt]') || card;
            const prompt=group.querySelector?.('.prompt-text,flow-expandable-prompt,[data-prompt]');
            const time=group.querySelector?.('time,[data-created-at]');
            const text=String(prompt?.innerText || prompt?.textContent || group.getAttribute?.('data-prompt') || card.getAttribute?.('aria-label') || '').trim().slice(0,1200);
            const image=card.querySelector?.('img.thumbnail,img');
            found.set(media,{media_id:media,prompt:text,created:String(time?.getAttribute?.('datetime') || time?.innerText || time?.getAttribute?.('data-created-at') || '').slice(0,100),
              duration:Number.isFinite(video?.duration) && video.duration>0?`${Math.round(video.duration*10)/10}秒`:''});
            sources.set(media,{video,image,poster:video?.poster || image?.currentSrc || image?.src || ''});
            if(found.size>=200)break;
          }
          const thumbnail=async source=>{
            try {
              const canvas=document.createElement('canvas');canvas.width=96;canvas.height=108;
              const paint=image=>{
                const width=image.videoWidth || image.naturalWidth || image.width,height=image.videoHeight || image.naturalHeight || image.height;
                if(!width || !height)return '';
                canvas.width=96; // Reset after a possible cross-origin canvas failure.
                const ctx=canvas.getContext('2d'),scale=Math.min(96/width,108/height);ctx.fillStyle='#20242b';ctx.fillRect(0,0,96,108);
                ctx.drawImage(image,(96-width*scale)/2,(108-height*scale)/2,width*scale,height*scale);
                const data=canvas.toDataURL('image/jpeg',0.6);return data.length<14000?data:'';
              };
              try {const result=paint(source.image || source.video);if(result)return result;}catch{}
              if(!source.poster || typeof fetch!=='function' || typeof createImageBitmap!=='function')return '';
              const url=new URL(source.poster,location.href);
              if(url.protocol!=='https:' || url.username || url.password || url.port && url.port!=='443' ||
                !['flow.google.com','flow-content.google','labs.google'].includes(url.hostname) && !url.hostname.endsWith('.googleusercontent.com'))return '';
              const abort=new AbortController();let timeout,bitmap;
              try {
                return await Promise.race([(async()=>{
                  const response=await fetch(url.href,{credentials:'include',signal:abort.signal});
                  if(!response.ok || !/^image\//i.test(response.headers.get('Content-Type') || ''))return '';
                  const blob=await response.blob();if(blob.size>4000000)return '';
                  bitmap=await createImageBitmap(blob);return paint(bitmap);
                })(),new Promise(resolve=>{timeout=setTimeout(()=>resolve(''),700);})]);
              }finally{clearTimeout(timeout);abort.abort();bitmap?.close();}
            }catch{return '';}
          };
          const videos=[...found.values()];
          await Promise.all(videos.slice(0,24).map(async item=>{item.thumbnail=await thumbnail(sources.get(item.media_id));}));
          return same()?{videos}: {videos:[]};
        },args:[project]});
      const current=await chrome.tabs.get(tab.id);
      if(epoch!==connectionEpoch || project!==config.project || current.pendingUrl || current.url!==tabUrl || !sameProjectTab(current,project))return null;
      return result?.[0]?.result;
    }catch{return null;}
  };
  const results=await Promise.race([Promise.all(tabs.slice(0,12).map(read)),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('读取视频列表超时，请待页面显示后刷新。')),8000);})]).finally(()=>clearTimeout(timer));
  if(epoch!==connectionEpoch || config.project!==project)throw Error('原项目或连接已经变化，请重新刷新。');
  if(results.every(value=>!value))throw Error('原项目页面未能读取，请待网页加载完成后刷新。');
  const merged=new Map();let thumbnailBytes=0;
  for(const result of results)for(const item of result?.videos || []){
    const previous=merged.get(item.media_id);
    if(previous){if(!previous.prompt && item.prompt)previous.prompt=item.prompt;if(!previous.thumbnail && item.thumbnail && thumbnailBytes+item.thumbnail.length<450000){previous.thumbnail=item.thumbnail;thumbnailBytes+=item.thumbnail.length;}continue;}
    if(merged.size>=200)break;
    if(thumbnailBytes+(item.thumbnail?.length || 0)>450000)delete item.thumbnail;
    thumbnailBytes+=item.thumbnail?.length || 0;merged.set(item.media_id,item);
  }
  return {project,videos:[...merged.values()],partial:true,
    warning:'按原项目列表页当前顺序读取，不合并其他标签页；尚未加载的历史视频，请先在网页显示后刷新。'};
}

async function parkNativeDownloads(message) {
  const batch=message.params.batch_id;
  if(!/^[a-f0-9]{32}$/.test(batch || ''))throw Error('下载暂存批次无效');
  const saved=await local();let cancelled=0,progressing=0;
  for(const [id,item] of Object.entries(saved.lzxDownloads || {})) {
    if(item.batch!==batch || item.download_path!==`FlowBridge/${batch}/result.mp4` || !item.mediaId || !item.submissionId)continue;
    const native=(await chrome.downloads.search({id:Number(id)}))[0];
    if(native?.state!=='in_progress')continue;
    const filename=String(native.filename || '').replaceAll('\\','/');
    if(!new RegExp(`/FlowBridge/${batch}/result(?: ?\\(\\d+\\))?\\.mp4$`).test(filename))continue;
    const bytes=Number(native.bytesReceived || 0),key=batch+':'+id,previous=downloadParkProgress.get(key);
    if(Number.isFinite(bytes) && bytes>0 && (previous===undefined || bytes>previous)) {
      downloadParkProgress.set(key,bytes);progressing++;continue;
    }
    await chrome.downloads.cancel(Number(id));cancelled++;
    downloadParkProgress.delete(key);
  }
  send({action:'skip_download_response',request_id:message.request_id,success:true,data:{parked:!progressing,cancelled,progressing}});
}
async function retryBoundDownload(message) {
  const batch = message.params.batch_id, restore = message.params.restore;
  if (!/^[a-f0-9]{32}$/.test(batch || '') || restore.download_path !== `FlowBridge/${batch}/result.mp4`) {
    throw new Error('原下载批次或路径绑定无效，未下载、未重新生成。');
  }
  const saved = await local();
  const selected=restore.manual_selection;
  const manual=selected?.schema===1 && selected.confirmed===true && selected.attempt===batch &&
    selected.browser===browserId && selected.media_id===restore.media_id && selected.task_id===restore.task_id &&
    /^manual_[a-f0-9]{32}$/.test(selected.submission_id || '') &&
    sameProjectTab({url:selected.project},config.project) && sameProjectTab({url:restore.project},selected.project);
  if(selected && !manual)throw Error('手动选择凭据与原任务、账号或项目不一致，请刷新列表后重新选择。');
  const allowed=b=>b?.source==='generation_response' && !manual || manual && b?.source==='manual_selection' && b.submissionId===selected.submission_id &&
    b.mediaIds?.length===1 && b.mediaIds[0]===selected.media_id;
  if (!manual && !saved.lzxBatches?.[batch]) throw new Error('此浏览器没有原提交批次记录，请打开原浏览器。');
  if(restore.project && !sameProjectTab({url:restore.project},config.project))throw Error('原视频项目与此配置保存的项目不同，未下载其他项目。');
  const binding = (restore.bindings || []).find(b => b?.schema === 2 && allowed(b) &&
    b.taskId === restore.task_id && b.submissionId && Array.isArray(b.readyIds) && Array.isArray(b.mediaIds) &&
    (restore.refresh_url && restore.media_id ? b.mediaIds.includes(restore.media_id) :
      b.readyIds.some(id => b.mediaIds.includes(id) && b.urls?.[id])));
  const mediaId = restore.media_id && binding?.mediaIds?.includes(restore.media_id) ? restore.media_id :
    binding?.readyIds?.find(id => binding.mediaIds.includes(id) && binding.urls?.[id]);
  let url = binding?.urls?.[mediaId];
  if (!mediaId || !/^[A-Za-z0-9_-]{1,256}$/.test(mediaId) || !restore.task_id ||
      !restore.refresh_url && (!url || new URL(url).protocol !== 'https:')) {
    throw new Error('原媒体的就绪地址尚不可用，请打开保存的原项目核对；未重新生成。');
  }
  for (const [id, binding] of Object.entries(saved.lzxDownloads || {})) {
    if (binding.batch !== batch || binding.taskId !== restore.task_id ||
        !restore.bindings.some(b => allowed(b) && b.taskId === binding.taskId &&
          b.submissionId === binding.submissionId && (b.readyIds?.includes(binding.mediaId) || restore.refresh_url && restore.media_id===binding.mediaId) && b.mediaIds?.includes(binding.mediaId))) continue;
    const item = (await chrome.downloads.search({id: Number(id)}))[0];
    if (item && item.exists !== false && ['complete', 'in_progress'].includes(item.state)) {
      if(await globalThis.__lzxInspectDownloadItem?.(item,binding)) {
        await serialize(() => reportDownload(Number(id)));
        continue;
      }
      await serialize(() => reportDownload(Number(id)));
      send({action: 'retry_download_response', request_id: message.request_id, success: true, data: {downloadId: Number(id)}});
      return;
    }
  }
  const project=config.project, epoch=connectionEpoch;
  let tabId;
  const videoPage=restore.video_page || '';
  const exactPage=tab=>{
    try {
      const expected=new URL(videoPage),current=new URL(tab?.pendingUrl || tab?.url);
      return current.origin===expected.origin && current.pathname.replace(/\/$/,'')===expected.pathname.replace(/\/$/,'');
    }catch {return false;}
  };
  if(videoPage) {
    let parsed;
    try {parsed=new URL(videoPage);}catch {}
    if(!parsed || parsed.protocol!=='https:' || parsed.username || parsed.password || parsed.port && parsed.port!=='443' ||
        !sameProjectTab({url:videoPage},project) || parsed.pathname.replace(/\/$/,'').split('/').at(-1)!==mediaId ||
        !/\/edit\/[A-Za-z0-9_-]+\/?$/.test(parsed.pathname))
      throw Error('视频详情页与原项目或原视频编号不一致，未打开或下载其他视频。');
    const matches=(await chrome.tabs.query({})).filter(tab=>Number.isInteger(tab.id) && exactPage(tab));
    const selected=matches.find(tab=>tab.id===ownedTab) || matches.find(tab=>tab.active) || matches[0];
    if(!selected)throw Object.assign(Error('未在原浏览器找到粘贴链接对应的视频详情页；请保留这条视频页面后重试下载，未重新生成。'),{code:'FLOW_VIDEO_PAGE_NOT_OPEN'});
    tabId=selected.id;
  } else if(manual) {
    const matches=(await chrome.tabs.query({})).filter(tab=>!tab.pendingUrl && sameProjectTab(tab,project));
    const exact=matches.find(tab=>new URL(tab.url).pathname.endsWith('/edit/'+mediaId));
    tabId=(exact || matches.find(tab=>tab.id===ownedTab) || matches[0])?.id;
    if(!Number.isInteger(tabId))throw Error('原项目页面未打开，请打开后重试下载。');
  } else tabId=await ensureTab();
  if(restore.refresh_url)await warmDownloadPage(tabId,batch,mediaId,message);
  const redirect=`https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=${encodeURIComponent(mediaId)}&mediaUrlType=MEDIA_URL_TYPE_FULL_MEDIA`;
  const tryUrl = async address => {
    const tab=await chrome.tabs.get(tabId);
    if (epoch!==connectionEpoch || config.project!==project || !sameProjectTab(tab,project) || videoPage && !exactPage(tab))
      throw Error('原项目或连接已经变化，停止本次下载恢复；未重新生成。');
    return await globalThis.__lzxRetryBoundDownload(address, restore.download_path, {
      taskId: restore.task_id, submissionId: binding.submissionId, mediaId,
      download_path: restore.download_path, tabId, frameId: 0});
  };
  // Only failures before creation or confirmed terminal bad-content items may
  // advance to another URL. Unknown native outcomes never authorize a retry.
  const safeFailure = result => !result?.success &&
    ['FLOW_DOWNLOAD_NOT_VIDEO','FLOW_DOWNLOAD_PROBE_FAILED','FLOW_DOWNLOAD_ACTUAL_NOT_VIDEO',
      'FLOW_DOWNLOAD_URL_UNRESOLVED','FLOW_DOWNLOAD_REJECTED_URL'].includes(result?.code);
  if(restore.refresh_url)url=await freshDownloadUrl(mediaId, batch, false, tabId, videoPage) || redirect;
  let result=await tryUrl(url), stage='原页面地址';
  if (restore.refresh_url && safeFailure(result) && url!==redirect) {
    stage='原视频编号取址'; result=await tryUrl(redirect);
  }
  if (restore.refresh_url && safeFailure(result)) {
    stage='原视频预览后取址';
    const refreshed=await freshDownloadUrl(mediaId,batch,true,tabId,videoPage);
    if(refreshed)result=await tryUrl(refreshed);
  }
  if (!result?.success) throw Object.assign(new Error(stage+'恢复未成功：'+
    (result?.error || '下载启动结果未确认；保留原视频，可粘贴其 /edit/ 页面链接恢复。')), {code:result?.code || ''});
  send({action: 'retry_download_response', request_id: message.request_id, success: true, data: {downloadId: result.downloadId}});
}
async function warmDownloadPage(tabId,batch,mediaId,message) {
  // The authenticated restore binding above authorizes this exact video only.
  // Keep the foreground phase separate from the actual native file transfer.
  const epoch=connectionEpoch,project=config.project,token=crypto.randomUUID().replaceAll('-','');
  let timer;
  try {
    if(browserOptions.ui_mutex) {
      const permit=await Promise.race([acquireUiLease(tabId,'retry_download',token,batch),
        new Promise(resolve=>{timer=setTimeout(()=>resolve({ok:false,state:'expired'}),10000);})]);
      clearTimeout(timer);
      if(!permit.ok)throw Object.assign(Error('下载预览等待UI锁，稍后重试'),{code:'FLOW_UI_MUTEX_UNAVAILABLE'});
    } else await readyWorkTab(tabId,message);
    if(epoch!==connectionEpoch || !sameProjectTab(await chrome.tabs.get(tabId),project))throw Error('原视频页面已变化，停止预览恢复');
    const deadline=Date.now()+3500;
    await Promise.race([chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'MAIN',
      func:async(project,id,deadline)=>{
        const path=url=>url.pathname.replace(/\/$/,'').replace(/\/edit\/[A-Za-z0-9_-]+$/,'');
        const valid=()=>{try{const a=new URL(project),b=new URL(location.href);return Date.now()<deadline && a.origin===b.origin && path(a)===path(b);}catch{return false;}};
        const media=raw=>{try{const u=new URL(raw,location.href);if(u.protocol!=='https:' || u.username || u.password || u.port && u.port!=='443')return '';return u.hostname==='flow-content.google'?u.pathname.match(/^\/(?:image|video)\/([A-Za-z0-9_-]+)$/)?.[1]:u.hostname==='flow.google.com'?u.pathname.match(/^\/asb\/([A-Za-z0-9_-]+)(?:=|$)/)?.[1]:u.hostname==='labs.google' && u.pathname==='/fx/api/trpc/media.getMediaUrlRedirect'?(u.searchParams.get('name') || '').split('/').at(-1):'';}catch{return '';}};
        const source=n=>n?.currentSrc || n?.src || n?.getAttribute?.('src') || '';
        const exact=value=>typeof value==='string' && /^[A-Za-z0-9_-]{1,256}$/.test(value)?value:'';
        const matches=tile=>{
          if(tile.closest?.('flow-image-tile,[data-media-type="image"],[data-type="image"],#flow-batch-overlay'))return false;
          const container=tile.parentElement?.closest?.('flow-grid-tile-container,[data-tile-id],[data-media-id]');
          const cardId=exact(tile.getAttribute?.('data-media-id') || tile.getAttribute?.('data-tile-id') ||
            container?.getAttribute?.('data-media-id') || container?.getAttribute?.('data-tile-id') ||
            tile.querySelector?.('[data-media-id]')?.getAttribute?.('data-media-id'));
          const videos=new Set([...tile.querySelectorAll('video,video source')].map(n=>media(source(n))).filter(Boolean));
          if(videos.size>1 || cardId && videos.size && !videos.has(cardId))return false;
          if(videos.size)return videos.has(id);
          if(cardId)return cardId===id;
          const links=new Set([...tile.querySelectorAll('a[href]')].map(a=>{
            try{const u=new URL(a.href,location.href),p=new URL(project);return u.origin===p.origin && path(u)===path(p)?exact(u.pathname.match(/\/edit\/([A-Za-z0-9_-]+)\/?$/)?.[1]):'';}catch{return '';}
          }).filter(Boolean));
          if(links.size)return links.size===1 && links.has(id);
          return [...tile.querySelectorAll('img')].some(n=>media(source(n))===id);
        };
        let played=null,muted=false;
        try {
          const hovered=new WeakSet();
          while(valid()) {
            const cards=[...document.querySelectorAll('flow-video-tile,[data-media-type="video"],[data-type="video"]')].filter(matches);
            if(cards.length===1) {
              const tile=cards[0];
              // Dispatch DOM events only. No native mouse move, click, CDP
              // pointer movement or scrolling; the user's cursor stays put.
              for(const node of [tile,tile.querySelector('img'),tile.querySelector('video')].filter(Boolean)) {
                if(hovered.has(node) || node.isConnected===false || typeof node.dispatchEvent!=='function')continue;
                const r=node.getBoundingClientRect?.() || tile.getBoundingClientRect();
                if(!(r.width>0 && r.height>0))continue;
                const init={bubbles:true,composed:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2};
                for(const kind of ['pointerover','pointerenter','pointermove'])if(typeof PointerEvent==='function')node.dispatchEvent(new PointerEvent(kind,{...init,pointerType:'mouse',pointerId:1,isPrimary:true}));
                for(const kind of ['mouseover','mouseenter','mousemove'])node.dispatchEvent(new MouseEvent(kind,init));
                hovered.add(node);
              }
              const video=tile.querySelector('video');
              if(video && !played && video.paused!==false && media(source(video))===id) {
                played=video;muted=video.muted;video.muted=true;
                Promise.resolve(video.play()).then(()=>{if(!valid()){video.pause();video.muted=muted;}},()=>{});
              }
            }
            await new Promise(resolve=>setTimeout(resolve,100));
          }
        } finally {if(played){played.pause();played.muted=muted;}}
      },args:[project,mediaId,deadline]}),new Promise(resolve=>{timer=setTimeout(resolve,4500);})]);
  } finally {clearTimeout(timer);releaseUiLease(token,false,true);}
}
async function freshDownloadUrl(mediaId, batchId, forcePreview=false, preferredTab=null, videoPage='') {
  if(!chrome.scripting?.executeScript)return '';
  try {
    const tabId=Number.isInteger(preferredTab)?preferredTab:await ensureTab(),tab=await chrome.tabs.get(tabId);
    if(!sameProjectTab(tab,config.project))return '';
    const result=await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'MAIN',
      func:async (project,id,batch,force,expectedPage)=>{
        const expected=new URL(project),current=new URL(location.href);
        const projectPath=url=>url.pathname.replace(/\/$/,'').replace(/\/edit\/[A-Za-z0-9_-]+$/,'');
        const matchesPage=page=>!expectedPage || page.origin===new URL(expectedPage).origin &&
          page.pathname.replace(/\/$/,'')===new URL(expectedPage).pathname.replace(/\/$/,'');
        if(current.origin!==expected.origin || projectPath(current)!==projectPath(expected) || !matchesPage(current))return '';
        let coldVideo = false;
        const read = () => {
        const signed=new Set(), direct=new Set(), resolvers=new Set();
        for(const node of document.querySelectorAll('video, video source, a[href]')) {
          const raw=node.currentSrc || node.src || node.href || node.getAttribute?.('src');
          if(!raw)continue;
          try {
            const url=new URL(raw,location.href);
            if(url.protocol!=='https:' || url.username || url.password || url.port && url.port!=='443')continue;
            let candidate='';
            if(url.hostname==='flow-content.google') {
              const expiry=Number(url.searchParams.get('Expires'));
              if(!url.searchParams.get('Signature') || !Number.isFinite(expiry) || expiry*1000<=Date.now()+5000)continue;
              candidate=url.pathname.match(/^\/video\/([A-Za-z0-9_-]+)$/)?.[1];
            }
            else if(url.hostname==='flow.google.com')candidate=url.pathname.match(/^\/asb\/([A-Za-z0-9_-]+)(?:=[A-Za-z0-9_,.-]+)?$/)?.[1];
            else if(url.hostname==='labs.google' && url.pathname==='/fx/api/trpc/media.getMediaUrlRedirect') {
              const name=url.searchParams.get('name') || '';candidate=name.split('/').at(-1);
            }
            if(candidate===id) {
              (url.hostname==='flow-content.google'?signed:url.hostname==='labs.google'?resolvers:direct).add(url.href);
              if (node.tagName === 'VIDEO' && node.readyState < 1) coldVideo = true;
            }
          }catch{}
        }
        // A lookup link is not a second video. Do not discard a real playable
        // URL merely because the same media also has a lookup anchor on page.
        if(signed.size)return signed.size===1 ? [...signed][0] : '';
        if(direct.size)return direct.size===1 ? [...direct][0] : '';
        return resolvers.size===1 ? [...resolvers][0] : '';
        };
        let url = read();
        if (!url || coldVideo || force || new URL(url).hostname==='labs.google') {
          await window.__lzxFlowTransport?.wakeResultPreview?.(id, batch, force);
          // Navigation or account changes during the await cannot authorize a
          // different project's URL, even if it happens to expose the same ID.
          const after = new URL(location.href);
          if (after.origin !== expected.origin || projectPath(after) !== projectPath(expected) || !matchesPage(after)) return '';
          url = read();
        }
        // A virtualized/offscreen result may have no DOM node at all. Its
        // verified generation ID still permits a bounded authenticated lookup
        // in this exact page; it never permits a native RPC/HTML download.
        if(!url)url=`https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=${encodeURIComponent(id)}&mediaUrlType=MEDIA_URL_TYPE_FULL_MEDIA`;
        // Resolve an authenticated page URL in its original page context. Chrome
        // download requests do not necessarily share this lookup's auth context.
        // Only the resulting exact-ID signed media URL leaves the page.
        if(url && new URL(url).hostname!=='flow-content.google') {
          if(typeof fetch!=='function' || typeof AbortController!=='function')return '';
          const abort=new AbortController();let timer,response;
          try {
            response=await Promise.race([fetch(url,{credentials:'include',cache:'no-store',redirect:'follow',
              headers:{Range:'bytes=0-31'},signal:abort.signal}),new Promise(resolve=>{timer=setTimeout(()=>resolve(null),3000);})]);
            if(!response || ![200,206].includes(response.status) || !response.url)return '';
            const resolved=new URL(response.url),expiry=Number(resolved.searchParams.get('Expires'));
            if(resolved.protocol!=='https:' || resolved.username || resolved.password || resolved.port && resolved.port!=='443' ||
              resolved.hostname!=='flow-content.google' || resolved.pathname!==`/video/${id}` || !resolved.searchParams.get('Signature') ||
              !Number.isFinite(expiry) || expiry*1000<=Date.now()+5000)return '';
            const after=new URL(location.href);
            if(after.origin!==expected.origin || projectPath(after)!==projectPath(expected) || !matchesPage(after))return '';
            url=resolved.href;
          } catch {return '';}
          finally {clearTimeout(timer);abort.abort();try {response?.body?.cancel?.()?.catch?.(()=>{});}catch{}}
        }
        return url;
      },args:[config.project,mediaId,batchId,forcePreview,videoPage]});
    return result?.[0]?.result || '';
  }catch {return '';}
}
async function connect() {
  if (booting || connecting || socket && [0, 1].includes(socket.readyState) || Date.now() < retryAt) return;
  connecting = true;
  const epoch = connectionEpoch;
  try {
    const saved = await local();
    if (epoch !== connectionEpoch) return;
    await applyTiming(saved.lzxTiming, false);
    await applyBrowserOptions(saved.lzxBrowserOptions, false);
    config = saved.lzxConfig || {};
    ownedTab = saved.lzxOwnedTab ?? null; activeBatch = saved.lzxBatch || '';
    if (!config.token || !Number.isInteger(config.port) || config.port < 1024 || config.port > 65535) return;
    const noWindows = chrome.windows?.getAll && !(await chrome.windows.getAll({windowTypes:['normal','popup']})).length;
    if (closingBrowser && !noWindows) return;
    if (noWindows && (!saved.lzxBrowserId || Date.now() - absenceReportedAt < timingMs('bridge_reconnect_max_seconds',20))) return;
    browserId = saved.lzxBrowserId || crypto.randomUUID();
    if (!saved.lzxBrowserId) await chrome.storage.local.set({lzxBrowserId: browserId});
    if (epoch !== connectionEpoch) return;
    const current = new WebSocket(`ws://127.0.0.1:${config.port}`);
    socket = current; authenticated = false;
    socketStartedAt = lastServerAt = Date.now();
    clearInterval(connectionWatchdog);
    connectionWatchdog = setInterval(() => {
      if (socket !== current) return;
      const expired = !authenticated ? Date.now() - socketStartedAt >= timingMs('bridge_handshake_timeout_seconds',15) :
        Date.now() - lastServerAt >= timingMs('bridge_heartbeat_timeout_seconds',90);
      if (expired) {
        // A hung CONNECTING socket or missing welcome/pong must not prevent
        // every future alarm from reconnecting. This never reloads the page.
        try {current.close();} catch {}
        retireSocket(current);
      }
    }, 1000);
    current.onopen = async () => {
      if (socket !== current || epoch !== connectionEpoch) {current.close(); return;}
      if (noWindows) {
        try {
          if (!(await chrome.windows.getAll({windowTypes:['normal','popup']})).length && epoch === connectionEpoch && socket === current) {
            send({type:'window_absence', protocol:1, token:config.token, browser_id:browserId,
              profile:config.profile, project:config.project});
            absenceReportedAt = Date.now();
          }
        } catch {} finally {current.close();}
        return;
      }
      retryDelay = timingMs('bridge_reconnect_initial_seconds',1);
      send({type: 'hello', protocol: 1, token: config.token, browser_id: browserId,
        profile: config.profile || '', project: config.project || '',
        extension_version: chrome.runtime.getManifest?.().version || '', setup_id: config.setup_id || '',
        capabilities: ['credit_pack_submit_v1', 'idle_reload', 'project_setup_v1', 'generation_media_identity_v2', 'close_exhausted_v1',
          'close_idle_backup_v1', 'close_released_batches_v1', 'window_inventory_v1', 'timing_settings_v1', 'focus_before_action_v1', 'native_foreground_v1', 'site_pause_evidence_v1', 'runtime_controls_v1', 'manual_browser_close_v1', 'protected_browser_close_v1', 'daily_credit_refresh_v1', 'skip_download_v1', 'ui_mutex_v1', 'ui_mutex_renew_v1', 'automatic_browser_close_v1', 'passive_credit_observation_v1', 'video_catalog_v1', 'reuse_project_tab_v1', 'download_backlog_v1']});
      clearInterval(heartbeat);
      heartbeat = setInterval(() => send({type: 'ping'}), timingMs('bridge_heartbeat_seconds',20));
    };
    current.onmessage = event => { if (socket !== current) return; try {
      const message = JSON.parse(event.data);
      if (!message || typeof message !== 'object') return;
      lastServerAt = Date.now();
      receive(message).catch(error => console.error('[AssistantFlow]', error.message));
    } catch {} };
    current.onclose = () => retireSocket(current);
    current.onerror = () => {};
  } finally { connecting = false; if (epoch !== connectionEpoch) setTimeout(connect, 0); }
}
function retireSocket(current) {
  if (socket !== current) return;
  cancelUiLeases('disconnected');
  clearInterval(heartbeat); clearInterval(connectionWatchdog);
  socket = null; authenticated = false; retryAt = Date.now() + retryDelay;
  setTimeout(connect, retryDelay); retryDelay = Math.min(retryDelay * 2, timingMs('bridge_reconnect_max_seconds',20));
}
let serial = Promise.resolve();
function serialize(work) {
  const result = serial.then(work);
  // Keep the queue usable after a failed write, but let its caller know the
  // durable operation failed instead of reporting a successful download/claim.
  serial = result.catch(error => console.error('[AssistantFlow]', error.message));
  return result;
}
async function reportDownload(id) {
  const saved = await local(), binding = (saved.lzxDownloads || {})[id];
  if (!binding) return;
  const item = (await chrome.downloads.search({id}))[0];
  if (!item || !['complete', 'interrupted'].includes(item.state)) return;
  const invalid=await globalThis.__lzxInspectDownloadItem?.(item,binding);
  const state=invalid?'interrupted':item.state;
  const eventId = `${binding.batch}:${id}:${state}`;
  const outbox = saved.lzxOutbox || {};
  if (binding.reported === state || outbox[eventId]) return;
  const event = {type: 'download', event_id: eventId, batch_id: binding.batch, data: {
    filename: item.filename, state, error: invalid?.message || item.error || '', downloadId: id,
    download_path: binding.download_path, taskId: binding.taskId,
    submissionId: binding.submissionId, mediaId: binding.mediaId}};
  outbox[eventId] = event;
  await chrome.storage.local.set({lzxOutbox: outbox});
  send(event);
}
globalThis.__lzxDownloadBound = async (id, task) => {
  const match = String(task.download_path).match(/^FlowBridge\/([a-f0-9]{32})\/result\.mp4$/);
  if (!match) return;
  await serialize(async () => {
    const saved = await local(), downloads = saved.lzxDownloads || {};
    const previous = downloads[id];
    downloads[id] = {batch: match[1], download_path: task.download_path, taskId: task.taskId,
      submissionId: task.submissionId, mediaId: task.mediaId,
      ...(previous?.batch === match[1] && previous?.submissionId === task.submissionId &&
          previous?.mediaId === task.mediaId && previous?.reported ? {reported: previous.reported} : {})};
    await chrome.storage.local.set({lzxDownloads: downloads});
    await reportDownload(id);
  });
};
chrome.downloads.onChanged.addListener(delta => {
  if (['complete', 'interrupted'].includes(delta.state?.current)) serialize(() => reportDownload(delta.id)).catch(() => {});
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if(message.action==='lzxUiMutex') {
    const token=message.token;
    if(sender.id!==chrome.runtime.id || sender.tab?.id!==ownedTab || sender.frameId!==0 ||
        !/^[a-f0-9]{32}$/.test(token || '') || !sameProjectTab(sender.tab,config.project)) {
      reply({ok:false,state:'rejected'});return;
    }
    if(message.release) {releaseUiLease(token,message.passive_wait===true,message.stopped===true);reply({ok:true});return;}
    if(!['get_points','prepare','upload_images','submit','retry_download'].includes(message.operation)) {reply({ok:false,state:'rejected'});return;}
    if(message.operation==='retry_download' && !/^[a-f0-9]{32}$/.test(message.batch_id || '')) {reply({ok:false,state:'rejected'});return;}
    if(message.operation==='submit' && message.batch_id!==activeBatch) {reply({ok:false,state:'rejected'});return;}
    acquireUiLease(sender.tab.id,message.operation,token,message.batch_id).then(reply).catch(error=>reply({ok:false,state:'error',error:String(error.message || error)}));
    return true;
  }
  if (message.action === 'lzxVisualUploadMatch') {
    const tabId=sender.tab?.id,entry=visualUploads.get(tabId);
    if (!authenticated || !browserOptions.visual_upload_fallback || sender.id!==chrome.runtime.id || sender.frameId!==0 ||
        tabId!==ownedTab || !entry || entry.epoch!==connectionEpoch || entry.reference.upload_id!==message.upload_id ||
        entry.count>=3 || Date.now()-entry.at>timingMs('page_upload_max_seconds',180)+2*timingMs('page_controls_wait_seconds',45)+30000) {
      reply({ok:false});return;
    }
    entry.count++;
    (async()=>{
      const tab=await chrome.tabs.get(tabId);
      if(!sameProjectTab(tab,entry.project))return {ok:false};
      const candidates=Array.isArray(message.candidates)?message.candidates:[],before=message.before_ids;
      if(!message.viewport || !['width','height'].every(key=>Number.isFinite(message.viewport[key]) && message.viewport[key]>=64 && message.viewport[key]<=10000) ||
          !Array.isArray(before) || before.length>2000 || before.some(id=>typeof id!=='string') || candidates.length<1 || candidates.length>40 ||
          candidates.some(item=>item.kind!=='image' || !/^[A-Za-z0-9_-]{1,256}$/.test(item.media_id || '') ||
            ['x','y','width','height'].some(key=>!Number.isFinite(item[key])) || item.x<0 || item.y<0 ||
            item.width<48 || item.height<48 || item.x+item.width>message.viewport?.width || item.y+item.height>message.viewport?.height))return {ok:false};
      const capture=await globalThis.__lzxCaptureUpload(tabId);
      if(!sameProjectTab(await chrome.tabs.get(tabId),entry.project) || visualUploads.get(tabId)!==entry || !browserOptions.visual_upload_fallback)return {ok:false};
      if(Math.abs(capture.width-message.viewport.width)>2 || Math.abs(capture.height-message.viewport.height)>2)return {ok:false};
      const matched=await globalThis.__lzxVisualUpload.match(entry.reference,candidates,before,capture);
      return {ok:!!matched,match:matched};
    })().then(reply).catch(()=>reply({ok:false}));
    return true;
  }
  const ownPage = sender.id === chrome.runtime.id && sender.url?.startsWith(`chrome-extension://${chrome.runtime.id}/`);
  if (message.action === 'lzxReconnect' && ownPage) {
    retryAt = 0;
    connect().then(() => reply({accepted: true})).catch(error => reply({error: error.message}));
    return true;
  }
  if (message.action === 'lzxSetupFromProject' && sender.id === chrome.runtime.id && sender.frameId === 0 && sender.tab) {
    claimProjectSetup(sender).then(reply).catch(error => reply({success: false, error: error.message}));
    return true;
  }
  if (message.action === 'lzxConnectionStatus' && sender.id === chrome.runtime.id &&
      sender.url?.startsWith(`chrome-extension://${chrome.runtime.id}/`)) {
    reply({configured: !!config.token, connected: authenticated && socket?.readyState === WebSocket.OPEN,
      profile: config.profile || '', project: config.project || '', setupId: config.setup_id || '', hasProject: projectUrl(config.project),
      version: chrome.runtime.getManifest?.().version || ''});
    return;
  }
  if (message.action !== 'lzxPageEvent' || sender.tab?.id !== ownedTab || sender.frameId !== 0) return;
  const event = message.data;
  if (!event || typeof event !== 'object') return;
  if(event.action==='upload_images_response') {
    visualUploads.delete(sender.tab.id);
    if(nativeUploads.get(sender.tab.id)?.request===event.request_id)nativeUploads.delete(sender.tab.id);
  }
  if (event.action === 'run_tasks_response') {
    serialize(async () => {
      const saved = await local(), batches = saved.lzxBatches || {};
      const entry = Object.values(batches).find(value => value?.request_id === event.request_id);
      if (entry && (entry.state !== 'accepted' || event.success)) {
        Object.assign(entry, {state: event.success ? 'accepted' : 'rejected',
          error: String(event.error || '').slice(0, 1000), code: String(event.code || '')});
        await chrome.storage.local.set({lzxBatches: batches});
      }
      send(event);
    }).catch(() => send({action: event.action, request_id: event.request_id, success: false,
      code: 'FLOW_BATCH_UNCONFIRMED', error: '网页批次回执保存失败，保留原任务核对，不再次生成。'}));
    return;
  }
  if (event.type === 'snapshot') {
    // A freshly claimed command may still be refused by a draining page.
    // Snapshot ownership belongs to actual page tasks, never that new claim.
    const batches = new Set((event.data?.tasks || []).map(task =>
      String(task.download_path || '').match(/^FlowBridge\/([a-f0-9]{32})\/result\.mp4$/)?.[1]).filter(Boolean));
    event.data = {...event.data, batch_id: batches.size === 1 ? [...batches][0] : ''};
  }
  if (['generation_response', 'binding_missing', 'generation_failed'].includes(event.type)) {
    // Late receipts belong to their saved submission, not a newer active batch.
    const batch = String(event.download_path || '').match(/^FlowBridge\/([a-f0-9]{32})\/result\.mp4$/)?.[1];
    if (!batch) return;
    event.batch_id = batch;
  }
  if (event.type === 'generation_failed') {
    const eventId = `generation-failed:${event.batch_id}:${event.submissionId}:${event.mediaId}`;
    serialize(async () => {
      const saved = await local(), outbox = saved.lzxOutbox || {};
      if (!outbox[eventId]) outbox[eventId] = {...event,event_id:eventId};
      await chrome.storage.local.set({lzxOutbox:outbox});
      send(outbox[eventId]);
    }).catch(error=>console.error('[AssistantFlow] 生成失败通知保存失败：',error.message));
    return;
  }
  send(event);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.lzxConfig) {
    if (configSignature(changes.lzxConfig.oldValue) === configSignature(changes.lzxConfig.newValue) ||
        changes.lzxConfig.newValue && configSignature(changes.lzxConfig.newValue) === configSignature(config)) return;
    connectionEpoch++; authenticated = false; retryAt = 0; clearInterval(heartbeat); clearInterval(connectionWatchdog);
    const previous = socket; socket = null; previous?.close(); setTimeout(connect, timingMs('bridge_config_reload_seconds',.05));
  }
});
chrome.runtime.onStartup.addListener(connect);
chrome.windows?.onRemoved?.addListener(async () => {
  try {
    if ((await chrome.windows.getAll({windowTypes:['normal','popup']})).length || !authenticated) return;
    // A socket drop alone proves nothing. This report follows a fresh native
    // inventory of this exact profile, including every popup and normal window.
    send({type:'windows_closed', profile:config.profile, project:config.project});
    closingBrowser = true;
    socket?.close();
  } catch {} // Keep the host reservation when absence cannot be proved.
});
chrome.runtime.onInstalled.addListener(() => { reconnectAlarm(); connect(); });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'lzxReconnect') connect(); });
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
resumeReloadSetup().catch(error => console.error('[AssistantFlow]', error.message)).finally(() => {booting = false; connect();});

async function resumeReloadSetup() {
  if (resumingSetup) return resumingSetup;
  resumingSetup = restoreSetupTab();
  try {return await resumingSetup;} finally {resumingSetup = null;}
}
async function restoreSetupTab() {
  const {lzxReloadSetup: entry} = await chrome.storage.local.get(['lzxReloadSetup']);
  if (!entry || entry.version !== chrome.runtime.getManifest?.().version) return;
  if (!projectUrl(entry.project)) return;
  const url = entry.mode === 'reload_only' ? entry.project : `${entry.project}#lzx_setup=${entry.ticket}&lzx_port=${entry.port}${entry.action === 'options' ? '&lzx_action=options' : ''}`;
  let tab;
  if (entry.tabId != null) {
    try {const original = await chrome.tabs.get(entry.tabId); if (sameProjectTab(original, entry.project)) tab = original;} catch {}
  }
  tab ||= await findProjectTab(entry.project);
  if (!tab) return; // Preserve the handoff until the desktop opens the project.
  try {if (!sameProjectTab(await chrome.tabs.get(tab.id), entry.project)) return;} catch {return;}
  await chrome.storage.local.remove('lzxReloadSetup');
  if(entry.mode!=='reload_only')await chrome.tabs.update(tab.id, {url, active: browserOptions.focus_before_action===true});
  await chrome.tabs.reload(tab.id);
}
chrome.tabs?.onCreated?.addListener(() => resumeReloadSetup().catch(() => {}));
chrome.tabs?.onUpdated?.addListener((_id, change) => {
  if (closingBrowser && change.url && sameProjectTab({url:change.url}, config.project)) {
    closingBrowser = false; retryAt = 0; connect().catch(() => {});
  }
  if (authenticated && (change.status === 'complete' || change.url)) bootstrapProject(_id).catch(() => {});
  if (_id === ownedTab && change.url && authenticated && projectUrl(config.project)) {
    try {
      if (new URL(change.url).hostname === 'accounts.google.com') send({type: 'account_issue',
        url: config.project, code: 'FLOW_ACCOUNT_SIGNED_OUT', error: 'Flow 项目页已转到 Google 登录页面，请重新登录后手动重置账号状态。'});
    } catch {}
  }
  if (change.url) resumeReloadSetup().catch(() => {});
});

async function claimProjectSetup(sender) {
  const entry = new URL(sender.url || sender.tab.url);
  if (!projectUrl(entry.href) || entry.username || entry.password || entry.port && entry.port !== '443') throw Error('连接入口不是有效的 Flow 项目。');
  const args = new URLSearchParams(entry.hash.slice(1)), ticket = args.get('lzx_setup'), port = Number(args.get('lzx_port'));
  if (!/^[a-f0-9]{32}$/.test(ticket || '') || !Number.isInteger(port) || port < 1024 || port > 65535) throw Error('本次自动连接入口无效。');
  entry.hash = ''; const project = entry.href.replace(/\/$/, '');
  const action = args.get('lzx_action') || '';
  if (action && !['options', 'update'].includes(action)) throw Error('未知的连接操作。');
  if (action) {
    const result = await projectSetupAction({project, ticket, port, action}, sender);
    if (!result.needsPairing) return result;
  }
  if (setupClaims.has(ticket)) return setupClaims.get(ticket);
  const work = new Promise((resolve, reject) => {
    const claim = new WebSocket(`ws://127.0.0.1:${port}`);
    let finished = false, receiving = false;
    const finish = (error, result) => {if (finished) return; finished = true; clearTimeout(timer); claim.close(); error ? reject(error) : resolve(result);};
    const timer = setTimeout(() => finish(Error('自动连接超时，请保持主程序打开后再点击连接。')), timingMs('setup_claim_timeout_seconds',10));
    claim.onopen = () => claim.send(JSON.stringify({type: 'setup_claim', ticket}));
    claim.onerror = () => finish(Error('本机连接服务不可用，请保持辅助工具打开。'));
    claim.onclose = () => {if (!finished && !receiving) finish(Error('主程序尚未完成自动连接，请正常重启辅助工具后重试。'));};
    claim.onmessage = async event => {
      if (finished || receiving) return;
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'setup_error') throw Error(message.error);
        if (message.type !== 'setup_bundle') return;
        const pair = message.pairing;
        if (!pair || pair.setup_id !== ticket || pair.port !== port || pair.extension_id !== chrome.runtime.id ||
            typeof pair.token !== 'string' || pair.token.length < 32 || !pair.profile ||
            new URL(pair.project).href.replace(/\/$/, '') !== project) throw Error('项目与本次配对请求不匹配，未更改配置。');
        receiving = true; clearTimeout(timer);
        // No token is returned to the Flow page or its content script.
        await chrome.storage.local.set({lzxConfig: {token: pair.token, port, profile: pair.profile, project,
          setup_id: ticket}, lzxOwnedTab: sender.tab.id});
        finish(null, {success: true, profile: pair.profile, project});
      } catch (error) {finish(error);}
    };
  });
  setupClaims.set(ticket, work);
  try {return await work;} finally {setupClaims.delete(ticket);}
}

async function projectSetupAction(entry, sender) {
  const {project, ticket, port, action} = entry;
  if (setupClaims.has(ticket)) return setupClaims.get(ticket);
  const work = new Promise((resolve, reject) => {
    const claim = new WebSocket(`ws://127.0.0.1:${port}`);
    let finished = false, receiving = false;
    const finish = (error, value) => {if (finished) return; finished = true; clearTimeout(timer); claim.close(); error ? reject(error) : resolve(value);};
    const timer = setTimeout(() => finish(Error('本机连接确认超时，请保持辅助工具打开。')), timingMs('setup_claim_timeout_seconds',10));
    claim.onopen = () => claim.send(JSON.stringify({type: 'setup_reload_check', ticket}));
    claim.onerror = () => finish(Error('无法连接本机软件。'));
    claim.onclose = () => {if (!finished && !receiving) finish(Error('本次连接尚未确认。'));};
    claim.onmessage = async event => {
      if (finished || receiving) return;
      try {
        const reply = JSON.parse(event.data);
        if (reply.type === 'setup_reload_denied' || reply.type === 'setup_error') throw Error(reply.error || '本次操作未获主程序确认。');
        if (reply.type !== 'setup_reload_allowed') return;
        if (reply.ticket !== ticket || !reply.profile || new URL(reply.project).href.replace(/\/$/, '') !== project) throw Error('项目与本次连接请求不匹配。');
        receiving = true; clearTimeout(timer);
        if (chrome.runtime.getManifest().version !== '1.1.60') {
          await chrome.storage.local.set({lzxReloadSetup: {...entry, tabId: sender.tab.id,
            mode: reply.mode || 'pair', profile: reply.profile, version: '1.1.60'}});
          finish(null, {success: true, updating: true}); chrome.runtime.reload(); return;
        }
        if (action === 'options') {
          const url = `chrome-extension://${chrome.runtime.id}/options.html#setup=${ticket}&port=${port}`;
          const current = await chrome.tabs.get(sender.tab.id);
          const saved = (await chrome.tabs.query({})).find(tab => tab.url?.split('#')[0] === url.split('#')[0]);
          if (!saved && (!Number.isInteger(current.windowId) || current.windowId < 0)) {
            throw Error('浏览器窗口尚未就绪，等待主程序打开此配置；不会在不明窗口创建标签页。');
          }
          const focus=browserOptions.focus_before_action===true;
          const tab = saved ? await chrome.tabs.update(saved.id, {url, active: focus}) :
            await chrome.tabs.create({url, active: focus, windowId: current.windowId});
          if (focus && Number.isInteger(tab.windowId) && tab.windowId >= 0) {
            try {await chrome.windows.update(tab.windowId, {focused: true});}
            catch (error) {if (!/no current window|no window with id|invalid window id/i.test(String(error.message || error))) throw error;}
          }
        }
        finish(null, {success: true, profile: reply.profile, project,
          needsPairing: action === 'update' && reply.mode !== 'reload_only'});
      } catch (error) {finish(error);}
    };
  });
  setupClaims.set(ticket, work);
  try {return await work;} finally {setupClaims.delete(ticket);}
}
