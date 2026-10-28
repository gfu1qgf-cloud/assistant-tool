// Small bounded media probe before creating an automatic Chrome download item.
// Never infer video readiness from a filename or a Google hostname alone.
(() => {
  const failure = message => Object.assign(Error(message), {code:'FLOW_DOWNLOAD_NOT_VIDEO'});
  const pending=new Map(), known=new Map(), blocked=new Map(), rejected=new Map();
  const managed=task=>/^FlowBridge\/[a-f0-9]{32}\/result\.mp4$/.test(task?.download_path || '') &&
    task.taskId && task.submissionId && task.mediaId;
  const taskKey=task=>JSON.stringify([task.download_path,task.taskId,task.submissionId,task.mediaId]);
  const href=value=>{try{return new URL(value).href;}catch{return '';}};
  const rejectionKey=task=>'lzxRejectedVideo:'+taskKey(task);
  async function rejectAddress(task,url) {
    if(!managed(task) || !href(url))return;
    const key=rejectionKey(task);
    let previous=rejected.get(key) || [];
    try {previous=(await bounded(chrome.storage.session?.get?.([key])))?.[key] || previous;}catch{}
    const addresses=[...new Set([...(Array.isArray(previous)?previous:[]),href(url)])].slice(-16);
    rejected.set(key,addresses);
    while(rejected.size>128)rejected.delete(rejected.keys().next().value);
    try {await bounded(chrome.storage.session?.set?.({[key]:addresses}));}catch{}
  }
  async function usableAddress(task,url) {
    const key=rejectionKey(task);let addresses=rejected.get(key) || [];
    try {addresses=(await bounded(chrome.storage.session?.get?.([key])))?.[key] || addresses;}catch{}
    if(Array.isArray(addresses) && addresses.includes(href(url)))
      throw Object.assign(Error('同一视频地址已返回网页或访问错误，未重复创建下载；等待原视频的新直链。'),{code:'FLOW_DOWNLOAD_REJECTED_URL'});
  }
  function videoAddress(value,task) {
    let final;
    try {final=new URL(value);}catch {throw failure('未取得可验证的视频最终地址，未创建下载项。');}
    const id=task.mediaId;
    // The address lookup RPC can require page authentication. A small successful
    // probe does not authorize passing that RPC URL to Chrome's download context.
    const safe=final.protocol==='https:' && !final.username && !final.password && (!final.port || final.port==='443') &&
      (final.hostname==='flow-content.google' && final.pathname===`/video/${id}` ||
       final.hostname==='flow.google.com' && final.pathname.match(/^\/asb\/([A-Za-z0-9_-]+)(?:=[A-Za-z0-9_,.-]+)?$/)?.[1]===id ||
       final.hostname.endsWith('.usercontent.google.com') || final.hostname.endsWith('.googleusercontent.com') ||
       final.hostname==='docs.google.com' && !/\/api\/|\/accounts?\b|\/signin\b/i.test(final.pathname));
    if(!safe)throw Object.assign(Error('取址接口尚未解析出真实视频直链，未创建下载项；等待原视频预览后重新取址。'),{code:'FLOW_DOWNLOAD_URL_UNRESOLVED'});
    if(final.hostname==='flow-content.google' && (!final.searchParams.get('Signature') || !Number.isFinite(Number(final.searchParams.get('Expires'))) || Number(final.searchParams.get('Expires'))*1000<=Date.now()+5000))
      throw failure('视频地址签名即将过期，未创建下载项；按原视频编号重新取址。');
    return final.href;
  }
  async function bounded(work) {
    let timer;
    try {return await Promise.race([work,new Promise(resolve=>{timer=setTimeout(()=>resolve(undefined),1000);})]);}
    finally {clearTimeout(timer);}
  }
  function remember(url,task) {
    if(!managed(task))return;
    pending.set(href(url),{task,at:Date.now()});
    for(const [key,entry] of pending)if(Date.now()-entry.at>300000)pending.delete(key);
    while(pending.size>128)pending.delete(pending.keys().next().value);
    while(known.size>256)known.delete(known.keys().next().value);
  }
  const invalid=item=>{
    const type=String(item.mime || '').split(';')[0].trim().toLowerCase();
    return type && !/^(?:video\/(?:mp4|quicktime)|application\/(?:octet-stream|mp4))$/.test(type) ||
      /\.(?:html?|json|txt)$/i.test(String(item.filename || '')) ||
      ['SERVER_UNAUTHORIZED','SERVER_FORBIDDEN','SERVER_BAD_CONTENT'].includes(item.error);
  };
  async function inspect(item,task=null) {
    if(!item || !Number.isInteger(item.id))return;
    let record=known.get(item.id);
    if(!record && managed(task))record={task};
    if(!record && item.byExtensionId===chrome.runtime.id)record=pending.get(href(item.url)) || pending.get(href(item.finalUrl));
    if(!record)return;
    known.set(item.id,record);
    record.url=href(item.url) || record.url;
    if(!invalid(item))return record.error;
    if(record.checking)return await record.checking;
    record.checking=(async()=>{
      let state=item.state;
      if(!['complete','interrupted'].includes(state)) {
        try {await bounded(chrome.downloads.cancel(item.id));}catch{}
        try {state=(await bounded(chrome.downloads.search({id:item.id})))?.[0]?.state;}catch{}
      }
      const settled=['complete','interrupted'].includes(state);
      await rejectAddress(record.task,record.url);
      const error=Object.assign(Error(settled?'Chrome实际返回网页／错误内容，已停止该下载；仅恢复原视频。':
        'Chrome实际返回非视频，尚未确认取消；停止新增下载，等待原下载状态。'),
        {code:settled?'FLOW_DOWNLOAD_ACTUAL_NOT_VIDEO':'FLOW_DOWNLOAD_OUTCOME_UNCONFIRMED'});
      record.error=error;
      if(!settled) {
        const key=taskKey(record.task);blocked.set(key,item.id);
        try {await chrome.storage.session?.set?.({['lzxUnconfirmedVideo:'+key]:item.id});}catch{}
      }
      return error;
    })();
    return await record.checking;
  }
  globalThis.__lzxReserveDownload=remember;
  globalThis.__lzxInspectDownloadItem=inspect;
  globalThis.__lzxGuardDownload=async(id,task)=>{
    if(!managed(task))return;
    known.set(id,known.get(id) || {task});
    const item=(await bounded(chrome.downloads.search({id})))?.[0];
    const error=await inspect(item,task);if(error)throw error;
  };
  chrome.downloads?.onCreated?.addListener(item=>{inspect(item).catch(()=>{});});
  chrome.downloads?.onChanged?.addListener(delta=>{
    if(!known.has(delta.id) && !delta.mime && !delta.state && !delta.filename)return;
    (async()=>{
      let task=known.get(delta.id)?.task;
      if(!task)try {const key=`download_${delta.id}`;task=(await chrome.storage.session?.get?.([key]))?.[key];}catch{}
      const items=await bounded(chrome.downloads.search({id:delta.id}));await inspect(items?.[0],task);
    })().catch(()=>{});
  });
  globalThis.__lzxPreflightVideo = async (url, task) => {
    if (!/^FlowBridge\/[a-f0-9]{32}\/result\.mp4$/.test(task?.download_path || '')) return;
    const key=taskKey(task), storageKey='lzxUnconfirmedVideo:'+key;
    try {const saved=await chrome.storage.session?.get?.([storageKey]);if(Number.isInteger(saved?.[storageKey]))blocked.set(key,saved[storageKey]);}catch{}
    if(blocked.has(key)) {
      const id=blocked.get(key),item=(await bounded(chrome.downloads.search({id})))?.[0];
      if(!item || !['complete','interrupted'].includes(item.state))throw Object.assign(Error('原非视频下载尚未确认结束，未创建新的下载项。'),{code:'FLOW_DOWNLOAD_OUTCOME_UNCONFIRMED'});
      blocked.delete(key);try {await chrome.storage.session?.remove?.(storageKey);}catch{}
    }
    const controller = new AbortController();
    let timer, reader, resolved;
    const probe = async () => {
      const response = await fetch(url,{method:'GET',credentials:'include',cache:'no-store',redirect:'follow',
        headers:{Range:'bytes=0-511'},signal:controller.signal});
      if (![200,206].includes(response.status)) throw failure('原视频尚未就绪或链接访问失败（HTTP '+response.status+'）；未创建下载项。');
      resolved=videoAddress(response.url,task);
      await usableAddress(task,resolved);
      const type=(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      const disposition=response.headers.get('content-disposition') || '';
      if (type && !/^(?:video\/(?:mp4|quicktime)|application\/(?:octet-stream|mp4))$/.test(type) ||
          /filename\*?\s*=.*\.(?:html?|json|txt)(?:["';\s]|$)/i.test(disposition))
        throw failure('原链接返回非视频内容（'+(type || '错误文件类型')+'），可能是错误页或登录页；未创建下载项。');
      reader=response.body?.getReader?.();
      if (!reader) throw failure('原视频响应无法读取，暂未创建下载项。');
      const header=new Uint8Array(32);let length=0;
      for(let reads=0;reads<8 && length<16;reads++) {
        const part=await reader.read();
        if(part.value?.length) {const count=Math.min(part.value.length,header.length-length);header.set(part.value.subarray(0,count),length);length+=count;}
        if(part.done)break;
      }
      if(length<12 || String.fromCharCode(...header.subarray(4,8))!=='ftyp')
        throw failure('原链接没有返回有效的 MP4 文件头，已拦截网页／错误内容；未创建下载项。');
    };
    try {
      await Promise.race([probe(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(failure('原视频内容预检超时，未创建下载项；稍后仅重试原视频下载。')),8000);})]);
      return resolved;
    } finally {
      clearTimeout(timer);controller.abort();
      try {reader?.cancel?.()?.catch?.(()=>{});} catch {}
    }
  };
})();

// background.js - 后台 Service Worker，负责处理下载任务、路径重定向与配置持久化

// 1. 监听来自 content-isolated.js 的消息
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // 后台版本握手：用于检测「网页脚本已更新但插件后台仍是旧版本」的情况
  if (message.action === "getBgVersion") {
    sendResponse({ version: "1.3.2-hosted", nativeDrag: !!(chrome.debugger && chrome.debugger.attach) });
    return;
  }

  // ===== 浏览器唯一 ID（每个浏览器配置独立，一旦生成永久保存，用于桌面软件精确识别/定位浏览器）=====
  if (message.action === "getInstanceInfo") {
    getInstanceInfo().then(sendResponse).catch(err => sendResponse({ error: String(err?.message || err) }));
    return true;
  }
  if (message.action === "resetInstanceId") {
    chrome.storage.local.remove(["flowcue_instance_id"], () => {
      getInstanceInfo().then(sendResponse).catch(err => sendResponse({ error: String(err?.message || err) }));
    });
    return true;
  }
  if (message.action === "setInstanceName") {
    const name = String(message.name || "").trim().slice(0, 40);
    chrome.storage.local.set({ flowcue_instance_name: name }, () => {
      getInstanceInfo().then(sendResponse).catch(err => sendResponse({ error: String(err?.message || err) }));
    });
    return true;
  }
  // 把本标签页所在窗口切到最前面并激活该标签页（桌面软件「定位浏览器」）
  if (message.action === "focusMyTab") {
    const tabId = sender.tab?.id;
    const windowId = sender.tab?.windowId;
    (async () => {
      try {
        if (windowId != null) {
          const w = await chrome.windows.get(windowId);
          const upd = { focused: true, drawAttention: true };
          if (w && w.state === "minimized") upd.state = "normal";
          await chrome.windows.update(windowId, upd);
        }
        if (tabId != null) await chrome.tabs.update(tabId, { active: true });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }
  // 兼容旧桥接消息；不再用全局当前任务重命名任何下载。
  if (["setActiveTask", "clearActiveTask"].includes(message.action)) {
    sendResponse({ success: true });
    return;
  }

  console.log(`[Background] 📥 收到来自 content-isolated 的消息: action=${message.action}`, message);

  if (message.action === "download") {
    handleDownload(message.url, message.filename, { ...message.task, tabId: sender.tab?.id, frameId: sender.frameId })
      .then(result => {
        console.log(`[Background] handleDownload 处理完毕返回:`, result);
        sendResponse(result);
      })
      .catch(err => {
        console.error(`[Background] handleDownload 处理异常:`, err);
        sendResponse({ error: err.message });
      });
    return true; // 保持通道开启以进行异步响应
  }

  if (message.action === "getDownloadStatus") {
    chrome.downloads.search({ id: message.downloadId }).then(items => {
      const item = items[0];
      console.log(`[Background] 查询 downloadId=${message.downloadId} 结果:`, item ? { state: item.state, error: item.error } : "不存在");
      sendResponse(item ? { state: item.state, error: item.error } : { error: "下载记录不存在" });
    }).catch(err => {
      console.error(`[Background] 查询 downloadId=${message.downloadId} 失败:`, err);
      sendResponse({ error: err.message });
    });
    return true;
  }

  if (message.action === "getStorage") {
    chrome.storage.local.get(message.keys, (result) => {
      sendResponse(result);
    });
    return true;
  }

  if (message.action === "setStorage") {
    chrome.storage.local.set(message.data, () => {
      sendResponse({ success: true });
    });
    return true;
  }

  // 本地图片原生拖放上传：通过 Chrome DevTools 协议把「本地绝对路径」作为真实文件拖进 Flow 页面
  if (message.action === "nativeFileDrag") {
    const tabId = sender.tab?.id;
    if (tabId == null) { sendResponse({ ok: false, error: "无法识别当前标签页" }); return; }
    dispatchNativeFileDrag(tabId, message.paths, message.x, message.y, message.ui_lease)
      .then(r => sendResponse(r))
      .catch(err => sendResponse({ ok: false, error: String(err?.message || err), dispatched:err.uploadDispatched }));
    return true;
  }

  // 原生硬件级鼠标点击保底：通过 Chrome DevTools 协议发出 isTrusted=true 的真实鼠标点击
  if (message.action === "nativeClick") {
    const tabId = sender.tab?.id;
    if (tabId == null) { sendResponse({ ok: false, error: "无法识别当前标签页" }); return; }
    dispatchNativeClick(tabId, message.x, message.y, message.ui_lease)
      .then(r => sendResponse(r))
      .catch(err => sendResponse({ ok: false, error: String(err?.message || err) }));
    return true;
  }

  // 原生硬件级鼠标悬停：用于唤醒 Google Flow 视频缩略图 (img.thumbnail) 的懒加载 <video> 标签
  if (message.action === "nativeHover") {
    const tabId = sender.tab?.id;
    if (tabId == null) { sendResponse({ ok: false, error: "无法识别当前标签页" }); return; }
    dispatchNativeHover(tabId, message.x, message.y, message.ui_lease)
      .then(r => sendResponse(r))
      .catch(err => sendResponse({ ok: false, error: String(err?.message || err) }));
    return true;
  }

  // 桌面软件「账号积分管理」：关闭本浏览器（关闭该浏览器配置下的所有窗口）
  if (message.action === "closeBrowser") {
    sendResponse({ success: true });
    setTimeout(() => {
      // 优先尝试关闭整个窗口
      chrome.windows.getAll({}, (wins) => {
        if (wins && wins.length > 0) {
            for (const w of wins) {
                chrome.windows.remove(w.id, () => {
                    // 如果关闭窗口失败（某些指纹浏览器可能拦截），则降级为关闭当前所有标签页
                    if (chrome.runtime.lastError) {
                        chrome.tabs.query({windowId: w.id}, (tabs) => {
                            for (const t of tabs) {
                                chrome.tabs.remove(t.id, () => void chrome.runtime.lastError);
                            }
                        });
                    }
                });
            }
        } else {
            // 保底关闭
            chrome.tabs.query({}, (tabs) => {
                for (const t of tabs) {
                    chrome.tabs.remove(t.id, () => void chrome.runtime.lastError);
                }
            });
        }
      });
    }, 200);
    return;
  }
});

// ===== 浏览器唯一 ID =====
// instance_id   : 存在 chrome.storage.local，每个浏览器配置（多开的每个浏览器）独立且永久不变，如 FC-7K3QX
// browser_session: 存在 chrome.storage.session，浏览器每次启动都会变化；
//                  用于区分「同一浏览器的两个标签页」与「被复制出来的浏览器配置（ID 重复）」
const ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";
function randomCode(len) {
  const buf = new Uint32Array(len);
  crypto.getRandomValues(buf);
  return Array.from(buf, n => ID_ALPHABET[n % ID_ALPHABET.length]).join("");
}
let memSessionId = "";
async function getBrowserSessionId() {
  try {
    if (chrome.storage.session) {
      const r = await chrome.storage.session.get(["flowcue_browser_session"]);
      if (r && r.flowcue_browser_session) return r.flowcue_browser_session;
      const sid = "S" + randomCode(8);
      await chrome.storage.session.set({ flowcue_browser_session: sid });
      return sid;
    }
  } catch (_) {}
  if (!memSessionId) memSessionId = "S" + randomCode(8);
  return memSessionId;
}
let instanceLock = Promise.resolve();
function getInstanceInfo() {
  const p = instanceLock.then(getInstanceInfoUnsafe);
  instanceLock = p.catch(() => {});
  return p;
}
async function getInstanceInfoUnsafe() {
  const r = await chrome.storage.local.get(["flowcue_instance_id", "flowcue_instance_name"]);
  let id = r.flowcue_instance_id;
  if (!id || !/^FC-[0-9A-Z]{4,8}$/.test(id)) {
    id = "FC-" + randomCode(5);
    await chrome.storage.local.set({ flowcue_instance_id: id });
  }
  return {
    instance_id: id,
    instance_name: r.flowcue_instance_name || "",
    browser_session: await getBrowserSessionId(),
    extension_id: chrome.runtime.id,
    user_agent: navigator.userAgent
  };
}

// 2.0 原生文件拖放（移植自 FlowStudio 插件的已验证方案）
// 原理：普通网页脚本 new File()/DataTransfer 伪造的拖放事件 isTrusted=false，Flow 会忽略；
// 而 Chrome DevTools 协议 Input.dispatchDragEvent 由浏览器内核发出「真实」拖放，
// 并且 data.files 直接接收本地绝对路径，浏览器自己读取文件，效果等同于用户从资源管理器拖入。
const dragLanes = new Map();
const recentTabDrags = new Map();
const DRAG_DEDUP_WINDOW_MS = 15000;

function serialByTab(tabId, fn) {
  const prev = dragLanes.get(tabId) || Promise.resolve();
  const p = prev.catch(() => {}).then(fn);
  dragLanes.set(tabId, p.catch(() => {}));
  return p;
}

async function dispatchNativeFileDrag(tabId, paths, x, y, uiToken) {
  const clean = [...new Set((paths || []).map(String).map(s => s.trim()).filter(Boolean))];
  if (!clean.length) throw new Error("没有可上传的本地文件路径");

  const ALLOWED_IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".webp", ".bmp"]);
  const validFiles = clean.filter(p => {
    const dot = p.lastIndexOf(".");
    return dot !== -1 && ALLOWED_IMAGE_EXTS.has(p.substring(dot).toLowerCase());
  });
  if (!validFiles.length) {
    throw new Error("安全拦截：原生拖放仅允许上传本地图片文件 (.jpg, .jpeg, .png, .webp, .bmp)");
  }

  const dedupKey = `${tabId}::${validFiles.map(f => f.toLowerCase()).sort().join("|")}`;

  const uiGuard=globalThis.__lzxUiNativeGuard?.(tabId,uiToken);
  return serialByTab(tabId, async () => {
    uiGuard?.check();
    const lastDragAt = recentTabDrags.get(dedupKey) || 0;
    if (Date.now() - lastDragAt < DRAG_DEDUP_WINDOW_MS) {
      console.warn(`[Background] 🛡️ 拦截到 ${DRAG_DEDUP_WINDOW_MS / 1000}s 内重复的原生拖放请求，已忽略 (tabId=${tabId}, files=${validFiles.length})`);
      return { ok: true, count: clean.length, method: "chrome_debugger_native_drag_dedup" };
    }

    const target = { tabId };
    let attachedHere = false, dropAttempted = false;
    try {
      try {
        uiGuard?.check();
        await chrome.debugger.attach(target, "1.3");
        attachedHere = true;
      } catch (e) {
        // 已被本扩展附加时继续使用；被其他调试器（如 DevTools 之外的工具）占用时报错
        if (!/already attached/i.test(String(e?.message || e))) throw e;
      }
      // 后台标签页也能正常处理拖放
      try {
        await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true });
        attachedHere = true; // Successful command proves this extension owns the session.
      } catch (_) {}
      const dragData = {
        items: [{ mimeType: "application/octet-stream", data: "", title: "", baseURL: "" }],
        files: validFiles,
        dragOperationsMask: 1
      };
      const px = Number.isFinite(Number(x)) ? Number(x) : 640;
      const py = Number.isFinite(Number(y)) ? Number(y) : 420;
      for (const type of ["dragEnter", "dragOver", "drop"]) {
        uiGuard?.check();
        if(type==="drop")dropAttempted=true;
        await chrome.debugger.sendCommand(target, "Input.dispatchDragEvent", { type, x: px, y: py, data: dragData });
        if (type !== "drop") await new Promise(r => setTimeout(r, 120));
      }
      recentTabDrags.set(dedupKey, Date.now());
      setTimeout(() => {
        if ((recentTabDrags.get(dedupKey) || 0) <= Date.now() - DRAG_DEDUP_WINDOW_MS) {
          recentTabDrags.delete(dedupKey);
        }
      }, DRAG_DEDUP_WINDOW_MS + 1000);
      console.log(`[Background] ✅ 原生拖放已发送 ${clean.length} 个文件 @(${px},${py})`);
      return { ok: true, count: clean.length, method: "chrome_debugger_native_drag" };
    } catch (failure) {failure.uploadDispatched=dropAttempted;throw failure;} finally {
      if (attachedHere) {
        try { await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: false }); } catch (_) {}
        try { await chrome.debugger.detach(target); } catch (_) {}
      }
    }
  }).finally(()=>uiGuard?.done());
}

