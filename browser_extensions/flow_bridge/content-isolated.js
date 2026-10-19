(() => {
if (window.top !== window) return;
// content-isolated.js - 消息桥梁脚本，运行在 ISOLATED 独立世界

// 向后台 Service Worker 发送消息，带有自动重试机制以解决 Service Worker 唤醒延迟/启动失败问题
function sendMessageToBackground(payload, maxRetries = 5, initialDelay = 500) {
  return new Promise((resolve) => {
    let attempt = 0;

    function execute() {
      attempt++;
      
      // 检查当前 context 是否已被销毁（例如扩展重装/重启后旧标签页的脚本）
      if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.sendMessage) {
        console.error(`[FlowIsolated] 插件上下文已失效（Orphaned Content Script）。请手动刷新页面。`);
        resolve({ error: "插件上下文已失效，请刷新页面重新连接。" });
        return;
      }

      try {
        chrome.runtime.sendMessage(payload, (response) => {
          // 检查是否有错误
          const lastError = chrome.runtime.lastError;
          
          if (lastError) {
            const errorMsg = lastError.message || "";
            console.warn(`[FlowIsolated] 发送消息失败 (尝试 ${attempt}/${maxRetries}): ${errorMsg}`);

            // 如果是上下文失效相关的错误，重试无意义，直接返回提示刷新
            if (errorMsg.includes("context invalidated") || errorMsg.includes("Extension context invalidated")) {
              resolve({ error: "插件上下文失效，请刷新页面重新连接: " + errorMsg });
              return;
            }

            // 对于原生拖放、自动下载等非幂等操作，若后台已收到消息但连接通道关闭（message port closed），绝不重试，避免重复上传或重复下载
            if ((payload?.action === "nativeFileDrag" || payload?.action === "download") && errorMsg.includes("message port closed")) {
              resolve({ ok: true, success: true, warning: errorMsg });
              return;
            }

            // 如果还没达到最大重试次数，进行延迟重试
            if (attempt < maxRetries) {
              const nextDelay = initialDelay * Math.pow(2, attempt - 1); // 指数退避: 500ms, 1000ms, 2000ms, 4000ms...
              console.log(`[FlowIsolated] 将在 ${nextDelay}ms 后尝试重新发送消息...`);
              setTimeout(execute, nextDelay);
            } else {
              // 达到最大重试次数，返回错误
              resolve({ error: `连接后台失败 (重试 ${maxRetries} 次): ${errorMsg}` });
            }
          } else {
            // 成功获取响应
            resolve(response || { success: true });
          }
        });
      } catch (e) {
        console.error(`[FlowIsolated] sendMessage 捕获到异常 (尝试 ${attempt}/${maxRetries}):`, e);
        const errorMsg = e.message || "";
        
        if (errorMsg.includes("context invalidated") || errorMsg.includes("Extension context invalidated")) {
          resolve({ error: "插件上下文失效，请刷新页面重新连接: " + errorMsg });
          return;
        }

        if (attempt < maxRetries) {
          const nextDelay = initialDelay * Math.pow(2, attempt - 1);
          setTimeout(execute, nextDelay);
        } else {
          resolve({ error: `发送消息异常: ${errorMsg}` });
        }
      }
    }

    execute();
  });
}

// 已处理的 callbackId 集合，防止主世界重发握手机制导致同一请求（如 nativeFileDrag）被执行两次
const handledCallbackIds = new Set();

// 1. 监听来自 MAIN 网页环境的 window.postMessage 消息
window.addEventListener("message", (event) => {
  // 安全校验：只处理当前 window 发送、且指定 sender 为 "flow-main" 的消息
  if (event.source !== window || !event.data || event.data.sender !== "flow-main") {
    return;
  }

  const { callbackId, payload } = event.data;
  if (callbackId) {
    // 立即通知主世界：桥接脚本已收到，取消 800ms / 2500ms 重发定时器
    window.postMessage({ sender: "flow-isolated-ack", callbackId }, "*");
    if (handledCallbackIds.has(callbackId)) {
      console.warn(`[FlowIsolated] 忽略重复重发的请求: action=${payload?.action}, callbackId=${callbackId}`);
      return;
    }
    handledCallbackIds.add(callbackId);
    setTimeout(() => handledCallbackIds.delete(callbackId), 120000);
  }
  console.log(`[FlowIsolated] 收到主页面 postMessage: action=${payload?.action}`);

  // 2. 将消息使用重试机制透传给插件后台 Service Worker
  sendMessageToBackground(payload).then((safeResponse) => {
    console.log(`[FlowIsolated] 收到后台响应并转发回主页面: action=${payload?.action}, response=`, safeResponse);
    // 3. 将后台返回的结果通过 postMessage 转发回 MAIN 网页环境
    window.postMessage({
      sender: "flow-isolated",
      callbackId: callbackId,
      response: safeResponse
    }, "*");
  });
});

// 监听来自后台 Service Worker 的广播通知 (如下载完成事件)
chrome.runtime.onMessage.addListener((message) => {
  if (message && ["downloadCompleted", "downloadFailed"].includes(message.action)) {
    console.log(`[FlowIsolated] 收到后台广播并转发到主页面: event=${message.action}`, message);
    window.postMessage({
      sender: "flow-isolated-event",
      event: message.action,
      data: message
    }, "*");
  }
});

console.log("%c[FlowIsolated] 消息桥梁脚本已成功注入当前页面！URL=" + location.href, "background: #3b82f6; color: white; padding: 2px 6px; border-radius: 4px; font-weight: bold;");

// 4. 守护保底：若主世界 UI 脚本未能成功挂载悬浮按钮，由独立世界兜底注入
function ensureFabMounted() {
  if (document.getElementById("flow-batch-fab")) return;
  
  const targetParent = document.body || document.documentElement;
  if (!targetParent) return;

  const fab = document.createElement("div");
  fab.id = "flow-batch-fab";
  fab.title = "打开批量生成助手";
  fab.style.cssText = "position: fixed !important; bottom: 120px !important; right: 24px !important; width: 56px !important; height: 56px !important; border-radius: 50% !important; background: linear-gradient(135deg, #8b5cf6, #3b82f6) !important; box-shadow: 0 8px 32px rgba(139, 92, 246, 0.6), inset 0 2px 4px rgba(255, 255, 255, 0.3) !important; display: flex !important; align-items: center !important; justify-content: center !important; cursor: pointer !important; z-index: 2147483630 !important; border: 2px solid rgba(255, 255, 255, 0.2) !important; box-sizing: border-box !important;";
  
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "28");
  svg.setAttribute("height", "28");
  svg.style.cssText = "width: 28px !important; height: 28px !important; fill: #ffffff !important; pointer-events: none !important;";
  
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z");
  path.setAttribute("fill", "#ffffff");
  
  svg.appendChild(path);
  fab.appendChild(svg);
  
  fab.addEventListener("click", () => {
    console.log("[FlowIsolated] 用户点击了 FAB 悬浮按钮，正在打开控制面板...");
    const overlay = document.getElementById("flow-batch-overlay");
    if (overlay) {
      overlay.style.cssText = "position: fixed !important; top: 0 !important; left: 0 !important; width: 100vw !important; height: 100vh !important; background: rgba(0, 0, 0, 0.75) !important; backdrop-filter: blur(12px) !important; z-index: 2147483640 !important; display: flex !important; align-items: center !important; justify-content: center !important; opacity: 1 !important; pointer-events: auto !important;";
      overlay.classList.add("active");
    }
    const panel = document.getElementById("flow-batch-panel");
    if (panel) {
      panel.style.cssText = "width: 92vw !important; height: 88vh !important; max-width: 1400px !important; background: #121216 !important; border: 1px solid rgba(255, 255, 255, 0.15) !important; border-radius: 20px !important; box-shadow: 0 24px 64px rgba(0, 0, 0, 0.8) !important; display: flex !important; flex-direction: column !important; color: #f3f4f6 !important; overflow: hidden !important; z-index: 2147483641 !important; pointer-events: auto !important;";
    }
    window.postMessage({ sender: "flow-isolated-open-panel" }, "*");
  });

  targetParent.appendChild(fab);
  console.log("[FlowIsolated] 已通过独立世界成功兜底挂载 FAB 悬浮按钮！", fab);
}

setTimeout(ensureFabMounted, 500);
setTimeout(ensureFabMounted, 1500);
setInterval(ensureFabMounted, 2500);


})();
