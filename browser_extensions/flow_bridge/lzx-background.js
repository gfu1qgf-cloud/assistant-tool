import './background.js';

let socket, heartbeat, connecting = false, retryAt = 0, retryDelay = 1000, connectionEpoch = 0, authenticated = false, booting = true;
let ownedTab = null, activeBatch = '', config = {}, browserId = '', opening, resumingSetup;
let closingBrowser = false, commandsInFlight = 0;
let absenceReportedAt = 0;
let connectionWatchdog, socketStartedAt = 0, lastServerAt = 0;
let timingValues = {};
let browserOptions = {focus_before_action:false};
async function applyBrowserOptions(raw, persist = true) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || typeof raw.focus_before_action !== 'boolean') return;
  const next = {focus_before_action:raw.focus_before_action};
  if (next.focus_before_action === browserOptions.focus_before_action) return;
  browserOptions = next;
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
  return projectUrl(current) && new URL(current).hostname === new URL(url).hostname &&
    new URL(current).pathname.replace(/\/$/, '') === new URL(url).pathname.replace(/\/$/, '');
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
async function nativeForeground(tabId, message) {
  const token = message.foreground_token;
  const marker = `[AssistantFlowFocus ${token}]`;
  let proof;
  try {
    proof = (await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'MAIN',
      func:(project, marker)=> {
        const expected=new URL(project), current=new URL(location.href);
        if (current.origin!==expected.origin || current.pathname.replace(/\/$/,'')!==expected.pathname.replace(/\/$/,'')) return null;
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
  const tab = await chrome.tabs.get(tabId);
  if (!sameProjectTab(tab, config.project)) throw Error('原 Flow 项目已切换，未操作其他页面。');
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
    try {await bootstrapProject();} catch (error) {console.warn('[AssistantFlow] 页面脚本恢复等待：',error.message);}
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
  if (!['get_points', 'prepare', 'upload_images', 'run_tasks', 'snapshot', 'retry_download', 'skip_download', 'open_project', 'refresh_project', 'repair_project', 'close_exhausted', 'close_browser', 'focus_window'].includes(message.action)) return;
  await applyBrowserOptions(message.browser_options);
  const tracked = !['snapshot', 'close_exhausted', 'close_browser'].includes(message.action);
  if (tracked) commandsInFlight++;
  try {
    if (closingBrowser) throw Object.assign(Error('此浏览器正在核对关闭，暂不接收新操作。'), {code:'FLOW_CLOSE_BUSY'});
    if (message.action === 'focus_window') {
      const state = await readyWorkTab(await ensureTab(),message);
      send({action:'focus_window_response',request_id:message.request_id,success:true,data:{state:state || 'Chrome 已请求聚焦，等待 Windows 前台校验'}});
      return;
    }
    if (message.action === 'close_exhausted' || message.action === 'close_browser') {
      await closeExhausted(message);
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
      const tab = await chrome.tabs.update(tabId, {active: true}) || await chrome.tabs.get(tabId);
      if (Number.isInteger(tab.windowId) && tab.windowId >= 0) {
        try {await chrome.windows.update(tab.windowId, {focused: true});}
        catch (error) {if (!/no current window|no window with id|invalid window id/i.test(String(error.message || error))) throw error;}
      }
      send({action: 'open_project_response', request_id: message.request_id, success: true});
      return;
    }
    if (message.action === 'retry_download' && message.params?.restore) {
      await retryBoundDownload(message);
      return;
    }
    if (message.action === 'skip_download' && message.params?.batch_id !== activeBatch) {
      send({action:'skip_download_response', request_id:message.request_id, success:true, data:{stale:true}});
      return;
    }
    if (message.action === 'retry_download' && message.params?.batch_id !== activeBatch) {
      throw new Error('原任务不再是此浏览器的当前批次，保留结果等待原项目恢复。');
    }
    if (message.action === 'run_tasks') {
      const batch = message.params?.batch_id;
      if (!/^[a-f0-9]{32}$/.test(batch || '') || message.params.tasks?.length !== 1) throw new Error('任务批次格式无效');
      const claimed = await serialize(async () => {
        const saved = await local();
        const batches = saved.lzxBatches || {};
        if (batches[batch]) return false;
        batches[batch] = {at: Date.now(), request_id: message.request_id, state: 'sent'};
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
  const low = !args.reason && Number.isFinite(args.balance) && args.balance >= 0 && args.balance < 10 &&
    Number.isFinite(args.until) && args.until * 1000 > Date.now();
  const validReason = message.action === 'close_browser' ? args.reason === 'manual_runtime' : (backup || low);
  if (args.idle !== true || args.profile !== config.profile || args.url !== config.project ||
      !validReason) {
    throw Error('缺少本配置的关闭授权或原项目确认，保留浏览器。');
  }
  const busy = text => Object.assign(Error(text), {code:'FLOW_CLOSE_BUSY'});
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
    if (!tabs.some(tab => tab.id === tabId && sameProjectTab(tab, config.project))) {
      throw busy('原项目已切换，保留浏览器。');
    }
    for (const tab of tabs.filter(tab => projectUrl(tab.pendingUrl || tab.url))) {
      let health;
      try {health = await chrome.tabs.sendMessage(tab.id, {action:'lzxPageHealth'}, {frameId:0});} catch {}
      if (health?.healthy !== true || health?.running !== false || health?.native_busy !== false) {
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
async function retryBoundDownload(message) {
  const batch = message.params.batch_id, restore = message.params.restore;
  if (!/^[a-f0-9]{32}$/.test(batch || '') || restore.download_path !== `FlowBridge/${batch}/result.mp4`) {
    throw new Error('原下载批次或路径绑定无效，未下载、未重新生成。');
  }
  const saved = await local();
  if (!saved.lzxBatches?.[batch]) throw new Error('此浏览器没有原提交批次记录，请打开原浏览器。');
  const binding = (restore.bindings || []).find(b => b?.schema === 2 && b.source === 'generation_response' &&
    b.taskId === restore.task_id && b.submissionId && Array.isArray(b.readyIds) && Array.isArray(b.mediaIds) &&
    b.readyIds.some(id => b.mediaIds.includes(id) && b.urls?.[id]));
  const mediaId = binding?.readyIds?.find(id => binding.mediaIds.includes(id) && binding.urls?.[id]);
  const url = binding?.urls?.[mediaId];
  if (!mediaId || !restore.task_id || !url || new URL(url).protocol !== 'https:') {
    throw new Error('原媒体的就绪地址尚不可用，请打开保存的原项目核对；未重新生成。');
  }
  for (const [id, binding] of Object.entries(saved.lzxDownloads || {})) {
    if (binding.batch !== batch || binding.taskId !== restore.task_id ||
        !restore.bindings.some(b => b.source === 'generation_response' && b.taskId === binding.taskId &&
          b.submissionId === binding.submissionId && b.readyIds?.includes(binding.mediaId) && b.mediaIds?.includes(binding.mediaId))) continue;
    const item = (await chrome.downloads.search({id: Number(id)}))[0];
    if (item && item.exists !== false && ['complete', 'in_progress'].includes(item.state)) {
      await serialize(() => reportDownload(Number(id)));
      send({action: 'retry_download_response', request_id: message.request_id, success: true, data: {downloadId: Number(id)}});
      return;
    }
  }
  const result = await globalThis.__lzxRetryBoundDownload(url, restore.download_path, {
    taskId: restore.task_id, submissionId: binding.submissionId, mediaId,
    download_path: restore.download_path, tabId: ownedTab, frameId: 0});
  if (!result?.success) throw new Error(result?.error || '原下载链接可能已过期，请打开原项目核对。');
  send({action: 'retry_download_response', request_id: message.request_id, success: true, data: {downloadId: result.downloadId}});
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
        capabilities: ['idle_reload', 'project_setup_v1', 'generation_media_identity_v2', 'close_exhausted_v1',
          'close_idle_backup_v1', 'close_released_batches_v1', 'window_inventory_v1', 'timing_settings_v1', 'focus_before_action_v1', 'native_foreground_v1', 'site_pause_evidence_v1', 'runtime_controls_v1', 'manual_browser_close_v1', 'daily_credit_refresh_v1', 'skip_download_v1']});
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
  const eventId = `${binding.batch}:${id}:${item.state}`;
  const outbox = saved.lzxOutbox || {};
  if (binding.reported === item.state || outbox[eventId]) return;
  const event = {type: 'download', event_id: eventId, batch_id: binding.batch, data: {
    filename: item.filename, state: item.state, error: item.error || '', downloadId: id,
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
  await chrome.tabs.update(tab.id, {url, active: true});
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
        if (chrome.runtime.getManifest().version !== '1.1.36') {
          await chrome.storage.local.set({lzxReloadSetup: {...entry, tabId: sender.tab.id,
            mode: reply.mode || 'pair', profile: reply.profile, version: '1.1.36'}});
          finish(null, {success: true, updating: true}); chrome.runtime.reload(); return;
        }
        if (action === 'options') {
          const url = `chrome-extension://${chrome.runtime.id}/options.html#setup=${ticket}&port=${port}`;
          const current = await chrome.tabs.get(sender.tab.id);
          const saved = (await chrome.tabs.query({})).find(tab => tab.url?.split('#')[0] === url.split('#')[0]);
          if (!saved && (!Number.isInteger(current.windowId) || current.windowId < 0)) {
            throw Error('浏览器窗口尚未就绪，等待主程序打开此配置；不会在不明窗口创建标签页。');
          }
          const tab = saved ? await chrome.tabs.update(saved.id, {url, active: true}) :
            await chrome.tabs.create({url, active: true, windowId: current.windowId});
          if (Number.isInteger(tab.windowId) && tab.windowId >= 0) {
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