async function dispatchNativeClick(tabId, x, y, uiToken) {
  const px = Number.isFinite(Number(x)) ? Math.round(Number(x)) : 0;
  const py = Number.isFinite(Number(y)) ? Math.round(Number(y)) : 0;
  if (px <= 0 || py <= 0) return { ok: false, error: "无效的点击坐标" };

  const uiGuard=globalThis.__lzxUiNativeGuard?.(tabId,uiToken);
  return serialByTab(tabId, async () => {
    uiGuard?.check();
    const target = { tabId };
    let attachedHere = false;
    try {
      try {
        uiGuard?.check();
        await chrome.debugger.attach(target, "1.3");
        attachedHere = true;
      } catch (e) {
        if (!/already attached/i.test(String(e?.message || e))) throw e;
      }
      try {
        await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true });
        attachedHere = true; // Successful command proves this extension owns the session.
      } catch (_) {}
      uiGuard?.check();
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mouseMoved", x: px, y: py, button: "none", buttons: 0
      });
      await new Promise(r => setTimeout(r, 35));
      uiGuard?.check();
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mousePressed", x: px, y: py, button: "left", buttons: 1, clickCount: 1
      });
      await new Promise(r => setTimeout(r, 45));
      uiGuard?.check();
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mouseReleased", x: px, y: py, button: "left", buttons: 0, clickCount: 1
      });
      console.log(`[Background] ✅ 原生硬件点击已发送 @(${px},${py})`);
      return { ok: true, method: "chrome_debugger_native_click" };
    } finally {
      if (attachedHere) {
        try { await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: false }); } catch (_) {}
        try { await chrome.debugger.detach(target); } catch (_) {}
      }
    }
  }).finally(()=>uiGuard?.done());
}

