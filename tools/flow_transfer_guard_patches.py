"""Small repeat-upload / stale-message / download seams; vendor stays intact."""

def exact(text,before,after,count=1):
    if text.count(before)!=count:raise ValueError('传输接入点变化：'+before[:90])
    return text.replace(before,after)

def patch_ui(text):
    text=exact(text,'  function sendToExtension(action, data) {',
        '  function sendToExtension(action, data) {\n'
        '    const uiLease = ["nativeFileDrag","nativeClick","nativeHover"].includes(action) ? window.__lzxFlowTransport?.uiLeaseToken?.() : undefined;')
    text=exact(text,'payload: { action, ...data }', 'payload: { action, ...data, ...(uiLease?{ui_lease:uiLease}:{}) }')
    text=exact(text,'operationGuard = null, forceUpload = false } = {}) {',
        'operationGuard = null, forceUpload = false, verifyOnly = false, knownBeforeIds = null, beforeDispatch = null } = {}) {')
    text=exact(text,'const oldIds = new Set(forceUpload ? getLoadedPageImages().map(img=>img.primaryMediaKey || img.id) : []);',
        'const oldIds = new Set(knownBeforeIds || (forceUpload ? getLoadedPageImages().map(img=>img.primaryMediaKey || img.id) : []));')
    text=exact(text,'      const dispatched = [];\n      for (let i = 0; i < todo.length; i += UPLOAD_BATCH_SIZE) {',
        '      const dispatched = verifyOnly ? todo.slice() : [];\n      for (let i = 0; !verifyOnly && i < todo.length; i += UPLOAD_BATCH_SIZE) {')
    text=exact(text,'const res = await sendToExtension("nativeFileDrag", { paths: chunk, x: pt.x, y: pt.y });',
        'await beforeDispatch?.();\n        const res = await sendToExtension("nativeFileDrag", { paths: chunk, x: pt.x, y: pt.y });')
    text=exact(text,'if (!res || res.ok === false || res.error) failAll(chunk, "拖放失败：" + (res?.error || "未知错误"));',
        'if (!res || res.ok === false || res.error) {\n'
        '          if(res?.dispatched===false)result.not_dispatched=[...(result.not_dispatched || []),...chunk];\n'
        '          failAll(chunk, "拖放失败：" + (res?.error || "未知错误"));\n        }')
    # A previous unknown drag is only observed, never repeated. Its original
    # before-ID snapshot remains available to the strict visual fallback.
    text=exact(text,'visualCount = 0, visualAt = start;', 'visualCount = 0, visualAt = verifyOnly ? start - 15000 : start;')
    text=exact(text,'  const extensionCallbacks = new Map();',
        '  const extensionCallbacks = new Map();\n  const deferredBridgeErrors = new Map(), liveBridgeAcks = new Set();\n'
        '  let lastBridgeWarning = -Infinity;\n'
        '  function reportBridgeFailure() {\n'
        '    if (Date.now()-lastBridgeWarning<30000) return;\n'
        '    lastBridgeWarning=Date.now();\n'
        '    showToast("网页状态保存连接已失效；请在任务空闲后刷新此项目页恢复。",8000);\n'
        '  }')
    text=exact(text,'      if (event.data.callbackId) ackedCallbacks.add(event.data.callbackId);',
        '      if (event.data.callbackId) {\n'
        '        ackedCallbacks.add(event.data.callbackId);\n'
        '        if (event.data.live === true) liveBridgeAcks.add(event.data.callbackId);\n'
        '      }')
    anchor='    const { callbackId, response } = event.data;\n    ackedCallbacks.delete(callbackId);'
    text=exact(text,anchor,'''    const { callbackId, response } = event.data;
    if (extensionCallbacks.has(callbackId) && /失效|invalidated/i.test(String(response?.error || ""))) {
      if (!deferredBridgeErrors.has(callbackId)) deferredBridgeErrors.set(callbackId,setTimeout(() => {
        deferredBridgeErrors.delete(callbackId);
        if (extensionCallbacks.has(callbackId) && !liveBridgeAcks.has(callbackId)) {
          const resolve=extensionCallbacks.get(callbackId);extensionCallbacks.delete(callbackId);
          ackedCallbacks.delete(callbackId);resolve(response);
        }
      },1500));
      return; // A dead old listener cannot override a live bridge's response.
    }
    clearTimeout(deferredBridgeErrors.get(callbackId));deferredBridgeErrors.delete(callbackId);
    liveBridgeAcks.delete(callbackId);
    ackedCallbacks.delete(callbackId);''')
    text=exact(text,'        ackedCallbacks.delete(callbackId);\n        if (extensionCallbacks.has(callbackId)) {',
        '        ackedCallbacks.delete(callbackId);liveBridgeAcks.delete(callbackId);\n'
        '        clearTimeout(deferredBridgeErrors.get(callbackId));deferredBridgeErrors.delete(callbackId);\n'
        '        if (extensionCallbacks.has(callbackId)) {')
    text=exact(text,'showToast("检测到插件连接失效，请刷新网页以重新连接！", 8000);','reportBridgeFailure();')
    text=exact(text,'showToast("检测到插件连接失效或重启，请刷新网页以重新连接！", 8000);','reportBridgeFailure();')
    # The native drag is a foreground action; the following image/transfer
    # observation is read-only and can let another browser use the desktop.
    text=exact(text,'    for (let i = 0; uploadInFlight && i < 60; i++) {operationGuard?.(); await delay(500);}',
        '''    const waitOtherUpload = async () => {
      for (let i = 0; uploadInFlight && i < 60; i++) {operationGuard?.(); await delay(500);}
    };
    if (uploadInFlight && window.__lzxFlowTransport?.uiWait) await window.__lzxFlowTransport.uiWait(waitOtherUpload);
    else await waitOtherUpload();''')
    text=exact(text,'        await beforeDispatch?.();\n        const res = await sendToExtension("nativeFileDrag",',
        '''        try {
          await beforeDispatch?.();
          operationGuard?.();
          window.__lzxFlowTransport?.assertUi?.();
        } catch (failure) {
          // No native message has been sent. Release the pending-upload latch
          // even when a reset or lease expiry interrupts the final checks.
          failure.uploadDispatched=false;
          throw failure;
        }
        const res = await sendToExtension("nativeFileDrag",''')
    text=exact(text,'        await delay(1500);\n      }\n\n      // 轮询确认',
        '        if (!window.__lzxFlowTransport?.uiWait) await delay(1500);\n      }\n\n      // 轮询确认')
    text=exact(text,'      const start = Date.now(), managed = !!window.__lzxFlowTransport;',
        '      const observeUpload = async () => {\n'
        '      if (window.__lzxFlowTransport?.uiWait) await delay(1500);\n'
        '      const start = Date.now(), managed = !!window.__lzxFlowTransport;')
    text=exact(text,'      if (result.uploaded.length) await delay(2000);\n      return result;',
        '      if (result.uploaded.length) await delay(2000);\n      return result;\n'
        '      };\n'
        '      return window.__lzxFlowTransport?.uiWait ? await window.__lzxFlowTransport.uiWait(observeUpload) : await observeUpload();')
    return text

