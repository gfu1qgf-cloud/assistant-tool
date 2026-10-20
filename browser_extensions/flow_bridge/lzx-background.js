import './background.js';

let socket, heartbeat, connecting = false, retryAt = 0, retryDelay = 1000, connectionEpoch = 0, authenticated = false, booting = true;
let ownedTab = null, activeBatch = '', config = {}, browserId = '', opening, resumingSetup;
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
async function local() { return chrome.storage.local.get(['lzxConfig', 'lzxBrowserId', 'lzxOwnedTab', 'lzxBatch', 'lzxOutbox', 'lzxDownloads', 'lzxBatches']); }
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
async function forward(message) {
  const usesPageUi = ['get_points', 'prepare', 'upload_images', 'run_tasks'].includes(message.action);
  for (let attempt = 0; attempt < 45; attempt++) {
    try {
      const tabId = await ensureTab();
      if (usesPageUi) await readyWorkTab(tabId);
      const response = await chrome.tabs.sendMessage(tabId, {action: 'lzxCommand', message}, {frameId: 0});
      if (response?.accepted) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('主程序打开的 Flow 项目页尚未就绪，请检查登录／项目页面；扩展没有另外创建标签页。');
}
async function readyWorkTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!sameProjectTab(tab, config.project)) throw Error('原 Flow 项目已切换，未操作其他页面。');
  // Upstream drives visible page controls. Match that normal manual path,
  // rather than adding a persistent debugger or overriding page visibility.
  if (!tab.active && typeof chrome.tabs.update === 'function') await chrome.tabs.update(tabId, {active: true});
  if (Number.isInteger(tab.windowId) && typeof chrome.windows?.get === 'function') {
    const window = await chrome.windows.get(tab.windowId);
    if (window.state === 'minimized') await chrome.windows.update(tab.windowId, {state: 'normal', focused: false});
  }
}
async function receive(message) {
  if (message.type === 'welcome') {
    authenticated = true;
    const saved = await local();
    for (const event of Object.values(saved.lzxOutbox || {})) send(event);
    return;
  }
  if (message.type === 'ack') {
    await serialize(async () => {
      const saved = await local();
      const outbox = saved.lzxOutbox || {};
      delete outbox[message.event_id];
      await chrome.storage.local.set({lzxOutbox: outbox});
    });
    return;
  }
  if (message.type === 'pong') return;
  if (!['get_points', 'prepare', 'upload_images', 'run_tasks', 'snapshot', 'retry_download', 'open_project', 'refresh_project'].includes(message.action)) return;
  try {
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
        batches[batch] = Date.now();
        // Atomically claim before forwarding; concurrent replays must also
        // observe this durable claim, including while storage is awaiting I/O.
        await chrome.storage.local.set({lzxBatch: batch, lzxBatches: batches});
        activeBatch = batch;
        return true;
      });
      if (typeof claimed !== 'boolean') throw new Error('无法保存批次去重记录，未提交生成。');
      if (!claimed) {
        send({action: 'run_tasks_response', request_id: message.request_id, success: true, data: {resumed: true}});
        await forward({action: 'snapshot'});
        return;
      }
    }
    await forward(message);
  } catch (error) {
    send({action: message.action + '_response', request_id: message.request_id, success: false, error: String(error.message || error)});
  }
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
    config = saved.lzxConfig || {}; browserId = saved.lzxBrowserId || crypto.randomUUID();
    ownedTab = saved.lzxOwnedTab ?? null; activeBatch = saved.lzxBatch || '';
    await chrome.storage.local.set({lzxBrowserId: browserId});
    if (epoch !== connectionEpoch) return;
    if (!config.token || !Number.isInteger(config.port) || config.port < 1024 || config.port > 65535) return;
    const current = new WebSocket(`ws://127.0.0.1:${config.port}`);
    socket = current; authenticated = false;
    current.onopen = () => {
      if (socket !== current || epoch !== connectionEpoch) {current.close(); return;}
      retryDelay = 1000;
      send({type: 'hello', protocol: 1, token: config.token, browser_id: browserId,
        profile: config.profile || '', project: config.project || '',
        extension_version: chrome.runtime.getManifest?.().version || '', setup_id: config.setup_id || '',
        capabilities: ['idle_reload', 'project_setup_v1', 'generation_media_identity_v2']});
      clearInterval(heartbeat);
      heartbeat = setInterval(() => send({type: 'ping'}), 20000);
    };
    current.onmessage = event => { if (socket !== current) return; try { receive(JSON.parse(event.data)).catch(error => console.error('[AssistantFlow]', error.message)); } catch {} };
    current.onclose = () => {
      if (socket !== current) return;
      clearInterval(heartbeat); socket = null; authenticated = false; retryAt = Date.now() + retryDelay;
      setTimeout(connect, retryDelay); retryDelay = Math.min(retryDelay * 2, 20000);
    };
    current.onerror = () => {};
  } finally { connecting = false; if (epoch !== connectionEpoch) setTimeout(connect, 0); }
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
    downloads[id] = {batch: match[1], download_path: task.download_path, taskId: task.taskId,
      submissionId: task.submissionId, mediaId: task.mediaId};
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
  if (event.type === 'snapshot') {
    // A freshly claimed command may still be refused by a draining page.
    // Snapshot ownership belongs to actual page tasks, never that new claim.
    const batches = new Set((event.data?.tasks || []).map(task =>
      String(task.download_path || '').match(/^FlowBridge\/([a-f0-9]{32})\/result\.mp4$/)?.[1]).filter(Boolean));
    event.data = {...event.data, batch_id: batches.size === 1 ? [...batches][0] : ''};
  }
  if (['generation_response', 'binding_missing'].includes(event.type)) {
    // Late receipts belong to their saved submission, not a newer active batch.
    const batch = String(event.download_path || '').match(/^FlowBridge\/([a-f0-9]{32})\/result\.mp4$/)?.[1];
    if (!batch) return;
    event.batch_id = batch;
  }
  send(event);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.lzxConfig) {
    if (configSignature(changes.lzxConfig.oldValue) === configSignature(changes.lzxConfig.newValue) ||
        changes.lzxConfig.newValue && configSignature(changes.lzxConfig.newValue) === configSignature(config)) return;
    connectionEpoch++; authenticated = false; retryAt = 0; clearInterval(heartbeat);
    const previous = socket; socket = null; previous?.close(); setTimeout(connect, 50);
  }
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create('lzxReconnect', {periodInMinutes: 0.5}); connect(); });
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
    const timer = setTimeout(() => finish(Error('自动连接超时，请保持主程序打开后再点击连接。')), 10000);
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
    const timer = setTimeout(() => finish(Error('本机连接确认超时，请保持辅助工具打开。')), 10000);
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
        if (chrome.runtime.getManifest().version !== '1.1.15') {
          await chrome.storage.local.set({lzxReloadSetup: {...entry, tabId: sender.tab.id,
            mode: reply.mode || 'pair', profile: reply.profile, version: '1.1.15'}});
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