async function dispatchNativeHover(tabId, x, y, uiToken) {
  const px = Number.isFinite(Number(x)) ? Math.round(Number(x)) : 0;
  const py = Number.isFinite(Number(y)) ? Math.round(Number(y)) : 0;
  if (px <= 0 || py <= 0) return { ok: false, error: "无效的悬停坐标" };

  const uiGuard=globalThis.__lzxUiNativeGuard?.(tabId,uiToken);
  return serialByTab(tabId, async () => {
    uiGuard?.check();
    const target = { tabId };
    let attachedHere = false;
    try {
      try {
        uiGuard?.check();
        await chrome.debugger.attach(target, "1.3");
        attachedHere = true;
      } catch (e) {
        if (!/already attached/i.test(String(e?.message || e))) throw e;
      }
      try {
        await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true });
        attachedHere = true; // Successful command proves this extension owns the session.
      } catch (_) {}
      uiGuard?.check();
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mouseMoved", x: Math.max(1, px - 12), y: Math.max(1, py - 12), button: "none", buttons: 0
      });
      await new Promise(r => setTimeout(r, 40));
      uiGuard?.check();
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mouseMoved", x: px, y: py, button: "none", buttons: 0
      });
      await new Promise(r => setTimeout(r, 40));
      uiGuard?.check();
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mouseMoved", x: px + 3, y: py + 2, button: "none", buttons: 0
      });
      console.log(`[Background] ✅ 原生硬件悬停已发送 @(${px},${py})`);
      return { ok: true, method: "chrome_debugger_native_hover" };
    } finally {
      if (attachedHere) {
        try { await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: false }); } catch (_) {}
        try { await chrome.debugger.detach(target); } catch (_) {}
      }
    }
  }).finally(()=>uiGuard?.done());
}