def patch_isolated(text):
    # A destroyed context cannot deliver its first native request. Do not
    # mistake this definite no-send failure for an uncertain completed drop.
    text=exact(text,'resolve({ error: "插件上下文已失效，请刷新页面重新连接。" });',
        'resolve({ error: "插件上下文已失效，请刷新页面重新连接。", dispatched:attempt===1?false:undefined });')
    text=exact(text,'resolve({ error: "插件上下文失效，请刷新页面重新连接: " + errorMsg });',
        'resolve({ error: "插件上下文失效，请刷新页面重新连接: " + errorMsg, dispatched:attempt===1?false:undefined });',count=2)
    text=exact(text,'window.addEventListener("message", (event) => {','const nativeBridgeListener = (event) => {')
    text=exact(text,'  const { callbackId, payload } = event.data;', '''  // Reloaded content scripts can remain on the page with a dead runtime.
  // Retire before ACK or dispatch; a new bridge may already be serving it.
  try {
    if (!chrome.runtime?.sendMessage || chrome.runtime.getManifest && !chrome.runtime.getManifest()) throw Error("invalidated");
  } catch {
    window.__lzxFlowNativeBridge = false;
    window.removeEventListener("message",nativeBridgeListener);
    window.postMessage({sender:"flow-isolated",callbackId:event.data.callbackId,
      response:{error:"插件上下文失效，旧消息桥已退出；等待当前桥接恢复。",dispatched:false}},"*");
    return;
  }
  const { callbackId, payload } = event.data;''')
    text=exact(text,'window.postMessage({ sender: "flow-isolated-ack", callbackId }, "*");',
        'window.postMessage({ sender: "flow-isolated-ack", callbackId, live:true }, "*");')
    text=exact(text,'});\n\n// 监听来自后台 Service Worker',
        '};\nwindow.addEventListener("message",nativeBridgeListener);\n\n// 监听来自后台 Service Worker')
    return text

