(() => {
  if (window.top !== window) return;
  if (window.__lzxFlowContent) return;
  window.__lzxFlowContent = true;
  let ready = false, lastPageAt = 0, running, bootReady, nativeBusy, referenceReady;
  let timingValues = {};
  const timingMs = (key,fallback) => Math.max(1, (Number.isFinite(timingValues[key]) && timingValues[key] > 0 ? timingValues[key] : fallback)*1000);
  function applyTiming(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
    timingValues = Object.fromEntries(Object.entries(raw).filter(([key,item]) => /^(page_|bridge_|setup_)/.test(key) &&
      typeof item === 'number' && Number.isFinite(item) && item > 0 && item <= 86400));
    window.postMessage({sender:'lzx-flow-extension',message:{action:'configure_timing',values:timingValues}},location.origin);
  }
  Promise.resolve(chrome.storage?.local?.get?.(['lzxTiming'])).then(saved=>applyTiming(saved?.lzxTiming)).catch(()=>{});
  window.addEventListener('message', event => {
    if (event.source === window && event.origin === location.origin && event.data?.sender === 'lzx-flow-page') {
      if (['page_status', 'snapshot'].includes(event.data.data?.type)) {
        ready = true; lastPageAt = Date.now();
        if (typeof event.data.data?.data?.running === 'boolean') running = event.data.data.data.running;
        const state = event.data.data.type === 'snapshot' ? event.data.data.data : event.data.data;
        if (typeof state?.boot_ready === 'boolean') bootReady = state.boot_ready;
        if (typeof state?.reference_ready === 'boolean') referenceReady = state.reference_ready;
        if (typeof state?.native_busy === 'boolean') nativeBusy = state.native_busy;
      }
      chrome.runtime.sendMessage({action: 'lzxPageEvent', data: event.data.data}).catch(() => {});
    }
  });
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.action === 'lzxTiming') {applyTiming(message.values); reply({configured:true}); return;}
    if (message.action === 'lzxPageHealth') {
      window.postMessage({sender: 'lzx-flow-extension', message: {action: 'snapshot'}}, location.origin);
      window.postMessage({sender: 'lzx-flow-extension', message: {action: 'ready'}}, location.origin);
      setTimeout(() => reply({healthy: !!window.__lzxFlowNativeBridge && ready && bootReady !== false && referenceReady !== false && Date.now() - lastPageAt < timingMs('page_health_valid_seconds',1.5) &&
        !!document.getElementById('flow-batch-fab') && !!document.getElementById('flow-batch-overlay'),
        native_bridge:!!window.__lzxFlowNativeBridge, reference_ready:referenceReady, running, native_busy:nativeBusy}), timingMs('page_health_wait_seconds',.3));
      return true;
    }
    if (message.action !== 'lzxCommand') return;
    applyTiming(message.message?.timing);
    if (!ready) {
      window.postMessage({sender: 'lzx-flow-extension', message: {action: 'ready'}}, location.origin);
      reply({accepted: false}); return;
    }
    window.postMessage({sender: 'lzx-flow-extension', message: message.message}, location.origin);
    reply({accepted: true});
  });
  window.postMessage({sender: 'lzx-flow-extension', message: {action: 'ready'}}, location.origin);
  let pairing = false;
  async function setupFromProject() {
    if (pairing || !new URLSearchParams(location.hash.slice(1)).has('lzx_setup')) return;
    pairing = true;
    const entry = location.href;
    try {
      const result = await chrome.runtime.sendMessage({action: 'lzxSetupFromProject'});
      if (result?.success && location.href === entry) {
        history.replaceState(null, '', location.pathname + location.search);
      } else if (!result?.success) console.warn('[AssistantFlow] 自动连接未完成：', result?.error || '请正常重启主程序和更新扩展后重试');
    } catch (error) {console.warn('[AssistantFlow] 自动连接未完成：', error.message);}
    finally {pairing = false;}
  }
  window.addEventListener('hashchange', setupFromProject);
  setupFromProject();
})();
