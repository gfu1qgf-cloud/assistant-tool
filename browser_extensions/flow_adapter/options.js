const byId = id => document.getElementById(id);
const extensionId = 'pnjppjjfgiccpcfajkpooklgohgihdom';
let editing = false, setupGeneration = 0, setupSocket = null;
const expectedVersion = '1.1.22';
let timingValues = {};
const timingMs = (key,fallback) => Math.max(1, (Number.isFinite(timingValues[key]) && timingValues[key] > 0 ? timingValues[key] : fallback)*1000);
async function loadTiming() {
  const saved = await chrome.storage.local.get(['lzxTiming']);
  timingValues = saved?.lzxTiming || {};
}
function projectLink(value) {
  try {
    const url = new URL(String(value || '').trim());
    if (url.protocol !== 'https:' || !['labs.google', 'flow.google.com'].includes(url.hostname) ||
        url.username || url.password || (url.port && url.port !== '443') ||
        !/\/project\/[A-Za-z0-9_-]+\/?$/.test(url.pathname)) throw Error();
    url.hash = ''; url.pathname = url.pathname.replace(/\/$/, ''); return url.href;
  } catch { throw new Error('请手动填写本 Chrome 配置的 Flow 项目地址，不会从其他网页自动选择。'); }
}
function readPair(value) {
  let pair;
  try { pair = JSON.parse(value); } catch { throw new Error('请粘贴软件连接向导复制的完整连接信息。'); }
  if (!pair || typeof pair.token !== 'string' || pair.token.length < 32 ||
      !Number.isInteger(pair.port) || pair.port < 1024 || pair.port > 65535 ||
      (pair.extension_id && pair.extension_id !== extensionId) ||
      (pair.setup_id !== undefined && !/^[a-f0-9]{32}$/.test(pair.setup_id))) throw new Error('配对配置无效，请回软件重新准备连接信息。');
  return pair;
}
function profileName(value) {
  const text = String(value || '').trim();
  if (!text || text.length > 100 || ['.', '..'].includes(text) || /[\\/:"<>|?*\x00-\x1f]/.test(text)) {
    throw new Error('请填写正确的 Chrome 配置目录，例如 Profile 23 或 Default。');
  }
  return text;
}
chrome.storage.local.get('lzxConfig').then(({lzxConfig: config = {}}) => {
  if (!editing) {
    byId('profile').value = config.profile || ''; byId('project').value = config.project || '';
  }
  if (config.token) byId('pair').placeholder = '已配对；不更换连接信息时可留空';
});
byId('pair').addEventListener('input', () => {
  editing = true;
  const text = byId('pair').value.trim();
  if (!text) return;
  try {
    const pair = readPair(text);
    if (pair.profile !== undefined) byId('profile').value = profileName(pair.profile);
    if (pair.project !== undefined) byId('project').value = projectLink(pair.project);
    byId('status').textContent = '已自动填入所选配置名和项目地址；核对后点“保存并连接”。';
  } catch (error) { byId('status').textContent = error.message; }
});
async function savePair(provided = null) {
  byId('save').disabled = true;
  try {
    const {lzxConfig: previous = {}} = await chrome.storage.local.get('lzxConfig');
    const pair = provided || (byId('pair').value.trim() ? readPair(byId('pair').value.trim()) : readPair(JSON.stringify(previous)));
    const profile = profileName(byId('profile').value), project = projectLink(byId('project').value);
    const setup = pair.setup_id || previous.setup_id;
    await chrome.storage.local.set({lzxConfig: {token: pair.token, port: pair.port, profile, project, ...(setup ? {setup_id: setup} : {})}});
    byId('pair').value = ''; byId('status').textContent = '已保存本配置的项目地址，正在连接主程序。可回软件查看连接状态。';
    return true;
  } catch (error) { byId('status').textContent = error.message; return false; }
  finally { byId('save').disabled = false; }
}
byId('save').onclick = async () => {if (await savePair()) await waitForConnection();};

async function connectionStatus() {
  try { return await chrome.runtime?.sendMessage?.({action: 'lzxConnectionStatus'}); }
  catch { return null; }
}
async function waitForConnection() {
  await loadTiming();
  const {lzxConfig: expected = {}} = await chrome.storage.local.get('lzxConfig');
  const attempts = Math.max(1, Math.ceil(timingMs('setup_status_wait_seconds',10)/timingMs('setup_status_poll_seconds',.5)));
  for (let count = 0; count < attempts; count++) {
    const state = await connectionStatus();
    if (state?.connected && state.profile === expected.profile && state.project === expected.project &&
        state.setupId === (expected.setup_id || '')) {
      byId('status').textContent = '已连接主程序，可以回软件提交任务。Flow 项目会自动打开。';
      return true;
    }
    byId('status').textContent = '配置已保存，正在等待后台与主程序完成连接…';
    await new Promise(resolve => setTimeout(resolve, timingMs('setup_status_poll_seconds',.5)));
  }
  byId('status').textContent = '配置已保存，但后台尚未连上主程序。请保持辅助工具打开；若刚更新扩展，请在扩展管理页重新加载。不会重复生成。';
  return false;
}

async function automaticSetup() {
  await loadTiming();
  if (typeof location === 'undefined') return;
  const generation = ++setupGeneration;
  setupSocket?.close(); setupSocket = null;
  if (!await ensureExtensionCurrent() || generation !== setupGeneration) return;
  if (!location.hash) {
    const {lzxConfig: saved = {}} = await chrome.storage.local.get('lzxConfig');
    const state = await connectionStatus();
    if (state?.connected) {byId('status').textContent = '此浏览器已连接主程序，无需再次配对。'; return;}
    if (saved.token) {
      byId('status').textContent = '正在使用此浏览器已保存的配置自动重连，无需粘贴。';
      try {await chrome.runtime.sendMessage({action: 'lzxReconnect'}); await waitForConnection();}
      catch {byId('status').textContent = '正在等待扩展后台恢复；请保持主程序打开，无需粘贴配置。';}
      return;
    }
    byId('status').textContent = '等待主程序自动连接请求。在软件点击“打开浏览器并连接”后，项目页会在后台自行配置；此页无需粘贴任何内容。';
    return;
  }
  const args = new URLSearchParams(location.hash.slice(1));
  const ticket = args.get('setup'), port = Number(args.get('port'));
  if (!/^[a-f0-9]{32}$/.test(ticket || '') || !Number.isInteger(port) || port < 1024 || port > 65535) {
    byId('status').textContent = '自动连接入口无效，请回软件重新点击连接。'; return;
  }
  editing = true;
  byId('status').textContent = '正在向本机软件领取本次连接信息，无需复制粘贴…';
  const socket = new WebSocket(`ws://127.0.0.1:${port}`);
  setupSocket = socket;
  let done = false, receiving = false;
  const fail = text => {if (done) return; done = true; clearTimeout(timer); socket.close();
    if (generation === setupGeneration) byId('status').textContent = text;};
  const timer = setTimeout(() => fail('自动连接超时，请保持软件打开，再回软件点一次连接。'), timingMs('setup_claim_timeout_seconds',10));
  socket.onopen = () => socket.send(JSON.stringify({type: 'setup_claim', ticket}));
  socket.onerror = () => fail('无法连接本机软件，请确认辅助工具已打开，再点击连接。');
  socket.onmessage = async event => {
    if (generation !== setupGeneration) return;
    try {
      const message = JSON.parse(event.data);
      if (message.type === 'setup_error') {fail(message.error || '本次连接已过期，请回软件重新点击连接。'); return;}
      if (message.type !== 'setup_bundle' || done || receiving) return;
      const pair = readPair(JSON.stringify(message.pairing));
      if (pair.setup_id !== ticket) throw Error('连接步骤不匹配，请回软件重试。');
      byId('profile').value = profileName(pair.profile); byId('project').value = projectLink(pair.project);
      receiving = true; clearTimeout(timer); socket.close();
      if (await savePair(pair)) {
        if (typeof history !== 'undefined') history.replaceState(null, '', location.pathname);
        await waitForConnection();
      }
      done = true;
    } catch (error) {fail(error.message || '自动配置失败，请回软件重新连接。');}
  };
}
async function ensureExtensionCurrent() {
  const installed = chrome.runtime?.getManifest?.().version;
  if (!installed || installed === expectedVersion) return true;
  byId('status').textContent = `浏览器当前载入 ${installed}，正在准备自动更新到 ${expectedVersion}；不会生成视频。`;
  const entries = [], current = new URLSearchParams(location.hash.slice(1));
  if (current.has('setup')) entries.push({ticket: current.get('setup'), port: Number(current.get('port'))});
  for (const tab of await chrome.tabs.query({})) {
    try {
      const url = new URL(tab.url), args = new URLSearchParams(url.hash.slice(1));
      if (args.has('lzx_setup')) entries.push({ticket: args.get('lzx_setup'), port: Number(args.get('lzx_port')),
        project: projectLink(url.href), tabId: tab.id});
    } catch {}
  }
  if (!entries.length) {
    const {lzxConfig: saved = {}} = await chrome.storage.local.get('lzxConfig');
    if (saved.token && saved.profile && Number.isInteger(saved.port)) {
      const found = await new Promise(resolve => {
        const socket = new WebSocket(`ws://127.0.0.1:${saved.port}`);
        let finished = false;
        const finish = value => {if (finished) return; finished = true; clearTimeout(timer); socket.close(); resolve(value);};
        const timer = setTimeout(() => finish(null), timingMs('setup_discovery_timeout_seconds',3));
        socket.onopen = () => socket.send(JSON.stringify({type: 'setup_lookup', token: saved.token, profile: saved.profile}));
        socket.onerror = () => finish(null); socket.onclose = () => finish(null);
        socket.onmessage = event => {try {const reply = JSON.parse(event.data); finish(reply.type === 'setup_ticket' && reply.profile === saved.profile ? reply : null);} catch {finish(null);}};
      });
      if (found) entries.push(found);
    }
  }
  for (const entry of entries.slice(0, 8)) {
    if (!/^[a-f0-9]{32}$/.test(entry.ticket || '') || !Number.isInteger(entry.port) || entry.port < 1024 || entry.port > 65535) continue;
    const allowed = await new Promise(resolve => {
      const socket = new WebSocket(`ws://127.0.0.1:${entry.port}`);
      let finished = false;
      const finish = value => {if (finished) return; finished = true; clearTimeout(timer); socket.close(); resolve(value);};
      const timer = setTimeout(() => finish(null), timingMs('setup_discovery_timeout_seconds',3));
      socket.onopen = () => socket.send(JSON.stringify({type: 'setup_reload_check', ticket: entry.ticket}));
      socket.onerror = () => finish(null); socket.onclose = () => finish(null);
      socket.onmessage = event => {try {const message = JSON.parse(event.data);
        finish(message.type === 'setup_reload_allowed' && message.ticket === entry.ticket ? message : null);
      } catch {finish(null);}};
    });
    if (allowed) {
      await chrome.storage.local.set({lzxReloadSetup: {...entry, mode: allowed.mode || 'pair', project: allowed.project, profile: allowed.profile, version: expectedVersion}});
      byId('status').textContent = '已确认此配置没有正在生成／传输的任务，正在更新扩展并自动重连。';
      if (typeof history !== 'undefined') history.replaceState(null, '', location.pathname);
      chrome.runtime.reload(); return false;
    }
  }
  byId('status').textContent = `扩展仍载入 ${installed}。保持此页打开，在软件点一次连接，程序会授权空闲更新；不需要复制配对信息。`;
  return false;
}
globalThis.addEventListener?.('hashchange', automaticSetup);
chrome.storage.onChanged?.addListener((changes, area) => {
  if (area === 'local' && changes.lzxConfig && !location.hash) automaticSetup();
});
let refreshTimer;
const refreshSetup = tab => {if (tab?.url?.includes('lzx_setup=')) {
  clearTimeout(refreshTimer); refreshTimer = setTimeout(automaticSetup, timingMs('setup_refresh_debounce_seconds',.1));
}};
chrome.tabs?.onCreated?.addListener(refreshSetup);
chrome.tabs?.onUpdated?.addListener((_id, change, tab) => {if (change.url) refreshSetup(tab);});
automaticSetup();