// 记录近期已发起的自动下载（按 submissionId/mediaId/filename 去重），彻底杜绝同一视频触发两次 chrome.downloads.download
const recentAutoDownloads = new Map();

// 2. 处理直接下载逻辑
async function handleDownload(url, filename, task) {
  try {
    console.log(`[Background] 🎬 handleDownload 启动: URL=${url}, Filename=${filename}, task=`, task);
    const isManual = !!(task && task.manual);
    if (isManual && /^FlowBridge\//.test(filename || "")) throw new Error("托管任务只允许下载已验证的原媒体，不允许跳过归属校验。");
    // 规范化文件名中的斜杠，防止创建非法的多层级路径
    if (!isManual && (!task?.mediaId || !task.submissionId || !task.taskId || !filename || filename !== task.download_path)) {
      console.error("[Background] ❌ 拒绝下载: 缺少媒体 ID 或任务文件名绑定", { task, filename });
      throw new Error("缺少媒体 ID 或任务文件名绑定，拒绝下载");
    }
    if (isManual && !filename) {
      throw new Error("缺少目标文件名");
    }
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) {
      throw new Error("下载地址必须是无账号信息的标准 HTTPS 地址");
    }
    const flowAsb = parsed.hostname === "flow.google.com" ? parsed.pathname.match(/^\/asb\/([a-zA-Z0-9_-]+)(?:=[a-zA-Z0-9_,.-]+)?$/) : null;
    const isVidsHost = (
      parsed.hostname.endsWith(".usercontent.google.com") ||
      parsed.hostname.endsWith(".googleusercontent.com") ||
      parsed.hostname === "docs.google.com"
    );
    const isGoogleFlowHost = (
      parsed.hostname === "flow-content.google" ||
      !!flowAsb ||
      parsed.hostname === "labs.google" ||
      parsed.hostname.endsWith(".labs.google")
    );
    if (!isVidsHost && !isGoogleFlowHost) {
      console.error(`[Background] ❌ 拒绝下载: 域名不在受信任的 Google CDN 白名单中: ${parsed.hostname}`);
      throw new Error("下载域名不受信任，已安全拦截");
    }

    if (!isManual && parsed.hostname === "flow-content.google" && (!parsed.searchParams.get("Signature") || !parsed.searchParams.get("Expires"))) {
      console.error(`[Background] ❌ 拒绝下载: flow-content.google 视频直链缺少完整 CDN 签名参数 (Expires/Signature): ${url}`);
      throw new Error("视频直链缺少完整签名参数，等待有效签名直链");
    }

    const mediaId = parsed.hostname === "flow-content.google" ? parsed.pathname.match(/^\/video\/([a-zA-Z0-9_-]+)$/)?.[1] :
      flowAsb ? flowAsb[1] :
      parsed.hostname === "labs.google" && parsed.pathname === "/fx/api/trpc/media.getMediaUrlRedirect" ?
        ((parsed.searchParams.get("name") || "").split("/media/").pop().replace(/^media\//, "")) :
      isVidsHost ? task.mediaId : null;
    console.log(`[Background] URL 解析出的 mediaId: ${mediaId}, task.mediaId: ${task?.mediaId}, isVidsHost: ${isVidsHost}, isManual: ${isManual}`);
    if (!isManual && mediaId !== task.mediaId) {
      console.error(`[Background] ❌ 拒绝下载: 下载地址的媒体 ID (${mediaId}) 与任务 (${task.mediaId}) 不一致`);
      throw new Error("下载地址的媒体 ID 与任务不一致");
    }
    const sanitizedFilename = filename
      .replace(/\\/g, "/")
      .replace(/^\/+/, "")
      .split("/")
      .map(part => part.replace(/[<>:"|?*\r\n\t]/g, "_").replace(/\.+$/, "").trim())
      .filter(Boolean)
      .join("/");
    if (sanitizedFilename.split("/").some(part => !part || part === "." || part === ".." || /[<>:"|?*]/.test(part))) {
      console.error(`[Background] ❌ 拒绝下载: 文件名包含非法字符: ${sanitizedFilename}`);
      throw new Error("下载文件名无效");
    }
    if (!sanitizedFilename.toLowerCase().endsWith(".mp4")) {
      console.error(`[Background] ❌ 拒绝下载: 仅允许下载 .mp4 视频文件: ${sanitizedFilename}`);
      throw new Error("文件类型不安全，仅允许 .mp4 视频");
    }

    // 自动下载幂等去重保护：防止同一视频因重复消息或并发检测触发两次下载（产生 (1).mp4）
    const dedupKey = !isManual ? `${task.submissionId || ""}::${task.mediaId || mediaId || ""}::${sanitizedFilename}` : null;
    const mediaFileKey = !isManual ? `${task.mediaId || mediaId || ""}::${sanitizedFilename}` : null;
    if (!isManual) {
      const now = Date.now();
      for (const [k, v] of recentAutoDownloads.entries()) {
        if (now - v.ts > 300000) recentAutoDownloads.delete(k);
      }
      const existing = (dedupKey && recentAutoDownloads.get(dedupKey)) || (mediaFileKey && recentAutoDownloads.get(mediaFileKey));
      if (existing && (now - existing.ts < 300000)) {
        console.warn(`[Background] 🛡️ 拦截到重复的自动下载请求 (${sanitizedFilename}, mediaId=${mediaId})，直接复用已有下载请求!`);
        const result = await existing.promise;
        const item = result?.downloadId != null ? (await chrome.downloads.search({id: result.downloadId}))[0] : null;
        if (item && item.exists !== false && ["in_progress", "complete"].includes(item.state)) return result;
        if (dedupKey) recentAutoDownloads.delete(dedupKey);
        if (mediaFileKey) recentAutoDownloads.delete(mediaFileKey);
      }
    }

    const executeDownload = async () => {
      try {
        if(/^FlowBridge\//.test(task?.download_path || "") && typeof globalThis.__lzxPreflightVideo!=="function")throw Object.assign(Error("视频内容预检未载入，未启动下载；等待扩展自动恢复。"),{code:"FLOW_DOWNLOAD_GUARD_UNAVAILABLE"});
        const checked=await globalThis.__lzxPreflightVideo?.(url,task);
        if(/^FlowBridge\//.test(task?.download_path || "") && !checked)throw Object.assign(Error("未取得经过内容验证的视频直链，未启动下载。"),{code:"FLOW_DOWNLOAD_GUARD_UNAVAILABLE"});
        if(checked)url=checked;
        if(/^FlowBridge\//.test(task?.download_path || "") && new URL(url).hostname==="labs.google")throw Object.assign(Error("取址接口不能直接作为视频文件下载，未创建下载项。"),{code:"FLOW_DOWNLOAD_URL_UNRESOLVED"});
      }
      catch(failure) {
        if(dedupKey)recentAutoDownloads.delete(dedupKey);
        if(mediaFileKey)recentAutoDownloads.delete(mediaFileKey);
        return {error:String(failure.message || failure),code:failure.code || "FLOW_DOWNLOAD_PROBE_FAILED"};
      }
      // 在 Chrome 触发文件命名事件之前保存这个 URL 的独立文件名。
      await chrome.storage.session.set({ [`filename_${url}`]: sanitizedFilename });
      console.log(`[Background] 🚀 开始调用 Chrome API 下载视频: URL=${url}, Filename=${sanitizedFilename}`);
      
      const tryChromeDownload = (targetFilename) => new Promise((resolve) => {
        globalThis.__lzxReserveDownload?.(url,task);
        chrome.downloads.download({
          url: url,
          filename: targetFilename,
          conflictAction: 'uniquify',
          saveAs: false
        }, async (downloadId) => {
          try {
            if (chrome.runtime.lastError) {
              console.error(`[Background] ❌ chrome.downloads.download (${targetFilename}) 失败:`, chrome.runtime.lastError.message);
              resolve({ error: chrome.runtime.lastError.message });
            } else {
              console.log(`[Background] ✅ chrome.downloads.download 已启动，分配 ID=${downloadId}, Filename=${targetFilename}`);
              await globalThis.__lzxGuardDownload?.(downloadId,task);
              await chrome.storage.session.set({ [`download_${downloadId}`]: { ...task, filename: targetFilename, sanitizedFilename, url } });
              await globalThis.__lzxDownloadBound?.(downloadId, { ...task, filename: targetFilename, url });
              resolve({ success: true, downloadId: downloadId, filename: targetFilename });
              // 下载可能在关联信息落盘之前就完成。
              const items = await chrome.downloads.search({ id: downloadId });
              if (items[0]?.state === "complete" || items[0]?.state === "interrupted") {
                await notifyDownload(downloadId, items[0].state, items[0].error);
              }
            }
          } catch (err) {
            console.error("[Background] 下载跟踪失败", err);
            resolve({ error: err.message, code:err.code || "", downloadId });
          }
        });
      });

      let res = await tryChromeDownload(sanitizedFilename);
      // 如果部分多开浏览器因多级子目录限制导致下载失败，自动降级为单层文件名重命名下载
      if (!res.success && !/^FlowBridge\//.test(sanitizedFilename) && sanitizedFilename.includes("/")) {
        const flatName = task?.flatFilename || sanitizedFilename.split("/").pop();
        console.warn(`[Background] ⚠️ 子目录下载失败，自动降级为单层文件名重试: ${flatName}`);
        await chrome.storage.session.set({ [`filename_${url}`]: flatName });
        res = await tryChromeDownload(flatName);
      }
      if (!res.success && !isManual) {
        if (dedupKey) recentAutoDownloads.delete(dedupKey);
        if (mediaFileKey) recentAutoDownloads.delete(mediaFileKey);
      }
      return res;
    };

    const dlPromise = executeDownload();
    if (!isManual) {
      const entry = { ts: Date.now(), promise: dlPromise };
      if (dedupKey) recentAutoDownloads.set(dedupKey, entry);
      if (mediaFileKey) recentAutoDownloads.set(mediaFileKey, entry);
    }
    return await dlPromise;
  } catch (e) {
    console.error(`[Background] 下载函数捕获到异常:`, e);
    return { error: e.message };
  }
}

// 只处理本扩展发起、精确 URL/下载 ID 已登记的下载。
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (item.byExtensionId !== chrome.runtime.id) { suggest(); return; }
  const keys = [`download_${item.id}`, `filename_${item.url}`];
  if (item.finalUrl) keys.push(`filename_${item.finalUrl}`);
  chrome.storage.session.get(keys).then(async saved => {
    if(await globalThis.__lzxInspectDownloadItem?.(item,saved[`download_${item.id}`])){suggest();return;}
    const filename = saved[`download_${item.id}`]?.filename || saved[`filename_${item.url}`] || (item.finalUrl ? saved[`filename_${item.finalUrl}`] : null);
    console.log(`[Background] 📝 onDeterminingFilename: ID=${item.id}, URL=${item.url}, 匹配目标文件名=${filename}`);
    if (filename) suggest({ filename, conflictAction: "uniquify" });
    else suggest();
  }).catch(() => suggest());
  return true;
});

// 按下载 ID 保存归属，标签页切换或 Service Worker 重启不会串任务。
async function notifyDownload(id, state, error) {
  const key = `download_${id}`;
  const saved = await chrome.storage.session.get(key);
  const task = saved[key];
  if (!task || task.tabId == null) return;
  if (state !== "complete" && task.sanitizedFilename) {
    recentAutoDownloads.delete(`${task.submissionId || ""}::${task.mediaId || ""}::${task.sanitizedFilename}`);
    recentAutoDownloads.delete(`${task.mediaId || ""}::${task.sanitizedFilename}`);
  }
  try {
    console.log(`[Background] 📢 notifyDownload: 通知标签页 tabId=${task.tabId}, downloadId=${id}, state=${state}, error=${error}`);
    await chrome.tabs.sendMessage(task.tabId, {
      action: state === "complete" ? "downloadCompleted" : "downloadFailed",
      downloadId: id, task, error: error || "下载中断"
    }, { frameId: task.frameId ?? 0 });
    await chrome.storage.session.remove(key);
  } catch (err) { console.warn("[Background] 下载通知未送达", err); }
}
chrome.downloads.onChanged.addListener(delta => {
  if (["complete", "interrupted"].includes(delta.state?.current)) {
    console.log(`[Background] 🔄 downloads.onChanged: ID=${delta.id}, state=${delta.state?.current}, error=${delta.error?.current || '无'}`);
    notifyDownload(delta.id, delta.state.current, delta.error?.current);
  }
});

// Share the native-operation lane; never capture another tab or keep debugger attached.
globalThis.__lzxCaptureUpload = tabId => serialByTab(tabId,async () => {
  const target={tabId}; let owned=false;
  const command=(name,params={})=>{
    let timer;
    return Promise.race([chrome.debugger.sendCommand(target,name,params),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error("截图响应超时")),8000);})]).finally(()=>clearTimeout(timer));
  };
  try {
    try {await chrome.debugger.attach(target,"1.3");owned=true;}
    catch(error) {if(!/already attached/i.test(String(error.message || error)))throw error;}
    const metrics=await command("Page.getLayoutMetrics");
    owned=true; // A successful command proves this extension owns the session.
    const viewport=metrics.cssVisualViewport;
    if(!viewport || viewport.clientWidth<64 || viewport.clientHeight<64 || viewport.clientWidth>10000 || viewport.clientHeight>10000)throw Error("项目截图视口无效");
    const result=await command("Page.captureScreenshot",{format:"png",fromSurface:true,captureBeyondViewport:false,
      clip:{x:viewport.pageX,y:viewport.pageY,width:viewport.clientWidth,height:viewport.clientHeight,scale:1}});
    if(!result.data || result.data.length>16000000)throw Error("项目截图过大或为空");
    return {data:result.data,width:viewport.clientWidth,height:viewport.clientHeight};
  } finally {if(owned)try {await chrome.debugger.detach(target);}catch{}}
});
// Host-authenticated recovery reuses the exact upstream bound downloader.
globalThis.__lzxRetryBoundDownload = handleDownload;