def patch_background(text, guard):
    start=text.index('async function dispatchNativeFileDrag(');end=text.index('async function dispatchNativeClick(',start)
    drag=text[start:end]
    drag=exact(drag,'    let attachedHere = false;','    let attachedHere = false, dropAttempted = false;')
    drag=exact(drag,'        await chrome.debugger.sendCommand(target, "Input.dispatchDragEvent",',
        '        if(type==="drop")dropAttempted=true;\n        await chrome.debugger.sendCommand(target, "Input.dispatchDragEvent",')
    drag=exact(drag,'    } finally {','    } catch (failure) {failure.uploadDispatched=dropAttempted;throw failure;} finally {')
    text=text[:start]+drag+text[end:]
    # Only the managed UI lease adds a fence. Manual operation with mutex OFF
    # remains vendor behaviour; cleanup commands always run after revocation.
    start=text.index('async function dispatchNativeFileDrag(')
    end=text.index('// Share the native-operation lane;',start)
    native=text[start:end]
    for function,args in [('dispatchNativeFileDrag','tabId, paths, x, y'),('dispatchNativeClick','tabId, x, y'),('dispatchNativeHover','tabId, x, y')]:
        native=exact(native,'async function '+function+'('+args+')',
            'async function '+function+'('+args+', uiToken)')
    native=exact(native,'  return serialByTab(tabId, async () => {',
        '  const uiGuard=globalThis.__lzxUiNativeGuard?.(tabId,uiToken);\n  return serialByTab(tabId, async () => {\n    uiGuard?.check();',count=3)
    native=exact(native,'        await chrome.debugger.attach(target, "1.3");',
        '        uiGuard?.check();\n        await chrome.debugger.attach(target, "1.3");',count=3)
    native=exact(native,'        if(type==="drop")dropAttempted=true;',
        '        uiGuard?.check();\n        if(type==="drop")dropAttempted=true;')
    native=exact(native,'      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent",',
        '      uiGuard?.check();\n      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent",',count=6)
    native=exact(native,'    }\n  });\n}', '    }\n  }).finally(()=>uiGuard?.done());\n}',count=3)
    text=text[:start]+native+text[end:]
    text=exact(text,'dispatchNativeFileDrag(tabId, message.paths, message.x, message.y)\n      .then(r => sendResponse(r))\n      .catch(err => sendResponse({ ok: false, error: String(err?.message || err) }));',
        'dispatchNativeFileDrag(tabId, message.paths, message.x, message.y, message.ui_lease)\n      .then(r => sendResponse(r))\n      .catch(err => sendResponse({ ok: false, error: String(err?.message || err), dispatched:err.uploadDispatched }));')
    for function in ('dispatchNativeClick','dispatchNativeHover'):
        text=exact(text,function+'(tabId, message.x, message.y)',function+'(tabId, message.x, message.y, message.ui_lease)')
    text=exact(text,'        chrome.downloads.download({',
        '        globalThis.__lzxReserveDownload?.(url,task);\n        chrome.downloads.download({')
    text=exact(text,'              await chrome.storage.session.set({ [`download_${downloadId}`]: { ...task, filename: targetFilename, sanitizedFilename, url } });',
        '              await globalThis.__lzxGuardDownload?.(downloadId,task);\n'
        '              await chrome.storage.session.set({ [`download_${downloadId}`]: { ...task, filename: targetFilename, sanitizedFilename, url } });')
    text=exact(text,'            resolve({ error: err.message });',
        '            resolve({ error: err.message, code:err.code || "", downloadId });')
    text=exact(text,'  chrome.storage.session.get(keys).then(saved => {',
        '  chrome.storage.session.get(keys).then(async saved => {\n'
        '    if(await globalThis.__lzxInspectDownloadItem?.(item,saved[`download_${item.id}`])){suggest();return;}')
    return guard+'\n'+exact(text,'    const executeDownload = async () => {',
        '    const executeDownload = async () => {\n'
        '      try {\n'
        '        if(/^FlowBridge\\//.test(task?.download_path || "") && typeof globalThis.__lzxPreflightVideo!=="function")throw Object.assign(Error("视频内容预检未载入，未启动下载；等待扩展自动恢复。"),{code:"FLOW_DOWNLOAD_GUARD_UNAVAILABLE"});\n'
        '        const checked=await globalThis.__lzxPreflightVideo?.(url,task);\n'
        '        if(/^FlowBridge\\//.test(task?.download_path || "") && !checked)throw Object.assign(Error("未取得经过内容验证的视频直链，未启动下载。"),{code:"FLOW_DOWNLOAD_GUARD_UNAVAILABLE"});\n'
        '        if(checked)url=checked;\n'
        '        if(/^FlowBridge\\//.test(task?.download_path || "") && new URL(url).hostname==="labs.google")throw Object.assign(Error("取址接口不能直接作为视频文件下载，未创建下载项。"),{code:"FLOW_DOWNLOAD_URL_UNRESOLVED"});\n'
        '      }\n'
        '      catch(failure) {\n'
        '        if(dedupKey)recentAutoDownloads.delete(dedupKey);\n'
        '        if(mediaFileKey)recentAutoDownloads.delete(mediaFileKey);\n'
        '        return {error:String(failure.message || failure),code:failure.code || "FLOW_DOWNLOAD_PROBE_FAILED"};\n'
        '      }')
