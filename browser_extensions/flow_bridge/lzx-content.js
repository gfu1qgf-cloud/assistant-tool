(() => {
  if (window.top !== window) return;
  let ready = false;
  window.addEventListener('message', event => {
    if (event.source === window && event.origin === location.origin && event.data?.sender === 'lzx-flow-page') {
      if (event.data.data?.type === 'page_status') ready = true;
      chrome.runtime.sendMessage({action: 'lzxPageEvent', data: event.data.data}).catch(() => {});
    }
  });
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.action !== 'lzxCommand') return;
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
