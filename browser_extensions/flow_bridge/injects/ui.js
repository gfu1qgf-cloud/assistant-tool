// injects/ui.js - 核心 UI 渲染及批量生成自动化控制脚本 (支持手动 JSON 导出对接与断点重载刷新)

(function() {
  "use strict";
  const console = window.__lzxFlowTransport ? {log() {}, warn: globalThis.console.warn.bind(globalThis.console), error: globalThis.console.error.bind(globalThis.console)} : globalThis.console;
  if (window.top !== window) return;

  console.log("[FlowUI] 界面控制器正在初始化...");

  // ----------------------------------------------------
  // 0. Trusted Types 兼容与安全 DOM 操作封装 (针对 flow.google.com CSP 限制)
  // ----------------------------------------------------
  let flowTrustedPolicy = null;
  if (typeof window !== "undefined" && window.trustedTypes && window.trustedTypes.createPolicy) {
    try {
      flowTrustedPolicy = window.trustedTypes.createPolicy("flow-ui-policy", {
        createHTML: (s) => s,
        createScript: (s) => s,
        createScriptURL: (s) => s,
      });
    } catch (e) {
      try {
        flowTrustedPolicy = window.trustedTypes.createPolicy("default", {
          createHTML: (s) => s,
          createScript: (s) => s,
          createScriptURL: (s) => s,
        });
      } catch (err) {
        flowTrustedPolicy = window.trustedTypes.defaultPolicy || null;
      }
    }
  }

  function setSafeHTML(element, htmlContent) {
    if (!element) return;
    try {
      if (flowTrustedPolicy) {
        element.innerHTML = flowTrustedPolicy.createHTML(htmlContent);
      } else if (window.trustedTypes && window.trustedTypes.defaultPolicy) {
        element.innerHTML = window.trustedTypes.defaultPolicy.createHTML(htmlContent);
      } else {
        element.innerHTML = htmlContent;
      }
    } catch (err) {
      console.warn("[FlowUI] innerHTML 受 Trusted Types 拦截，使用 DOMParser 安全降级注入:", err);
      try {
        const doc = new DOMParser().parseFromString(htmlContent, "text/html");
        element.replaceChildren(...doc.body.childNodes);
      } catch (domErr) {
        console.warn("[FlowUI] DOMParser 降级失败，尝试 ContextualFragment:", domErr);
        try {
          const range = document.createRange();
          range.selectNodeContents(document.documentElement || document.body);
          const fragment = range.createContextualFragment(htmlContent);
          element.replaceChildren(fragment);
        } catch (rangeErr) {
          console.error("[FlowUI] 无法注入 HTML 内容:", rangeErr);
        }
      }
    }
  }

  // ----------------------------------------------------
  // 1. 消息中间件（使用 window.postMessage 桥接后台）
  // ----------------------------------------------------
  const extensionCallbacks = new Map();
  const ackedCallbacks = new Set();
  let isolatedBridgeReady = false;
  const NON_RETRYABLE_ACTIONS = new Set([
    "nativeFileDrag", "nativeClick", "nativeHover", "download", "closeBrowser", "focusMyTab"
  ]);

  function sendToExtension(action, data) {
    return new Promise((resolve) => {
      const callbackId = Math.random().toString(36).substring(2, 9) + "_" + Date.now().toString(36);
      extensionCallbacks.set(callbackId, resolve);

      const postPayload = () => {
        if (!extensionCallbacks.has(callbackId)) return;
        window.postMessage({
          sender: "flow-main",
          callbackId: callbackId,
          payload: { action, ...data }
        }, "*");
      };

      // 仅在页面刚启动、content-isolated.js 尚未就绪且为只读初始化请求时才做握手机制重发；
      // 严禁对 nativeFileDrag 等有副作用的耗时操作在 800ms/2500ms 重发，否则一旦拖放耗时超过 800ms 就会触发二次/三次重复上传！
      if (!isolatedBridgeReady && !NON_RETRYABLE_ACTIONS.has(action)) {
        setTimeout(() => {
          if (!isolatedBridgeReady && !ackedCallbacks.has(callbackId)) postPayload();
        }, 800);
        setTimeout(() => {
          if (!isolatedBridgeReady && !ackedCallbacks.has(callbackId)) postPayload();
        }, 2500);
      }

      const timeoutMs = action === "nativeFileDrag" ? 45000 : 15000;
      setTimeout(() => {
        ackedCallbacks.delete(callbackId);
        if (extensionCallbacks.has(callbackId)) {
          extensionCallbacks.delete(callbackId);
          console.warn(`[FlowUI] sendToExtension(${action}) ${timeoutMs / 1000}秒无响应，自动跳过`);
          resolve({ error: "timeout" });
        }
      }, timeoutMs);

      postPayload();
    });
  }

  // 监听来自 content-isolated.js 的响应与广播事件
  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data) return;

    if (event.data.sender === "flow-isolated-ack") {
      isolatedBridgeReady = true;
      if (event.data.callbackId) ackedCallbacks.add(event.data.callbackId);
      return;
    }

    if (event.data.sender === "flow-isolated-open-panel") {
      isolatedBridgeReady = true;
      openPanel();
      return;
    }

    if (event.data.sender === "flow-isolated-event") {
      isolatedBridgeReady = true;
      if (["downloadCompleted", "downloadFailed"].includes(event.data.event)) {
        const taskInfo = event.data.data?.task;
        console.log("[FlowUI] 收到后台下载完成广播通知:", taskInfo);
        if (taskInfo && taskInfo.projectId === getStorageKey()) {
          const r = taskInfo.rowIndex;
          const l = taskInfo.lineIndex;
          const tBinding = state.tasks[r]?.mediaBindings?.[l];
          if (state.tasks[r]?.taskId === taskInfo.taskId && tBinding?.submissionId === taskInfo.submissionId) {
            if (event.data.event === "downloadFailed") {
              tBinding.downloadInitiated = false;
              if (taskInfo.mediaId) inFlightOrCompletedDownloads.delete(`media:${taskInfo.mediaId}`);
              if (taskInfo.submissionId) inFlightOrCompletedDownloads.delete(`sub:${taskInfo.submissionId}`);
              tBinding.downloadFailCount = (tBinding.downloadFailCount || 0) + 1;
              if (state.tasks[r].downloadIds) state.tasks[r].downloadIds[l] = null;
              if (tBinding.downloadFailCount < 3 && state.isRunning) {
                console.warn(`[FlowUI] ⚠️ 下载中断 (${event.data.data.error})，准备自动刷新签名直链重试 (${tBinding.downloadFailCount}/3)...`);
                if (tBinding.urls && taskInfo.mediaId) delete tBinding.urls[taskInfo.mediaId];
                updateLineStatus(r, l, "generating", `下载受阻 (${event.data.data.error})，正在刷新视频直链自动重试 (${tBinding.downloadFailCount}/3)...`);
                saveState();
                return;
              }
              updateLineStatus(r, l, "failed", `下载失败: ${event.data.data.error}`);
              return;
            }
            tBinding.downloadInitiated = true;
            if (taskInfo.mediaId) inFlightOrCompletedDownloads.add(`media:${taskInfo.mediaId}`);
            if (taskInfo.submissionId) inFlightOrCompletedDownloads.add(`sub:${taskInfo.submissionId}`);
            if (state.tasks[r].downloadIds) state.tasks[r].downloadIds[l] = null;
            if (!Array.isArray(state.tasks[r].downloaded)) {
              state.tasks[r].downloaded = [];
            }
            state.tasks[r].downloaded[l] = true;
            updateLineStatus(r, l, "success", `自动下载成功: ${taskInfo.download_path}`);
            saveState();
          }
        }
      }
      return;
    }

    if (event.data.sender !== "flow-isolated") {
      return;
    }
    isolatedBridgeReady = true;
    const { callbackId, response } = event.data;
    ackedCallbacks.delete(callbackId);
    if (extensionCallbacks.has(callbackId)) {
      extensionCallbacks.get(callbackId)(response);
      extensionCallbacks.delete(callbackId);
    }
  });

  const downloadedUrlsSet = new Set();
  const discoveredVideoUrlsById = new Map();
  const inFlightOrCompletedDownloads = new Set();

  // ----------------------------------------------------
  // 2. 状态管理与配置项
  // ----------------------------------------------------
  let state = {
    tasks: [],             // 批量生成任务列表 (含 prompt, mode, image_name, local_image_path, duration, download_path, status, message, downloaded)
    taskRecord: {},        // mediaName -> 任务进度关联信息映射表
    activeTasksQueue: [],  // FIFO 活跃任务排队队列，用于与完成的视频松散匹配
    downloadedUrls: [],    // 已下载视频 URL 记录，防止重复下载
    settings: {
      interval: 5,         // 每次生成的间隔基数(秒，默认 5 秒，遵循软件规则设置)
      intervalRandom: 0,   // 随机抖动上限(秒，默认 0 秒，严格按设定间隔执行)
      maxLetterFor4s: 40,  // 4s视频最大字数
      maxLetterFor6s: 60,  // 6s视频最大字数
      defaultCount: 1,     // 默认生成数量
      footageType: "VIDEO_FRAMES", // 默认素材类型 (VIDEO_FRAMES 帧模式)
      resolution: "VIDEO_RESOLUTION_720P", // 默认分辨率 (720p)
      aspectRatio: "ASPECT_RATIO_9_16",    // 尺寸比例 (默认 9:16 竖屏)
      videoModel: "omni_1_1_flash"       // 生成模型 (默认 Omni 1.1 Flash)
    },
    presets: [             // 提示词模板预设 (首尾拼接)
      { name: "无模版(直接填入)", content: "\n\n" },
      { name: "写实风格电影感", content: "A cinematic film of\n\n4k resolution, highly detailed, photorealistic." },
      { name: "3D卡通动画", content: "A 3D animated scene of\n\npixar style, vibrant colors, clay model." },
      { name: "极简插画", content: "A minimalist vector illustration of\n\nflat color, clean lines, modern design." }
    ],
    isRunning: false,
    isSuspended: false,
    activeProjectId: "",
    currentIndex: 0,       // 当前正在跑的任务索引
    currentLineIndex: 0,   // 当前正在跑的提示词分行索引
    retryCount: 0          // 单个任务失败刷新重试计数
  };

  // 检测当前是否为 Google Vids 页面
  function isVidsPage() {
    return location.hostname === "docs.google.com" && location.pathname.includes("/videos/");
  }

  // 生成缓存 KEY 规则：工程ID + 合集ID (或 Vids 文档 ID)
  function getStorageKey() {
    const url = location.href;
    if (isVidsPage()) {
      const vidsMatch = url.match(/\/videos\/d\/([^/]+)/);
      return vidsMatch ? `vids_${vidsMatch[1]}` : "default-vids-project";
    }
    const projectMatch = url.match(/\/project\/([^/]+)/);
    const collectionMatch = url.match(/\/collection\/([^/]+)/);
    const pId = projectMatch ? projectMatch[1] : "";
    const cId = collectionMatch ? collectionMatch[1] : "";
    return [pId, cId].filter(Boolean).join("-") || "default-flow-project";
  }

  // 写入配置和状态到 Storage (通过插件 Background 存储)
  async function saveState() {
    const key = getStorageKey();
    state.activeProjectId = key;
    
    const dataToSave = {
      [`tasks_${key}`]: state.tasks,
      settings: state.settings,
      presets: state.presets,
      [`taskRecord_${key}`]: state.taskRecord,
      [`downloadedUrls_${key}`]: Array.from(downloadedUrlsSet),
      [`isRunning_${key}`]: state.isRunning,
      [`currentIndex_${key}`]: state.currentIndex,
      [`currentLineIndex_${key}`]: state.currentLineIndex,
      [`retryCount_${key}`]: state.retryCount
    };
    
    const res = await sendToExtension("setStorage", { data: dataToSave });
    if (res && res.error) {
      console.error("[FlowUI] 保存状态失败:", res.error);
      if (res.error.includes("失效") || res.error.includes("invalidated")) {
        showToast("检测到插件连接失效，请刷新网页以重新连接！", 8000);
      }
    }
  }

  // 从 Storage 读取配置和状态
  async function loadState() {
    const key = getStorageKey();
    state.activeProjectId = key;
    
    const saved = await sendToExtension("getStorage", {
      keys: [
        `tasks_${key}`, "settings", "presets", `taskRecord_${key}`, `downloadedUrls_${key}`,
        `isRunning_${key}`, `currentIndex_${key}`, `currentLineIndex_${key}`, `retryCount_${key}`
      ]
    });
    
    if (saved) {
      if (saved.error) {
        console.error("[FlowUI] 加载状态失败:", saved.error);
        if (saved.error.includes("失效") || saved.error.includes("invalidated")) {
          showToast("检测到插件连接失效或重启，请刷新网页以重新连接！", 8000);
        }
        return;
      }
      // 关键保护：当批量队列正在内存中运行时，严禁用 Storage 快照覆盖内存中的 state.tasks 引用，否则会导致 runBatch 持有旧对象引用而丢失下载完成状态！
      const isLiveRunning = state.isRunning && Array.isArray(state.tasks) && state.tasks.length > 0;
      if (!isLiveRunning && saved[`tasks_${key}`]) state.tasks = saved[`tasks_${key}`];
      if (saved.settings) state.settings = { ...state.settings, ...saved.settings };
      // 迁移: 旧默认值 VIDEO_REFERENCES / veo_fast 自动升级为标准模型 ID
      if (state.settings.videoModel === "VIDEO_REFERENCES" || !state.settings.videoModel) {
        state.settings.videoModel = "omni_1_1_flash";
      } else if (state.settings.videoModel === "veo_fast") {
        state.settings.videoModel = "veo_3_1_fast";
      }
      if (saved.presets) state.presets = saved.presets;
      if (!isLiveRunning && saved[`taskRecord_${key}`]) state.taskRecord = saved[`taskRecord_${key}`];
      if (saved[`downloadedUrls_${key}`] && Array.isArray(saved[`downloadedUrls_${key}`])) {
        state.downloadedUrls = saved[`downloadedUrls_${key}`];
        saved[`downloadedUrls_${key}`].forEach(u => downloadedUrlsSet.add(u));
      }
      if (!isLiveRunning) {
        if (saved[`isRunning_${key}`] !== undefined) state.isRunning = saved[`isRunning_${key}`];
        if (saved[`currentIndex_${key}`] !== undefined) state.currentIndex = saved[`currentIndex_${key}`];
        if (saved[`currentLineIndex_${key}`] !== undefined) state.currentLineIndex = saved[`currentLineIndex_${key}`];
        if (saved[`retryCount_${key}`] !== undefined) state.retryCount = saved[`retryCount_${key}`];
      }
    }
  }

  // ----------------------------------------------------
  // 3. UI 界面构建
  // ----------------------------------------------------
  let fabEl = null;
  let overlayEl = null;
  let importDialogEl = null;
  let alarmDialogEl = null;

  function createUI() {
    const targetParent = document.body || document.documentElement;
    let existingFab = document.getElementById("flow-batch-fab");
    if (existingFab && (!existingFab.querySelector("#flow-batch-fab-quick") || !existingFab.querySelector("#flow-batch-fab-vids"))) {
      // 旧版单/双按钮（或独立世界兜底按钮）→ 替换为三按钮胶囊框 (⚡ + 🎬 Vids + ➕)
      existingFab.remove();
      existingFab = null;
    }
    const existingOverlay = document.getElementById("flow-batch-overlay");
    if (existingFab && existingOverlay && targetParent && targetParent.contains(existingFab)) {
      return;
    }

    console.log("[FlowUI] 正在创建控制台 UI 界面...", location.href);

    if (!existingFab) {
      // 三按钮悬浮胶囊框：上 ⚡ 一键导入剪贴板并开始；中 🎬 Google Vids 逐个生成模式；下 + 打开控制面板
      // 在 Vids 页面上，右侧是 AI 视频片段侧边栏，因此自动将悬浮胶囊框置于页面左侧，绝不遮挡右侧生成栏！
      const horizontalPos = isVidsPage() ? "left: 24px !important;" : "right: 24px !important;";
      fabEl = document.createElement("div");
      fabEl.id = "flow-batch-fab";
      fabEl.style.cssText = `position: fixed !important; bottom: 120px !important; ${horizontalPos} width: 56px !important; height: 168px !important; border-radius: 28px !important; background: linear-gradient(180deg, #8b5cf6, #3b82f6) !important; box-shadow: 0 8px 32px rgba(139, 92, 246, 0.6), inset 0 2px 4px rgba(255, 255, 255, 0.3) !important; display: flex !important; flex-direction: column !important; align-items: stretch !important; justify-content: stretch !important; cursor: default !important; z-index: 2147483647 !important; border: 2px solid rgba(255, 255, 255, 0.2) !important; box-sizing: border-box !important; overflow: hidden !important; padding: 0 !important; transform: none !important;`;

      const makeFabBtn = (title, pathD, color, viewBox) => {
        const btn = document.createElement("div");
        btn.title = title;
        btn.style.cssText = "flex: 1 1 33.33% !important; display: flex !important; align-items: center !important; justify-content: center !important; cursor: pointer !important; transition: background 0.15s ease !important; background: transparent !important;";
        btn.addEventListener("mouseenter", () => btn.style.setProperty("background", "rgba(255,255,255,0.18)", "important"));
        btn.addEventListener("mouseleave", () => btn.style.setProperty("background", "transparent", "important"));
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", viewBox || "0 0 24 24");
        svg.setAttribute("width", "24");
        svg.setAttribute("height", "24");
        svg.style.cssText = `width: 24px !important; height: 24px !important; fill: ${color} !important; pointer-events: none !important; transform: none !important;`;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", pathD);
        path.setAttribute("fill", color);
        svg.appendChild(path);
        btn.appendChild(svg);
        return btn;
      };

      const makeDivider = () => {
        const div = document.createElement("div");
        div.style.cssText = "height: 1px !important; margin: 0 10px !important; background: rgba(255,255,255,0.35) !important; flex: 0 0 1px !important;";
        return div;
      };

      const quickBtn = makeFabBtn("⚡ 一键执行：读取剪贴板 JSON → 导入 → 开始批量生成",
        "M13 2L4.5 13.5H11L10 22L19.5 10.5H13L13 2Z", "#FDE047");
      quickBtn.id = "flow-batch-fab-quick";
      quickBtn.addEventListener("click", (e) => { e.stopPropagation(); quickRunFromClipboard(); });

      const vidsBtn = makeFabBtn("🎬 Vids 专属一键生成：自动打开 AI 视频侧边栏 → 切换「添加动画」→ 逐个任务上传图片、生成并下载视频 (无 50 积分限制)",
        "M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4zm-6.75 11.25L10 18l-1.25-2.75L6 14l2.75-1.25L10 10l1.25 2.75L14 14l-2.75 1.25zm5.69-3.31L16 14l-.94-2.06L13 11l2.06-.94L16 8l.94 2.06L19 11l-2.06.94z", "#67E8F9");
      vidsBtn.id = "flow-batch-fab-vids";
      vidsBtn.addEventListener("click", (e) => { e.stopPropagation(); vidsQuickRunFromClipboard(); });

      const openBtn = makeFabBtn("打开批量生成助手控制面板", "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z", "#ffffff");
      openBtn.id = "flow-batch-fab-open";
      openBtn.addEventListener("click", (e) => { e.stopPropagation(); openPanel(); });

      fabEl.appendChild(quickBtn);
      fabEl.appendChild(makeDivider());
      fabEl.appendChild(vidsBtn);
      fabEl.appendChild(makeDivider());
      fabEl.appendChild(openBtn);

      if (targetParent) {
        targetParent.appendChild(fabEl);
        console.log("[FlowUI] FAB 三按钮已挂载至:", targetParent.tagName);
      }
    } else {
      fabEl = existingFab;
      if (targetParent && !targetParent.contains(fabEl)) {
        targetParent.appendChild(fabEl);
      }
    }

    if (document.getElementById("flow-batch-overlay")) return;

    // 创建控制面板遮罩层 (使用 setSafeHTML 安全注入)
    overlayEl = document.createElement("div");
    overlayEl.id = "flow-batch-overlay";
    const panelTitleText = isVidsPage()
      ? "FlowCue · Google Vids 逐个生成与自动下载助手 (无 50 积分限制)"
      : "Flow 批量生成与自动下载助手";
    setSafeHTML(overlayEl, `
      <div id="flow-batch-panel">
        <div id="flow-batch-panel-header">
          <h2>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #a78bfa;"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path><polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline><line x1="12" y1="22.08" x2="12" y2="12"></line></svg>
            ${panelTitleText}
          </h2>
          <div class="flow-close-btn" id="flow-panel-close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
          </div>
        </div>
        
        <div id="flow-batch-panel-content">
          <!-- 左侧：任务表格 -->
          <div id="flow-table-container">
            <table class="flow-table" id="flow-tasks-table">
              <thead>
                <tr>
                  <th style="width: 145px; text-align: center;">任务序号 / 项目</th>
                  <th style="width: 115px; text-align: center;">图片</th>
                  <th>提示词文本 (分行分割)</th>
                  <th style="width: 135px;">提示词模板</th>
                  <th style="width: 125px;">素材类型</th>
                  <th style="width: 80px;">时长（秒）</th>
                  <th style="width: 65px;">生成数</th>
                  <th style="width: 220px;">状态与日志</th>
                  <th style="width: 155px; text-align: center;">操作</th>
                </tr>
              </thead>
              <tbody>
                <!-- 动态渲染 -->
              </tbody>
            </table>
          </div>

          <!-- 右侧：设置面板 -->
          <div id="flow-settings-drawer">
            <h3 style="margin-top: 0; font-family: var(--flow-font-heading); font-size: 16px; border-bottom: 1px solid var(--flow-border-glass); padding-bottom: 10px;">自动化参数配置</h3>
            
            <div class="settings-group">
              <label>生成间隔基数 (秒)</label>
              <input type="number" class="settings-input" id="cfg-interval" min="0" value="5">
            </div>
            
            <div class="settings-group">
              <label>随机抖动时间 (秒)</label>
              <input type="number" class="settings-input" id="cfg-random" min="0" value="0">
            </div>
            
            <div class="settings-group">
              <label>4秒视频最大字数限额</label>
              <input type="number" class="settings-input" id="cfg-max4s" min="1" value="40">
            </div>
            
            <div class="settings-group">
              <label>6秒视频最大字数限额</label>
              <input type="number" class="settings-input" id="cfg-max6s" min="1" value="60">
            </div>

            <div class="settings-group">
              <label>画面尺寸 / 比例</label>
              <select class="settings-input" id="cfg-aspect" style="background: rgba(255,255,255,0.05); color: var(--flow-text-primary);">
                <option value="ASPECT_RATIO_9_16" selected>9:16 (竖屏 / 手机短视频 默认)</option>
                <option value="ASPECT_RATIO_16_9">16:9 (横屏 / 宽屏)</option>
                <option value="ASPECT_RATIO_1_1">1:1 (正方形)</option>
              </select>
            </div>

            <div class="settings-group">
              <label>视频生成模型</label>
              <select class="settings-input" id="cfg-model" style="background: rgba(255,255,255,0.05); color: var(--flow-text-primary);">
                <option value="omni_1_1_flash" selected>Omni 1.1 Flash (默认)</option>
                <option value="veo_3_1_lite">Veo 3.1 - Lite</option>
                <option value="veo_3_1_fast">Veo 3.1 - Fast</option>
                <option value="veo_3_1_quality">Veo 3.1 - Quality</option>
                <option value="veo_3_1_lite_low_priority">Veo 3.1 - Lite [Lower Priority]</option>
                <option value="veo_2">Veo 2</option>
              </select>
            </div>

            <div class="settings-group">
              <label>清晰度 / 分辨率</label>
              <select class="settings-input" id="cfg-resolution" style="background: rgba(255,255,255,0.05); color: var(--flow-text-primary);">
                <option value="VIDEO_RESOLUTION_720P" selected>720p (高清 默认)</option>
                <option value="VIDEO_RESOLUTION_360P">360p (低分辨率 / 省流量)</option>
                <option value="VIDEO_RESOLUTION_1080P">1080p (全高清)</option>
              </select>
            </div>

            <div class="settings-group">
              <label>默认素材模式</label>
              <select class="settings-input" id="cfg-footagetype" style="background: rgba(255,255,255,0.05); color: var(--flow-text-primary);">
                <option value="VIDEO_FRAMES" selected>帧模式 (首帧作为起始视频帧 默认)</option>
                <option value="VIDEO_REFERENCES">素材模式 (图片作为参考物)</option>
              </select>
            </div>

            <div class="settings-group" style="margin-top: 20px; border-top: 1px solid var(--flow-border-glass); padding-top: 15px;">
              <label style="font-weight: 600;">模版自定义预设</label>
              <button class="flow-btn secondary" id="btn-add-preset" style="font-size: 12px; padding: 6px 12px;">新建模板</button>
              <div id="presets-list-container" style="display:flex; flex-direction:column; gap:8px; margin-top:8px; max-height: 180px; overflow-y:auto;">
                <!-- 预设列表 -->
              </div>
            </div>
          </div>
        </div>

        <div id="flow-batch-panel-footer">
          <div class="footer-actions-left">
            <button class="flow-btn secondary" id="btn-load-images">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>
              加载页面图片
            </button>
            <button class="flow-btn secondary" id="btn-open-import" style="border-color: #8b5cf6; color: #a78bfa;">
              导入外部 JSON
            </button>
            <button class="flow-btn secondary" id="btn-upload-local" style="border-color: #10b981; color: #34d399;" title="把任务中的本地图片（local_image_path）真实上传到当前 Flow 项目">
              上传本地图片到 Flow
            </button>
            <button class="flow-btn secondary" id="btn-copy-report">
              导出结果报告
            </button>
            <button class="flow-btn secondary" id="btn-clear-tasks" style="color: #ef4444; border-color: rgba(239,68,68,0.2);" title="清空表格中的全部任务">
              清空全部任务列表
            </button>
          </div>
          <div class="footer-actions-right">
            <button class="flow-btn danger" id="btn-stop-batch" style="display: none !important;">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"></rect></svg>
              停止生成
            </button>
            <button class="flow-btn primary" id="btn-start-batch">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
              开始批量生成
            </button>
          </div>
        </div>
      </div>
    `);
    targetParent.appendChild(overlayEl);

    // 创建粘贴导入弹窗
    importDialogEl = document.createElement("div");
    importDialogEl.id = "flow-import-dialog";
    setSafeHTML(importDialogEl, `
      <div class="import-box">
        <h3 class="import-title">
          导入外部数据配置 (多套 JSON)
          <span style="font-size:11px; font-weight:normal; color:var(--flow-text-secondary);">支持多任务数组一键导入</span>
        </h3>
        <textarea class="import-textarea" id="flow-import-textarea" placeholder='请在此处粘贴 Python 导出的 JSON 数组配置...\n例如:\n[\n  {\n    "prompt": "Un perro corriendo",\n    "mode": "VIDEO_FRAMES",\n    "image_name": "dog.png",\n    "duration": 6,\n    "download_path": "D:\\\\videos\\\\project\\\\dog.mp4"\n  }\n]'></textarea>
        <div class="import-actions">
          <button class="flow-btn secondary" id="btn-import-cancel">取消</button>
          <button class="flow-btn primary" id="btn-import-confirm">确认导入</button>
        </div>
      </div>
    `);
    targetParent.appendChild(importDialogEl);

    // 创建异常活动报警弹窗
    alarmDialogEl = document.createElement("div");
    alarmDialogEl.id = "flow-alarm-dialog";
    alarmDialogEl.style.cssText = "display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(220, 38, 38, 0.15); backdrop-filter: blur(12px); z-index: 2147483646 !important; align-items: center; justify-content: center;";
    setSafeHTML(alarmDialogEl, `
      <div class="import-box" style="border-color: rgba(220, 38, 38, 0.4); box-shadow: 0 20px 48px rgba(220, 38, 38, 0.25);">
        <h3 class="import-title" style="color: #ef4444; display: flex; align-items: center; gap: 8px;">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
          🚨 检测到安全限制（异常活动）
        </h3>
        <p style="font-size: 14px; line-height: 1.6; color: var(--flow-text-primary); margin: 8px 0;">
          Google Flow 网页提示“我们发现了一些异常活动”安全拦截。
        </p>
        <p id="flow-alarm-message" style="font-size: 13px; color: #f59e0b; font-weight: 500; margin: 4px 0;">
          系统将在 45 秒后自动刷新网页以尝试恢复，您也可以立即进行手动处理...
        </p>
        <div class="import-actions" style="margin-top: 16px;">
          <button class="flow-btn secondary" id="btn-alarm-manual" style="border-color: rgba(245, 158, 11, 0.3); color: #f59e0b;">我来手动处理 (暂停倒计时)</button>
          <button class="flow-btn danger" id="btn-alarm-reload">立即刷新网页</button>
          <button class="flow-btn primary" id="btn-alarm-resume">消除限制，继续生成</button>
        </div>
      </div>
    `);
    targetParent.appendChild(alarmDialogEl);

    // 创建 Toast 元素
    const toast = document.createElement("div");
    toast.id = "flow-batch-toast";
    toast.className = "flow-toast";
    targetParent.appendChild(toast);

    // 安全绑定基础 UI 事件
    const safeOn = (id, event, handler) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener(event, handler);
      else console.warn(`[FlowUI] 挂载未找到元素 #${id}`);
    };

    safeOn("flow-panel-close", "click", () => closePanel());
    if (overlayEl) {
      overlayEl.addEventListener("click", (e) => {
        if (e.target === overlayEl) closePanel();
      });
    }

    // 绑定数据导入/导出/加载按钮
    safeOn("btn-load-images", "click", () => loadPageImages());
    safeOn("btn-open-import", "click", () => openImportDialog());
    safeOn("btn-import-cancel", "click", () => closeImportDialog());
    safeOn("btn-import-confirm", "click", () => handleJSONImport());
    safeOn("btn-copy-report", "click", () => copyExecutionReport());
    safeOn("btn-upload-local", "click", () => {
      if (batchPromise) { showToast("请先停止队列，再手动上传图片"); return; }
      uploadLocalImagesToFlow({ respectRegistry: false });
    });
    
    safeOn("btn-alarm-manual", "click", () => pauseAlarmCountdown());
    safeOn("btn-alarm-reload", "click", () => {
      if (alarmTimer) clearInterval(alarmTimer);
      saveState().then(() => location.reload());
    });
    safeOn("btn-alarm-resume", "click", () => resumeFromAlarm());
    
    safeOn("btn-clear-tasks", "click", () => {
      if (batchPromise) { showToast("请先停止队列，再清空任务列表"); return; }
      if (confirm("确定清空插件中的全部任务列表吗？Flow 云端项目和已下载文件不受影响。")) {
        state.tasks = [];
        state.taskRecord = {};
        state.activeTasksQueue = [];
        state.currentIndex = 0;
        state.currentLineIndex = 0;
        state.retryCount = 0;
        renderTable();
        saveState();
        showToast("已清空全部任务列表");
      }
    });

    overlayEl.addEventListener("click", event => {
      if (!batchPromise) return;
      const target = event.target.closest("button, input, select, textarea");
      if (!target) return;
      if (target.closest("#flow-tasks-table") || ["btn-open-import", "btn-load-images", "btn-clear-tasks"].includes(target.id)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        showToast("请先停止队列，再修改任务");
      }
    }, true);
    safeOn("btn-start-batch", "click", () => startBatch());
    safeOn("btn-stop-batch", "click", () => stopBatch());

    bindSettingsEvents();
  }

  function showToast(message, duration = 3000) {
    let el = document.getElementById("flow-batch-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "flow-batch-toast";
      el.className = "flow-toast";
      (document.body || document.documentElement).appendChild(el);
    }
    el.innerText = message;
    el.style.cssText = "position: fixed !important; bottom: 24px !important; left: 24px !important; background: rgba(18, 18, 22, 0.95) !important; backdrop-filter: blur(8px) !important; border: 1px solid rgba(255, 255, 255, 0.15) !important; padding: 12px 24px !important; border-radius: 12px !important; color: #f3f4f6 !important; box-shadow: 0 8px 32px rgba(0,0,0,0.5) !important; font-size: 14px !important; z-index: 2147483647 !important; transform: translateY(0) !important; opacity: 1 !important; pointer-events: none !important; display: block !important;";
    el.classList.add("show");
    setTimeout(() => {
      el.classList.remove("show");
      el.style.setProperty("display", "none", "important");
    }, duration);
  }

  async function openPanel() {
    console.log("[FlowUI] 正在呼出主控制面板...");
    if (!overlayEl) overlayEl = document.getElementById("flow-batch-overlay");
    if (!overlayEl) {
      console.warn("[FlowUI] 遮罩层丢失，正在重新创建 UI...");
      createUI();
      overlayEl = document.getElementById("flow-batch-overlay");
    }

    if (overlayEl) {
      overlayEl.style.cssText = "position: fixed !important; top: 0 !important; left: 0 !important; width: 100vw !important; height: 100vh !important; background: rgba(0, 0, 0, 0.75) !important; backdrop-filter: blur(12px) !important; z-index: 2147483640 !important; display: flex !important; align-items: center !important; justify-content: center !important; opacity: 1 !important; pointer-events: auto !important;";
      overlayEl.classList.add("active");
    }

    const panel = document.getElementById("flow-batch-panel");
    if (panel) {
      panel.style.cssText = "width: 92vw !important; height: 88vh !important; max-width: 1400px !important; background: #121216 !important; border: 1px solid rgba(255, 255, 255, 0.15) !important; border-radius: 20px !important; box-shadow: 0 24px 64px rgba(0, 0, 0, 0.8) !important; display: flex !important; flex-direction: column !important; color: #f3f4f6 !important; overflow: hidden !important; z-index: 2147483641 !important; pointer-events: auto !important;";
    }

    try {
      if (!state.isRunning || !Array.isArray(state.tasks) || state.tasks.length === 0) {
        await loadState();
      }
    } catch (e) {
      console.warn("[FlowUI] 读取状态异常:", e);
    }
    
    // 安全同步设置项输入框
    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el && val !== undefined) el.value = val;
    };
    setVal("cfg-interval", state.settings.interval);
    setVal("cfg-random", state.settings.intervalRandom);
    setVal("cfg-max4s", state.settings.maxLetterFor4s);
    setVal("cfg-max6s", state.settings.maxLetterFor6s);
    setVal("cfg-aspect", state.settings.aspectRatio || "ASPECT_RATIO_9_16");
    setVal("cfg-model", state.settings.videoModel || "omni_1_1_flash");
    setVal("cfg-resolution", state.settings.resolution || "VIDEO_RESOLUTION_720P");
    setVal("cfg-footagetype", state.settings.footageType || "VIDEO_FRAMES");
    
    try {
      renderTable();
    } catch (e) {
      console.warn("[FlowUI] 渲染任务表异常:", e);
    }
    try {
      renderPresetsList();
    } catch (e) {
      console.warn("[FlowUI] 渲染模板列表异常:", e);
    }
  }

  function closePanel() {
    if (!overlayEl) overlayEl = document.getElementById("flow-batch-overlay");
    if (overlayEl) {
      overlayEl.style.cssText = "display: none !important; opacity: 0 !important; pointer-events: none !important;";
      overlayEl.classList.remove("active");
    }
  }

  // ⚡ 一键执行：剪贴板 JSON → 打开面板 → 导入外部 JSON → 粘贴 → 确认导入 → 开始批量生成
  let quickRunBusy = false;
  async function quickRunFromClipboard() {
    if (quickRunBusy) return;
    if (batchPromise) { showToast("⚡ 批量生成正在运行中，请先停止后再使用一键执行", 4000); return; }
    quickRunBusy = true;
    try {
      // 3. 先读取剪贴板（必须在点击后立即读取，否则浏览器会拒绝）
      let text = "";
      try {
        text = ((await navigator.clipboard.readText()) || "").trim();
      } catch (e) {
        console.warn("[FlowUI] 读取剪贴板失败:", e);
        showToast("⚡ 无法读取剪贴板：请在地址栏左侧允许本网站「剪贴板」权限后重试", 6000);
        return;
      }
      if (!text) { showToast("⚡ 剪贴板为空，请先在软件中点击「复制任务 JSON」", 5000); return; }
      if (!text.startsWith("[")) { showToast("⚡ 剪贴板内容不是任务 JSON（应以 [ 开头），已取消", 5000); return; }

      // 1. 打开 UI 控制页面
      await openPanel();
      await delay(500);

      // 1.5. 在导入前先清空原本的任务列表
      state.tasks = [];
      state.taskRecord = {};
      state.globalProcessedTasks = 0;
      renderTable();
      saveState();
      await delay(100);

      // 2. 点击「导入外部 JSON」
      const btnOpenImport = document.getElementById("btn-open-import");
      if (!btnOpenImport) { showToast("⚡ 未找到「导入外部 JSON」按钮"); return; }
      btnOpenImport.click();
      await delay(500);

      // 4. 粘贴到「导入外部数据配置 (多套 JSON)」
      const ta = document.getElementById("flow-import-textarea");
      if (!ta) { showToast("⚡ 未找到导入输入框"); return; }
      ta.value = text;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      await delay(400);

      // 5. 点击「确认导入」
      const before = state.tasks.length;
      const btnConfirm = document.getElementById("btn-import-confirm");
      if (!btnConfirm) { showToast("⚡ 未找到「确认导入」按钮"); return; }
      btnConfirm.click();
      await delay(800);
      const added = state.tasks.length - before;
      if (added <= 0) { showToast("⚡ 导入未成功，已停止，未开始生成", 5000); return; }

      // 6. 点击「开始批量生成」
      await delay(500);
      const btnStart = document.getElementById("btn-start-batch");
      if (!btnStart) { showToast("⚡ 未找到「开始批量生成」按钮"); return; }
      btnStart.click();
      if (isVidsPage()) {
        await delay(400);
        closePanel();
        showToast(`🎬 [Vids 模式] 已导入 ${added} 个任务，开始逐个上传图片并生成视频！`, 5000);
      } else {
        showToast(`⚡ 已导入 ${added} 个任务并开始批量生成`, 4000);
      }
    } catch (e) {
      console.error("[FlowUI] 一键执行异常:", e);
      showToast(`⚡ 一键执行出错：${e.message}`, 6000);
    } finally {
      quickRunBusy = false;
    }
  }

  // 🎬 Google Vids 专属一键生成：自动打开右侧 AI 视频片段侧边栏 → 切换「添加动画」→ 读取剪贴板 JSON（或已有任务）逐个上传图片、生成并下载
  async function vidsQuickRunFromClipboard() {
    if (quickRunBusy) return;
    if (batchPromise) {
      await openPanel();
      showToast("🎬 Vids 队列正在运行中，您可在控制面板查看进度或点击「停止生成」", 4000);
      return;
    }
    if (!isVidsPage()) {
      showToast("🎬 正在为您打开 Google Vids 页面，进入视频项目后再次点击 🎬 按钮即可开始！", 6000);
      window.open("https://docs.google.com/videos/", "_blank");
      return;
    }
    quickRunBusy = true;
    try {
      let text = "";
      try {
        text = ((await navigator.clipboard.readText()) || "").trim();
      } catch (e) {
        console.warn("[FlowUI] [Vids] 读取剪贴板提示:", e);
      }

      if (text && text.startsWith("[")) {
        let parsedList = null;
        try {
          parsedList = JSON.parse(text);
        } catch (err) {
          showToast(`🎬 剪贴板 JSON 解析失败：${err.message}`, 5000);
          return;
        }
        if (Array.isArray(parsedList) && parsedList.length > 0) {
          state.tasks = [];
          state.taskRecord = {};
          state.activeTasksQueue = [];
          state.currentIndex = 0;
          state.currentLineIndex = 0;
          state.retryCount = 0;
          const added = importTaskList(parsedList);
          try { renderTable(); } catch (_) {}
          await saveState();
          closePanel();
          showToast(`🎬 [Vids 模式] 已导入 ${added} 个任务（无 50 积分限制），正在逐个上传图片并生成视频...`, 5000);
          startBatch();
          return;
        }
      }

      // 若剪贴板不是新 JSON，但当前列表已有待执行任务，则直接启动 Vids 逐个生成
      const hasPending = Array.isArray(state.tasks) && state.tasks.some(t =>
        splitTextToLines(t.text).length > 0 && (!Array.isArray(t.status) || t.status.some(s => s !== "success"))
      );
      if (hasPending) {
        closePanel();
        showToast(`🎬 [Vids 模式] 开始执行列表中的 ${state.tasks.length} 个任务...`, 4000);
        startBatch();
        return;
      }

      await openPanel();
      openImportDialog();
      showToast("🎬 请先在 FlowCue 软件中点击「复制任务 JSON」，然后粘贴导入或再次点击 🎬 按钮", 6000);
    } catch (e) {
      console.error("[FlowUI] [Vids] 一键执行异常:", e);
      showToast(`🎬 Vids 一键执行出错：${e.message}`, 6000);
    } finally {
      quickRunBusy = false;
    }
  }

  function openImportDialog() {
    if (!importDialogEl) importDialogEl = document.getElementById("flow-import-dialog");
    if (importDialogEl) {
      importDialogEl.style.cssText = "position: fixed !important; top: 0 !important; left: 0 !important; width: 100vw !important; height: 100vh !important; background: rgba(0, 0, 0, 0.75) !important; backdrop-filter: blur(12px) !important; z-index: 2147483647 !important; display: flex !important; align-items: center !important; justify-content: center !important; opacity: 1 !important; pointer-events: auto !important;";
      importDialogEl.classList.add("active");
    }
    const txt = document.getElementById("flow-import-textarea");
    if (txt) txt.value = "";
  }

  function closeImportDialog() {
    if (!importDialogEl) importDialogEl = document.getElementById("flow-import-dialog");
    if (importDialogEl) {
      importDialogEl.style.cssText = "display: none !important; opacity: 0 !important; pointer-events: none !important;";
      importDialogEl.classList.remove("active");
    }
  }

  function bindSettingsEvents() {
    const syncSetting = (elementId, settingKey) => {
      const el = document.getElementById(elementId);
      if (el) {
        el.addEventListener("change", (e) => {
          const val = parseInt(e.target.value, 10);
          if (!Number.isNaN(val) && val >= 0) {
            state.settings[settingKey] = val;
          }
          saveState();
        });
      }
    };

    syncSetting("cfg-interval", "interval");
    syncSetting("cfg-random", "intervalRandom");
    syncSetting("cfg-max4s", "maxLetterFor4s");
    syncSetting("cfg-max6s", "maxLetterFor6s");

    const syncSelect = (elementId, settingKey) => {
      const el = document.getElementById(elementId);
      if (el) {
        el.addEventListener("change", (e) => {
          state.settings[settingKey] = e.target.value;
          saveState();
        });
      }
    };

    syncSelect("cfg-aspect", "aspectRatio");
    syncSelect("cfg-model", "videoModel");
    syncSelect("cfg-resolution", "resolution");
    syncSelect("cfg-footagetype", "footageType");

    document.getElementById("btn-add-preset").addEventListener("click", () => {
      const name = prompt("请输入模板名称:");
      if (!name) return;
      const content = prompt("请输入模板内容（在要插入提示词的位置，用两个回车/换行符隔开）");
      if (content === null) return;
      state.presets.push({ name, content });
      saveState();
      renderPresetsList();
      renderTable();
    });
  }

  function renderPresetsList() {
    const container = document.getElementById("presets-list-container");
    if (!container) return;
    container.replaceChildren();
    state.presets.forEach((preset, index) => {
      const item = document.createElement("div");
      item.style.cssText = "display:flex; justify-content:space-between; align-items:center; background:rgba(255,255,255,0.03); padding:6px 12px; border-radius:6px; border:1px solid var(--flow-border-glass);";
      setSafeHTML(item, `
        <span style="font-size:12px; font-weight:500;">${preset.name}</span>
        <button class="flow-btn secondary" style="font-size:10px; padding:4px 8px; color:#ef4444; border-color:rgba(239,68,68,0.2);" data-idx="${index}">删除</button>
      `);
      item.querySelector("button").addEventListener("click", (e) => {
        const idx = parseInt(e.target.dataset.idx, 10);
        if (confirm(`确定删除模板 "${state.presets[idx].name}" 吗？`)) {
          state.presets.splice(idx, 1);
          saveState();
          renderPresetsList();
          renderTable();
        }
      });
      container.appendChild(item);
    });
  }

  // ----------------------------------------------------
  // 4. 加载页面图片
  // ----------------------------------------------------
  // 检查是否为 Google Flow 系统按钮/热键图标文字（排除取消收藏、更多选项等噪声，兼容简中/繁中/英文）
  function isSystemHotbarLabel(text) {
    if (!text || typeof text !== "string") return true;
    const t = text.trim().toLowerCase();
    const systemSet = new Set([
      "取消收藏", "收藏", "加入最愛", "移除最愛", "取消最愛", "favorite", "unfavorite", "add to favorites", "remove from favorites",
      "更多选项", "更多選項", "更多项目选项", "更多專案選項", "more_vert", "more options", "more project options",
      "image", "play_circle", "play_arrow", "download", "redo", "delete", "search", "filter_list", "add", "home", "help", "swap_horiz",
      "显示用户图片的图块", "顯示使用者圖像的圖塊", "顯示使用者圖片的圖塊", "tile showing user image", "user image tile",
      "生成的视频缩略图", "生成的影片縮圖", "generated video thumbnail", "video thumbnail",
      "图块", "圖塊", "tile", "重复使用提示词", "重複使用提示詞", "reuse prompt"
    ]);
    return systemSet.has(t);
  }

  // 精准提取媒体图块标题名称
  function extractTileDisplayName(container, imgEl) {
    if (!container) return "";
    
    // 1. 最优先：明确的卡片标题元素（Google Flow 底部浮层文件名）
    const titleEl = typeof container.querySelector === "function" ? container.querySelector(".footer-title, .tile-name, .tile-title, input.editable-text-input, [data-tile-title]") : null;
    if (titleEl) {
      const val = titleEl.value || titleEl.textContent?.trim();
      if (val && !isSystemHotbarLabel(val)) return val;
    }

    // 2. 宿主卡片的 aria-label（Google Flow 顶层容器通常带有完整文件名）
    const card = (typeof container.closest === "function" ? container.closest("flow-grid-tile-container, [data-tile-id], flow-tile, flow-media-tile") : null) || container;
    const cardAria = card?.getAttribute?.("aria-label") || container.getAttribute?.("aria-label");
    if (cardAria && !isSystemHotbarLabel(cardAria)) {
      return cardAria.trim();
    }

    // 3. footer-left 区域（移除 mat-icon 等图标后的纯文件名）
    const footerLeft = typeof container.querySelector === "function" ? container.querySelector(".footer-left, .hover-footer, flow-tile-hover-footer") : null;
    if (footerLeft) {
      if (typeof footerLeft.cloneNode === "function") {
        const clone = footerLeft.cloneNode(true);
        if (typeof clone.querySelectorAll === "function") {
          clone.querySelectorAll("mat-icon, svg, button").forEach(el => el.remove());
        }
        const val = clone.textContent?.trim();
        if (val && !isSystemHotbarLabel(val)) return val;
      } else {
        const val = footerLeft.textContent?.trim();
        if (val && !isSystemHotbarLabel(val)) return val;
      }
    }

    // 4. 卡片的 title 属性
    const cardTitle = card?.getAttribute?.("title") || container.getAttribute?.("title");
    if (cardTitle && !isSystemHotbarLabel(cardTitle)) return cardTitle.trim();

    // 5. 图片自身属性
    if (imgEl) {
      const imgTitle = imgEl.getAttribute?.("title") || imgEl.getAttribute?.("aria-label");
      if (imgTitle && !isSystemHotbarLabel(imgTitle)) return imgTitle.trim();
      if (imgEl.alt && !isSystemHotbarLabel(imgEl.alt)) {
        return imgEl.alt.trim();
      }
    }

    return "";
  }

  // 深度归一化图名辅助器
  function normalizeNameHelper(name) {
    if (!name || typeof name !== "string") return "";
    return name
      .toLowerCase()
      // 全角转半角 (如 （1） 转为 (1) )
      .replace(/[\uff01-\uff5e]/g, ch => String.fromCharCode(ch.charCodeAt(0) - 0xfee0))
      // 去除文件扩展名
      .replace(/\.(jpeg|jpg|png|webp|gif|bmp|mp4|mov)$/i, "")
      // 统一符号为空格
      .replace(/[()[\]{}（）【】_.\-\s]+/g, " ")
      .trim();
  }

  function cleanCompactName(name) {
    return normalizeNameHelper(name).replace(/\s+/g, "");
  }

  function extractSequenceIndex(name) {
    if (!name) return null;
    const norm = String(name).replace(/[\uff01-\uff5e]/g, ch => String.fromCharCode(ch.charCodeAt(0) - 0xfee0));
    const m = norm.match(/(?:[(_\-\s]|^)(\d{1,3})(?:[)_\-\s]|\.\w+$|$)/);
    return m ? parseInt(m[1], 10) : null;
  }

  // 智能图名比对判定器（完全支持各种日期、格式、括号、序号、前后缀差异）
  function isImageNameMatch(targetName, pageName) {
    if (!targetName || !pageName) return false;

    const targetClean = cleanCompactName(targetName);
    const pageClean = cleanCompactName(pageName);

    if (!targetClean || !pageClean) return false;

    // 1. 紧凑形式完全一致
    if (targetClean === pageClean) return true;

    // 2. 检查序号一致性：若两者都有序号，但序号不相等，则严禁匹配！（例如 (3) 绝对不匹配 (6)）
    const targetSeq = extractSequenceIndex(targetName);
    const pageSeq = extractSequenceIndex(pageName);
    if (targetSeq !== null && pageSeq !== null && targetSeq !== pageSeq) {
      return false;
    }

    // 3. 包含匹配（且序号必须一致）
    if (targetClean.includes(pageClean) || pageClean.includes(targetClean)) {
      if (targetSeq !== null && pageSeq !== null) {
        return targetSeq === pageSeq;
      }
      return true;
    }

    // 4. 标准分词重叠匹配（去掉纯数字后关键词有交集）
    const targetNorm = normalizeNameHelper(targetName);
    const pageNorm = normalizeNameHelper(pageName);
    const targetWords = targetNorm.split(" ").filter(w => w.length >= 2 && !/^\d+$/.test(w));
    const pageWords = pageNorm.split(" ").filter(w => w.length >= 2 && !/^\d+$/.test(w));

    if (targetWords.length > 0 && pageWords.length > 0) {
      const hasCommon = targetWords.some(tw => pageWords.some(pw => tw.includes(pw) || pw.includes(tw)));
      if (hasCommon) {
        if (targetSeq !== null && pageSeq !== null) {
          return targetSeq === pageSeq;
        }
        return true;
      }
    }

    return false;
  }

  // 获取当前 Flow 合集/项目页面的所有已上传素材列表
  function getLoadedPageImages() {
    const images = [];
    const seenIds = new Set();

    // 1. 优先从所有图块容器中扫描（排除视频图块 flow-video-tile）
    const candidateContainers = document.querySelectorAll(
      "flow-grid-tile-container, " +
      "flow-image-tile, " +
      "flow-tile, " +
      "flow-media-tile, " +
      ".virtual-item-container:has(img), " +
      "[data-tile-id]"
    );

    const isIgnoredImg = (img, src) => {
      if (!src || src.startsWith("data:image/svg") || src.includes(".svg") || src.includes("googlelogo") || src.includes("avatar") || src.includes("favicon") || src.includes("gb_")) return true;
      if (/googleusercontent\.com\/a[-/]/i.test(src) || /lh\d+\.google\.com\/u\//i.test(src)) return true;
      if (img.closest("header, nav, flow-header, flow-header-user-icon, .header-user-button, .gb_X, .gbii, .cdk-overlay-container, #flow-batch-overlay, #flow-import-dialog")) return true;
      return false;
    };

    candidateContainers.forEach((container, idx) => {
      if (container.tagName === "FLOW-VIDEO-TILE" || container.querySelector?.("flow-video-tile") || container.closest?.("flow-video-tile")) return;
      const img = container.querySelector("img");
      if (!img || img.classList.contains("thumbnail")) return;
      const src = img.src || img.getAttribute("src") || "";
      if (isIgnoredImg(img, src)) return;

      const displayName = extractTileDisplayName(container, img) || `素材图片_${idx + 1}`;
      const mediaId = img.getAttribute("data-media-id") || container.dataset?.tileId || container.closest("[data-tile-id]")?.dataset?.tileId;
      const nameMatch = src.match(/name=([0-9a-zA-Z_-]+)/) || src.match(/\/([0-9a-fA-F-]{36})/) || src.match(/key=([^&]+)/);
      const uniqueId = mediaId || (nameMatch ? nameMatch[1] : `img_${idx}_${src.substring(src.length - 16)}`);

      if (!seenIds.has(uniqueId)) {
        seenIds.add(uniqueId);
        images.push({
          id: uniqueId,
          primaryMediaKey: uniqueId,
          displayName: displayName,
          src: src
        });
      }
    });

    // 2. 降级兜底：扫描素材视口内的 img 元素（排除顶栏头像与小图标）
    if (!images.length) {
      document.querySelectorAll("cdk-virtual-scroll-viewport img, .tiles-container img, .virtual-scroll-container img").forEach((img, idx) => {
        if (img.classList.contains("thumbnail") || img.closest("flow-video-tile")) return;
        const src = img.src || "";
        if (isIgnoredImg(img, src)) return;
        const r = img.getBoundingClientRect();
        if (r.width <= 60 || r.height <= 60) return;
        const card = img.closest("flow-grid-tile-container, [data-tile-id], mat-card, div") || img.parentElement;
        const displayName = extractTileDisplayName(card, img) || `页面素材_${idx + 1}`;
        const nameMatch = src.match(/name=([0-9a-zA-Z_-]+)/) || src.match(/\/([0-9a-fA-F-]{36})/) || src.match(/key=([^&]+)/);
        const uniqueId = nameMatch ? nameMatch[1] : `img_fallback_${idx}_${src.substring(src.length - 16)}`;
        if (!seenIds.has(uniqueId)) {
          seenIds.add(uniqueId);
          images.push({
            id: uniqueId,
            primaryMediaKey: uniqueId,
            displayName: displayName,
            src: src
          });
        }
      });
    }

    console.log(`[FlowUI] 读取到页面素材图片共 ${images.length} 张:`, images);
    return images;
  }

  function loadPageImages() {
    const images = getLoadedPageImages();
    if (!images.length) {
      showToast("页面暂未识别到候选素材图片。请确认项目已有素材，或直接点击“导入外部配置”粘贴任务。");
      return;
    }

    // 对当前任务列表中未关联到缩略图的任务进行自动匹配补齐
    let autoResolvedCount = 0;
    state.tasks.forEach(task => {
      if (!task.image?.src) {
        const taskName = task.image?.displayName || "";
        const localName = getFilenameFromPath(task.image?.localImagePath || task.local_image_path);
        const matched = images.find(img => 
          (taskName && isImageNameMatch(taskName, img.displayName)) || 
          (localName && isImageNameMatch(localName, img.displayName))
        );
        if (matched) {
          task.image = {
            ...matched,
            src: matched.src,
            displayName: task.image?.displayName || matched.displayName,
            localImagePath: task.image?.localImagePath || task.local_image_path
          };
          autoResolvedCount++;
        }
      }
    });

    const addedImageIds = new Set(state.tasks.map(t => t.image?.id).filter(Boolean));
    const newTasks = [];

    images.forEach(img => {
      if (!addedImageIds.has(img.id)) {
        newTasks.push({
          image: img,
          text: "",
          preset: state.presets[0]?.name || "无模版(直接填入)",
          footageType: state.settings.footageType,
          count: state.settings.defaultCount,
          status: [],
          message: [],
          downloaded: [],
          download_path: ""
        });
      }
    });

    if (newTasks.length > 0 || autoResolvedCount > 0) {
      if (newTasks.length > 0) state.tasks = [...state.tasks, ...newTasks];
      renderTable();
      saveState();
      showToast(`加载成功！新增 ${newTasks.length} 个任务，自动补全 ${autoResolvedCount} 个已有任务图块`);
    } else {
      showToast(`页面识别到 ${images.length} 张图片，任务列表已为最新`);
    }
  }

  // ----------------------------------------------------
  // 4.5 本地图片 → Flow 原生拖放上传（方案移植自 FlowStudio 插件）
  //   网页脚本伪造的 DragEvent/File 都是 isTrusted=false，Flow 不会接收；
  //   这里改由后台 chrome.debugger 发送 Input.dispatchDragEvent，
  //   把 JSON 中的 local_image_path 绝对路径作为「真实文件」拖入 Flow 工作区。
  // ----------------------------------------------------
  const UPLOAD_BATCH_SIZE = 10;
  const UPLOAD_CONFIRM_TIMEOUT_MS = 120000;
  let uploadInFlight = false;

  // 严格比对：避免模糊关键词匹配把「同人物不同编号」的图误判为已上传
  function isStrictImageMatch(targetName, pageName) {
    const a = cleanCompactName(targetName), b = cleanCompactName(pageName);
    if (!a || !b) return false;
    if (a === b) return true;
    // Flow 卡片名称可能被截断：较长的一方以较短一方开头，且较短一方足够长
    const [s, l] = a.length <= b.length ? [a, b] : [b, a];
    if (!(s.length >= 12 && l.startsWith(s))) return false;
    // 同一项目的图片名前缀几乎相同（只差结尾编号），结尾编号必须一致，否则会把 01 误判成 02
    const tailA = (a.match(/(\d+)$/) || [])[1] || "";
    const tailB = (b.match(/(\d+)$/) || [])[1] || "";
    return !!tailA && tailA === tailB;
  }

  function getTaskUploadName(task) {
    return getFilenameFromPath(task.image?.localImagePath || task.local_image_path || "") || task.image?.displayName || "";
  }

  function findStrictPageImage(task, pageImages) {
    const names = [getTaskUploadName(task), task.image?.displayName].filter(Boolean);
    return pageImages.find(img => names.some(n => isStrictImageMatch(n, img.displayName))) || null;
  }

  // ----------------------------------------------------
  // 上传登记表（防重复上传）
  //   问题：部分浏览器 / 英文界面下 Flow 素材卡片不显示文件名，按名称比对永远找不到，
  //        于是「托管上传 → 重新上传失败 → 生成前自动补传」会把同一张图拖进去 2~3 次。
  //   方案：按「项目 ID + 本地路径」登记每次拖放；
  //        confirmed = 已确认进入 Flow，之后不再重复拖放；
  //        pending   = 已拖放但未按名称确认，只要页面图片数达到拖放时的预期数量就视为已到达。
  // ----------------------------------------------------
  const UPLOAD_REGISTRY_KEY = "flowcue_upload_registry_v1";
  const UPLOAD_REGISTRY_TTL_MS = 12 * 60 * 60 * 1000;

  function getProjectKey() {
    const m = location.pathname.match(/\/project\/([^/?#]+)/);
    return m ? m[1] : "";
  }
  function readUploadRegistry() {
    try { return JSON.parse(localStorage.getItem(UPLOAD_REGISTRY_KEY) || "{}") || {}; } catch (_) { return {}; }
  }
  function writeUploadRegistry(reg) {
    try { localStorage.setItem(UPLOAD_REGISTRY_KEY, JSON.stringify(reg)); } catch (_) {}
  }
  function getProjectUploadEntries() {
    const pk = getProjectKey();
    if (!pk) return {};
    const reg = readUploadRegistry();
    const proj = reg[pk] || {};
    const now = Date.now();
    let changed = false;
    Object.keys(proj).forEach(k => { if (!proj[k] || now - (proj[k].t || 0) > UPLOAD_REGISTRY_TTL_MS) { delete proj[k]; changed = true; } });
    if (changed) { reg[pk] = proj; writeUploadRegistry(reg); }
    return proj;
  }
  function registerUploads(paths, info) {
    const pk = getProjectKey();
    if (!pk || !paths || !paths.length) return;
    const reg = readUploadRegistry();
    const proj = reg[pk] || {};
    const now = Date.now();
    paths.forEach(p => {
      const k = String(p || "").trim().toLowerCase();
      if (!k) return;
      if (info.state === "pending" && proj[k] && proj[k].state === "confirmed") return; // 不降级
      proj[k] = { ...info, t: now };
    });
    reg[pk] = proj;
    writeUploadRegistry(reg);
  }
  function clearProjectUploadRegistry() {
    const pk = getProjectKey();
    if (!pk) return;
    const reg = readUploadRegistry();
    delete reg[pk];
    writeUploadRegistry(reg);
  }
  // 登记表判断该路径是否已经在 Flow 中（无法按名称确认时的兜底）
  function isRegisteredInFlow(path, currentCount, entries) {
    const e = (entries || getProjectUploadEntries())[String(path || "").trim().toLowerCase()];
    if (!e) return false;
    if (e.state === "confirmed") return currentCount > 0; // 页面媒体被清空后允许重新上传
    return false; // pending：拖放过但未确认，不能当作已上传
  }

  // 图块稳定标识（优先媒体 ID / 链接中的 UUID），用于判断「拖放后真正新增了几张图」
  function imageStableKey(img) {
    if (img.id && !String(img.id).startsWith("img_")) return String(img.id);
    const s = String(img.src || "");
    const m = s.match(/name=([0-9a-zA-Z_-]+)/) || s.match(/\/([0-9a-fA-F-]{36})/);
    return m ? m[1] : s.split("?")[0];
  }
  function snapshotImageKeys(imgs) { return new Set(imgs.map(imageStableKey)); }
  // 只有「新出现的图块数」和「图块总数增量」都 ≥ 拖放数量时才算数量确认（防止懒加载/重渲染造成假成功）
  function newTilesConfirmed(beforeKeys, beforeLen, imgs, dispatchedCount) {
    const fresh = imgs.filter(i => !beforeKeys.has(imageStableKey(i))).length;
    return dispatchedCount > 0 && fresh >= dispatchedCount && (imgs.length - beforeLen) >= dispatchedCount;
  }

  // 找出「有本地路径但 Flow 页面中还没有」的任务
  // respectRegistry=true 时，会跳过之前已拖放过且已到达的图片（自动/托管流程使用，防止重复上传）
  function getTasksNeedingUpload(rowFilter = null, { respectRegistry = true } = {}) {
    const pageImages = getLoadedPageImages();
    const regEntries = respectRegistry ? getProjectUploadEntries() : {};
    const out = [];
    state.tasks.forEach((task, idx) => {
      if (rowFilter !== null && idx !== rowFilter) return;
      if (!splitTextToLines(task.text).length) return;
      if ((task.status || []).length && task.status.every(s => s === "success")) return;
      const path = task.image?.localImagePath || task.local_image_path || "";
      if (!path || !/^([a-zA-Z]:[\\/]|\/|\\\\)/.test(path)) return; // 必须是绝对路径
      if (findStrictPageImage(task, pageImages)) return;
      if (respectRegistry && isRegisteredInFlow(path, pageImages.length, regEntries)) {
        console.log(`[FlowUpload] 登记表显示已上传过，跳过重复拖放: ${path}`);
        return;
      }
      out.push({ idx, task, path, name: getTaskUploadName(task) });
    });
    // 同一张图被多个任务使用时只上传一次
    const seen = new Set();
    return out.filter(x => { const k = x.path.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; });
  }

  // 定位 Flow 的拖放区域（「开始创建或拖放媒体」/ 素材网格，兼容简中/繁中/英文）
  function findFlowDropPoint() {
    const phrases = [
      "开始创建或拖放媒体", "拖放媒体", "开始创建",
      "開始建立或拖放媒體", "拖放媒體", "新增媒體", "上傳項目",
      "drop media", "drag media", "drop files", "drag files", "start creating"
    ];
    let best = null;
    document.querySelectorAll("div,section,main,[role='main'],[role='button'],span,p").forEach(el => {
      if (el.closest("#flow-batch-overlay, #flow-import-dialog, #flow-alarm-dialog, #flow-batch-fab")) return;
      const r = el.getBoundingClientRect();
      if (r.width < 80 || r.height < 30 || r.bottom <= 0 || r.top >= innerHeight) return;
      const txt = String((el.innerText || "") + " " + (el.getAttribute("aria-label") || "")).replace(/\s+/g, " ").toLowerCase();
      if (!phrases.some(p => txt.includes(p))) return;
      const area = r.width * r.height;
      if (!best || area > best.area) best = { r, area };
    });
    if (best) {
      return {
        x: Math.max(10, Math.min(innerWidth - 10, Math.round(best.r.left + best.r.width / 2))),
        y: Math.max(10, Math.min(innerHeight - 10, Math.round(best.r.top + best.r.height / 2))),
        source: "flow_drop_zone"
      };
    }
    // 素材网格区域
    const grid = document.querySelector("flow-project-page cdk-virtual-scroll-viewport, cdk-virtual-scroll-viewport, .tiles-container, flow-project-page");
    if (grid) {
      const r = grid.getBoundingClientRect();
      if (r.width > 100 && r.height > 100) {
        return { x: Math.round(r.left + r.width / 2), y: Math.round(Math.max(r.top + 60, Math.min(r.bottom - 60, r.top + r.height * 0.4))), source: "media_grid" };
      }
    }
    return { x: Math.round(Math.max(420, innerWidth * 0.58)), y: Math.round(Math.max(330, innerHeight * 0.46)), source: "workspace_fallback" };
  }

  // 上传期间必须隐藏插件的全屏遮罩，否则原生拖放会落在插件面板上而不是 Flow 页面
  function setOverlayHiddenForUpload(hidden) {
    const ids = ["flow-batch-overlay", "flow-import-dialog", "flow-batch-fab"];
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      if (hidden) {
        el.dataset.uploadPrevVisibility = el.style.getPropertyValue("visibility") || "";
        el.style.setProperty("visibility", "hidden", "important");
        el.style.setProperty("pointer-events", "none", "important");
      } else {
        const prev = el.dataset.uploadPrevVisibility;
        if (prev) el.style.setProperty("visibility", prev, "important"); else el.style.removeProperty("visibility");
        el.style.setProperty("pointer-events", "auto", "important");
        delete el.dataset.uploadPrevVisibility;
      }
    });
  }

  /**
   * 把缺失的本地图片批量上传到当前 Flow 项目，并等待素材出现在页面中。
   * @returns {Promise<{ok:boolean, uploaded:number, missing:string[]}>}
   */
  async function uploadLocalImagesToFlow({ rowFilter = null, silent = false, respectRegistry = true } = {}) {
    if (uploadInFlight) { showToast("图片正在上传中，请稍候..."); return { ok: false, uploaded: 0, missing: [] }; }
    if (!location.href.includes("/project/")) {
      showToast("请先进入具体的 Flow 项目页面再上传图片", 5000);
      return { ok: false, uploaded: 0, missing: [] };
    }
    const pending = getTasksNeedingUpload(rowFilter, { respectRegistry });
    if (!pending.length) {
      if (!silent) showToast("所有任务图片都已在 Flow 中，无需上传");
      return { ok: true, uploaded: 0, missing: [] };
    }

    uploadInFlight = true;
    const btn = document.getElementById("btn-upload-local");
    if (btn) btn.disabled = true;
    try {
      showToast(`📤 正在把 ${pending.length} 张本地图片上传到 Flow...`, 4000);
      setOverlayHiddenForUpload(true);
      await delay(250);

      const beforeImgs = getLoadedPageImages();
      const beforeKeys = snapshotImageKeys(beforeImgs);
      let droppedCount = 0;

      // 1) 分批原生拖放
      for (let i = 0; i < pending.length; i += UPLOAD_BATCH_SIZE) {
        if (isStopRequested) break;
        const chunk = pending.slice(i, i + UPLOAD_BATCH_SIZE);
        const pt = findFlowDropPoint();
        console.log(`[FlowUpload] 拖放第 ${i / UPLOAD_BATCH_SIZE + 1} 批 ${chunk.length} 张 @`, pt, chunk.map(c => c.path));
        const res = await sendToExtension("nativeFileDrag", { paths: chunk.map(c => c.path), x: pt.x, y: pt.y });
        if (!res || res.ok === false || res.error) {
          const err = res?.error || "未知错误";
          console.error("[FlowUpload] 原生拖放失败:", err);
          showToast(`❌ 图片上传失败：${err}\n（如提示 debugger 权限，请在扩展管理页重新加载插件）`, 8000);
          return { ok: false, uploaded: 0, missing: pending.map(p => p.name) };
        }
        droppedCount += chunk.length;
        registerUploads(chunk.map(c => c.path), { state: "pending", expect: beforeImgs.length + droppedCount });
        await delay(1500);
      }

      // 2) 轮询 Flow 素材网格，直到全部图片出现（按文件名 或 按新增图块数量 确认）
      const start = Date.now();
      let remaining = pending;
      while (Date.now() - start < UPLOAD_CONFIRM_TIMEOUT_MS) {
        if (isStopRequested) break;
        await delay(2000);
        const pageImages = getLoadedPageImages();
        remaining = pending.filter(p => !findStrictPageImage(p.task, pageImages));
        if (remaining.length && newTilesConfirmed(beforeKeys, beforeImgs.length, pageImages, droppedCount)) {
          console.log(`[FlowUpload] 无法按文件名确认 ${remaining.length} 张，但新出现的图块数 ≥ 拖放 ${droppedCount}，视为已全部到达`);
          remaining = [];
        }
        showToast(`📤 Flow 已接收 ${pending.length - remaining.length}/${pending.length} 张图片...`, 2200);
        if (!remaining.length) break;
      }
      const remainingSet = new Set(remaining.map(r => r.path));
      registerUploads(pending.filter(p => !remainingSet.has(p.path)).map(p => p.path), { state: "confirmed" });
      // 给 Flow 一点时间完成缩略图/媒体处理
      if (!remaining.length) await delay(2500);

      // 3) 将任务严格重新绑定到刚上传的素材（覆盖导入时可能的模糊误匹配）
      const pageImages = getLoadedPageImages();
      state.tasks.forEach((task, idx) => {
        if (rowFilter !== null && idx !== rowFilter) return;
        const m = findStrictPageImage(task, pageImages);
        if (m) {
          task.image = { ...m, src: m.src, displayName: task.image?.displayName || m.displayName, localImagePath: task.image?.localImagePath || task.local_image_path };
        }
      });
      await saveState();

      const missing = remaining.map(r => r.name);
      if (missing.length) {
        console.warn("[FlowUpload] 以下图片未在 Flow 中确认:", missing);
        showToast(`⚠️ 上传完成 ${pending.length - missing.length}/${pending.length}，未确认：${missing.slice(0, 3).join("、")}${missing.length > 3 ? " 等" : ""}`, 8000);
      } else {
        showToast(`✅ ${pending.length} 张本地图片已全部上传到 Flow`, 4000);
      }
      return { ok: missing.length === 0, uploaded: pending.length - missing.length, missing };
    } finally {
      setOverlayHiddenForUpload(false);
      uploadInFlight = false;
      if (btn) btn.disabled = false;
      try { renderTable(); } catch (_) {}
    }
  }

  /**
   * 全局托管模式：按「本地绝对路径列表」上传图片，并逐张确认是否真正进入 Flow。
   * @returns {Promise<{uploaded:string[], skipped:string[], failed:string[], errors:Object}>}
   */
  async function uploadPathsToFlow(paths, { timeoutMs = 60000 } = {}) {
    const clean = [...new Set((paths || []).map(p => String(p || "").trim()).filter(Boolean))];
    const result = { uploaded: [], skipped: [], failed: [], errors: {} };
    if (!clean.length) return result;
    const failAll = (list, reason) => list.forEach(p => { result.failed.push(p); result.errors[p] = reason; });

    if (!location.href.includes("/project/")) { failAll(clean, "浏览器当前不在 Flow 项目页面"); return result; }
    for (let i = 0; uploadInFlight && i < 60; i++) await delay(500);
    if (uploadInFlight) { failAll(clean, "插件正在执行其他上传"); return result; }

    const nameOf = p => getFilenameFromPath(p);
    const isPresent = (p, imgs) => imgs.some(img => isStrictImageMatch(nameOf(p), img.displayName));

    uploadInFlight = true;
    try {
      let imgs = getLoadedPageImages();
      const regEntries = getProjectUploadEntries();
      const todo = [];
      result.verify = {};
      result.page_images_before = imgs.length;
      clean.forEach(p => {
        if (isPresent(p, imgs)) { result.skipped.push(p); result.verify[p] = "name"; return; }
        if (isRegisteredInFlow(p, imgs.length, regEntries)) {
          console.log(`[FlowUpload] 托管上传：登记表显示本浏览器已确认上传过，跳过重复拖放: ${p}`);
          result.skipped.push(p);
          result.verify[p] = "registry";
          return;
        }
        todo.push(p);
      });
      if (!todo.length) return result;

      setOverlayHiddenForUpload(true);
      await delay(250);
      const beforeCount = imgs.length;
      const beforeKeys = snapshotImageKeys(imgs);
      const dispatched = [];
      for (let i = 0; i < todo.length; i += UPLOAD_BATCH_SIZE) {
        const chunk = todo.slice(i, i + UPLOAD_BATCH_SIZE);
        const pt = findFlowDropPoint();
        const res = await sendToExtension("nativeFileDrag", { paths: chunk, x: pt.x, y: pt.y });
        if (!res || res.ok === false || res.error) failAll(chunk, "拖放失败：" + (res?.error || "未知错误"));
        else {
          dispatched.push(...chunk);
          registerUploads(chunk, { state: "pending" });
        }
        await delay(1500);
      }

      // 轮询确认（按文件名，或按「新出现的图块」数量兜底：部分浏览器/英文界面读不到卡片文件名）
      const start = Date.now();
      let waiting = dispatched.slice();
      let countConfirmed = false;
      while (waiting.length && Date.now() - start < timeoutMs) {
        await delay(2000);
        imgs = getLoadedPageImages();
        waiting = waiting.filter(p => !isPresent(p, imgs));
        if (waiting.length && newTilesConfirmed(beforeKeys, beforeCount, imgs, dispatched.length)) {
          console.log(`[FlowUpload] 托管上传：新出现的图块 ≥ 拖放 ${dispatched.length}，视为已全部到达`);
          countConfirmed = true;
          break;
        }
      }
      dispatched.forEach(p => {
        if (!countConfirmed && waiting.includes(p)) { result.failed.push(p); result.errors[p] = "Flow 页面中没有出现这张图片（超时）"; }
        else { result.uploaded.push(p); result.verify[p] = waiting.includes(p) ? "count" : "name"; }
      });
      result.page_images_after = imgs.length;
      registerUploads(result.uploaded, { state: "confirmed" });
      if (result.uploaded.length) await delay(2000);
      return result;
    } finally {
      setOverlayHiddenForUpload(false);
      uploadInFlight = false;
    }
  }

  // ----------------------------------------------------
  // 5. 对接导入解析与闭环报告导出
  // ----------------------------------------------------
  
  // 截取本地路径中的文件名
  function getFilenameFromPath(filePath) {
    if (!filePath) return "";
    const cleanPath = filePath.replace(/\\/g, "/");
    return cleanPath.substring(cleanPath.lastIndexOf("/") + 1);
  }

  function normalizeDuration(value, fallback = null) {
    if (value == null || value === "") return fallback;
    const match = String(value).trim().match(/^(\d+(?:\.\d+)?)\s*(?:s|秒|seconds?)?$/i);
    const seconds = match ? Number(match[1]) : NaN;
    if (!Number.isFinite(seconds) || seconds <= 0) throw new Error(`无效的视频秒数: ${value}`);
    return seconds;
  }

  // 手动解析 JSON 数据对接导入
  function handleJSONImport() {
    const rawVal = document.getElementById("flow-import-textarea").value.trim();
    if (!rawVal) {
      alert("内容为空！");
      return;
    }

    let parsedList = [];
    try {
      parsedList = JSON.parse(rawVal);
      if (!Array.isArray(parsedList)) {
        alert("导入数据格式错误！必须为 JSON 数组格式（由 [ 和 ] 包裹）。");
        return;
      }
    } catch (e) {
      alert(`JSON 解析失败，请检查格式！\n错误详情: ${e.message}`);
      return;
    }

    let importCount = 0;
    try {
      importCount = importTaskList(parsedList);
    } catch (err) { alert(`导入失败：${err.message}`); return; }

    closeImportDialog();
    renderTable();
    saveState();
    
    showToast(`导入成功！已加入 ${importCount} 个任务配置`);
  }

  // 可复用的任务导入核心（手动导入 / 全局托管模式共用）；出错时抛出异常，不弹窗
  function importTaskList(parsedList) {
    if (!Array.isArray(parsedList)) throw new Error("任务数据必须是数组");
    parsedList.forEach((item, index) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error(`第 ${index + 1} 项不是任务对象`);
      item.duration = normalizeDuration(item.duration ?? item.duration_seconds ?? item.durationSeconds, window.__lzxFlowTransport ? null : 6);
    });
    const loadedImages = getLoadedPageImages();
    console.log("[FlowUI] 正在导入 JSON，当前页面识别到的图片列表为:", loadedImages);
    
    let importCount = 0;

    parsedList.forEach(item => {
      const prompt = item.prompt || item.text || "";
      const mode = item.mode || item.footageType || "VIDEO_FRAMES";
      const duration = item.duration;
      const downloadPath = item.download_path || item.downloadPath || "";
      const imageName = item.image_name || item.imageName || item.displayName || item.name || "";
      const localImagePath = item.local_image_path || item.localImagePath || item.path || "";
      const localFileName = getFilenameFromPath(localImagePath);
      // 支持直接在 JSON 中传入网络图片或 Base64 DataURL
      const incomingSrc = item.image_url || item.imageUrl || item.src || item.image_src || item.base64 || item.image_base64 || item.dataUrl || "";

      // 图片查找匹配逻辑：采用多维度智能图名比对 (归一化、中英文括号、序号一致性、关键词交集)
      let matchedImg = null;
      if (imageName || localFileName) {
        matchedImg = loadedImages.find(img => {
          return (imageName && isImageNameMatch(imageName, img.displayName)) ||
                 (localFileName && isImageNameMatch(localFileName, img.displayName));
        });
      }

      console.log("[FlowUI] 匹配比对详情:", {
        输入图名: imageName,
        输入本地路径: localImagePath,
        匹配目标清理: { targetName: cleanCompactName(imageName), targetLocalName: cleanCompactName(localFileName) },
        匹配结果: matchedImg ? matchedImg.displayName : "未在页面即时找到"
      });

      const finalDisplayName = imageName || localFileName || (matchedImg ? matchedImg.displayName : "未命名素材");
      let finalTaskImage = null;

      if (matchedImg) {
        finalTaskImage = {
          ...matchedImg,
          src: matchedImg.src || incomingSrc,
          displayName: matchedImg.displayName || finalDisplayName,
          localImagePath: localImagePath
        };
      } else {
        finalTaskImage = {
          id: null,
          primaryMediaKey: null,
          displayName: finalDisplayName,
          src: incomingSrc,
          localImagePath: localImagePath
        };
      }

      // 关键！状态一律初始化为 pending 就绪态，绝不导入时判为 failed 错误！
      const linesCount = splitTextToLines(prompt).length || 1;
      const statusList = new Array(linesCount).fill("pending");
      const messageList = new Array(linesCount).fill("等待批量调度");

      // 提取 JSON 中可能指定的独立比例、分辨率、模型和间隔时间 (具备智能归一化与规则同步)
      const rawAspect = item.aspect_ratio || item.aspectRatio || "";
      let taskAspect = state.settings.aspectRatio;
      if (rawAspect.includes("9:16") || rawAspect.includes("9_16") || String(rawAspect).toUpperCase().includes("PORTRAIT")) {
        taskAspect = "ASPECT_RATIO_9_16";
      } else if (rawAspect.includes("1:1") || rawAspect.includes("1_1") || String(rawAspect).toUpperCase().includes("SQUARE")) {
        taskAspect = "ASPECT_RATIO_1_1";
      } else if (rawAspect.includes("16:9") || rawAspect.includes("16_9") || String(rawAspect).toUpperCase().includes("LANDSCAPE")) {
        taskAspect = "ASPECT_RATIO_16_9";
      }

      const rawRes = item.resolution || item.video_resolution || "";
      let taskRes = state.settings.resolution;
      if (String(rawRes).includes("360")) taskRes = "VIDEO_RESOLUTION_360P";
      else if (String(rawRes).includes("1080")) taskRes = "VIDEO_RESOLUTION_1080P";
      else if (String(rawRes).includes("720")) taskRes = "VIDEO_RESOLUTION_720P";

      const rawModel = String(item.model || item.video_model || "").trim().toLowerCase();
      let taskModel = state.settings.videoModel || "omni_1_1_flash";
      if (rawModel) {
        if (rawModel.includes("omni") || rawModel.includes("flash")) {
          taskModel = "omni_1_1_flash";
        } else if (rawModel.includes("low_priority") || rawModel.includes("lower priority") || rawModel.includes("lowpriority") || rawModel.includes("低优先级") || rawModel.includes("低優先順序")) {
          taskModel = "veo_3_1_lite_low_priority";
        } else if (rawModel.includes("quality")) {
          taskModel = "veo_3_1_quality";
        } else if (rawModel.includes("fast")) {
          taskModel = "veo_3_1_fast";
        } else if (rawModel.includes("lite")) {
          taskModel = "veo_3_1_lite";
        } else if (rawModel.includes("veo_2") || rawModel.includes("veo2") || rawModel === "veo 2") {
          taskModel = "veo_2";
        }
      }

      const rawInterval = item.interval ?? item.task_interval;
      let taskInterval = null;
      if (rawInterval !== undefined && rawInterval !== null && rawInterval !== "") {
        const parsedInt = Number(rawInterval);
        if (Number.isFinite(parsedInt) && parsedInt >= 0) {
          taskInterval = parsedInt;
          // 同步软件规则设置的间隔时间到全局设置，并将随机抖动归零以严格执行该间隔
          state.settings.interval = taskInterval;
          state.settings.intervalRandom = 0;
        }
      }

      // 同步软件规则设置的默认参数到插件面板
      if (rawAspect) state.settings.aspectRatio = taskAspect;
      if (rawModel) state.settings.videoModel = taskModel;
      if (item.mode || item.footageType) state.settings.footageType = mode;

      state.tasks.push({
        image: finalTaskImage,
        text: prompt,
        preset: "无模版(直接填入)",
        footageType: mode,
        count: item.count || 1,
        status: statusList,
        message: messageList,
        downloaded: new Array(linesCount).fill(false),
        download_path: downloadPath,
        duration: duration,
        aspectRatio: taskAspect,
        resolution: taskRes,
        videoModel: taskModel,
        interval: taskInterval,
        project_id: item.project_id || item.projectId || "",
        project_short: item.project_short || item.projectShort || "",
        segment_index: item.segment_index ?? item.segmentIndex ?? item.index ?? null,
        segment_no: item.segment_no || item.segmentNo || "",
        batch_no: item.batch_no ?? item.batchNo ?? null
      });
      importCount++;
    });

    // 同步更新设置抽屉 DOM 控件显示
    const syncDomVal = (id, val) => {
      const el = document.getElementById(id);
      if (el && val !== undefined && val !== null) el.value = val;
    };
    syncDomVal("cfg-interval", state.settings.interval);
    syncDomVal("cfg-random", state.settings.intervalRandom);
    syncDomVal("cfg-aspect", state.settings.aspectRatio);
    syncDomVal("cfg-model", state.settings.videoModel);
    syncDomVal("cfg-footagetype", state.settings.footageType);

    return importCount;
  }

  // 复制执行报告（供 Python 读取剪贴板，一键归位）
  function copyExecutionReport() {
    if (!state.tasks.length) {
      alert("当前任务列表为空，无可导出的报告。");
      return;
    }

    const report = state.tasks.map(task => {
      // 合并状态 (因为页面支持分行，导入只对应单条绝对路径，直接去 status[0])
      return {
        prompt: task.text,
        image_name: task.image.displayName,
        download_path: task.download_path,
        duration: task.duration ?? null,
        status: task.status[0] || "pending",
        message: task.message[0] || ""
      };
    });

    try {
      const jsonStr = JSON.stringify(report, null, 2);
      navigator.clipboard.writeText(jsonStr).then(() => {
        alert("执行报告 JSON 已成功复制到系统剪贴板！\n请回到您的 Python 客户端软件中读取并点击“剪切归位”按钮。");
      }).catch(err => {
        alert("复制剪贴板失败，请手动在控制台查看。");
        console.log("[Execution Report]", jsonStr);
      });
    } catch (e) {
      alert("导出错误: " + e.message);
    }
  }

  // ----------------------------------------------------
  // 6. 任务序号/项目简称解析、手动下载并重命名、任务表格渲染
  // ----------------------------------------------------
  function escapeHtmlAttr(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // 解析任务所属项目简称 (01_用户名称前10字符) 与软件任务序号 (如 08 / 08.mp4)
  function parseTaskIdentity(task, rowIndex) {
    const dlPath = String(task?.download_path || "").trim();
    const imgPath = String(task?.image?.localImagePath || task?.local_image_path || "").trim();
    const normDl = dlPath.replace(/\\/g, "/");
    const normImg = imgPath.replace(/\\/g, "/");

    // 1. 提取项目文件夹完整名称 (如 01_用户的名称_备注-flow)
    let rawFolder = String(task?.project_id || "").trim();
    if (!rawFolder) {
      const pickFolderFromPath = (p) => {
        if (!p) return "";
        const parts = p.split("/").filter(Boolean);
        const flowFolder = parts.find(seg => /-flow$/i.test(seg));
        if (flowFolder) return flowFolder;
        const dlIdx = parts.findIndex(seg => seg.toLowerCase() === "downloads");
        if (dlIdx > 0) return parts[dlIdx - 1];
        if (parts.length >= 2) return parts[parts.length - 2];
        return "";
      };
      rawFolder = pickFolderFromPath(normDl) || pickFolderFromPath(normImg);
    }

    // 2. 提取「项目序号(01、02...) + 用户名称前10个字符」
    let projectShort = String(task?.project_short || "").trim();
    if (!projectShort && rawFolder) {
      const cleanFolder = rawFolder.replace(/-flow$/i, "").trim();
      const m = cleanFolder.match(/^(\d+)(?:[_\-\s]+(.*))?$/);
      if (m) {
        const numStr = m[1].padStart(2, "0");
        const rest = (m[2] || "").trim();
        let userNamePart = rest;
        const lastUnderscore = rest.lastIndexOf("_");
        if (lastUnderscore > 0) {
          userNamePart = rest.slice(0, lastUnderscore);
        }
        if (userNamePart.startsWith(`${numStr}_`)) {
          userNamePart = userNamePart.slice(numStr.length + 1).trim();
        }
        const first10 = Array.from(userNamePart).slice(0, 10).join("");
        projectShort = first10 ? `${numStr}_${first10}` : numStr;
      } else {
        projectShort = Array.from(cleanFolder).slice(0, 12).join("");
      }
    }
    if (!projectShort) {
      projectShort = "当前网页任务";
    }

    // 3. 提取软件中的具体任务序号 (如 01, 02, 08 -> 对应 08.mp4，与图片名称不同)
    let segmentNo = String(task?.segment_no || "").trim();
    let videoFileName = "";
    if (normDl) {
      const base = normDl.substring(normDl.lastIndexOf("/") + 1);
      if (base) {
        videoFileName = base;
        const stem = base.replace(/\.\w+$/, "");
        if (!segmentNo && stem) {
          segmentNo = /^\d+$/.test(stem) ? stem.padStart(2, "0") : stem;
        }
      }
    }
    if (!segmentNo && task?.segment_index != null && task.segment_index !== "") {
      const n = Number(task.segment_index);
      if (Number.isFinite(n) && n > 0) segmentNo = String(n).padStart(2, "0");
    }
    if (!segmentNo) {
      segmentNo = String(rowIndex + 1).padStart(2, "0");
    }
    if (!videoFileName) {
      videoFileName = `${segmentNo}.mp4`;
    }

    const batchNo = (task?.batch_no != null && task.batch_no !== "") ? Number(task.batch_no) : null;
    const saveDirPath = dlPath ? dlPath.replace(/[\\/][^\\/]+$/, "") : "";

    return {
      projectFull: rawFolder || projectShort,
      projectShort,
      segmentNo,
      videoFileName,
      batchNo: Number.isFinite(batchNo) && batchNo > 0 ? batchNo : null,
      fullSavePath: dlPath,
      saveDirPath
    };
  }

  // 浏览器端直接拉取视频 Blob 并触发重命名下载 (针对部分拦截 chrome.downloads 的特殊多开浏览器保底)
  async function triggerBrowserBlobRenameDownload(videoUrl, targetFileName, task, rowIndex, lineIdx = 0) {
    const cleanUrl = String(videoUrl || "").trim().replace(/&amp;/g, "&");
    if (!cleanUrl) return false;
    try {
      showToast(`⏳ 正在拉取视频数据并重命名为 ${targetFileName}...`, 4000);
      const resp = await fetch(cleanUrl, { credentials: "include" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = targetFileName;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {
        try { a.remove(); URL.revokeObjectURL(blobUrl); } catch (_) {}
      }, 15000);
      if (task) {
        task.downloaded = task.downloaded || [];
        task.downloaded[lineIdx] = true;
        updateLineStatus(rowIndex, lineIdx, "success", `已手动下载并重命名: ${targetFileName}`);
      }
      showToast(`✅ 已通过浏览器直存下载并重命名为: ${targetFileName}\n📋 完整保存路径已复制到剪贴板`, 5000);
      return true;
    } catch (err) {
      console.warn("[FlowUI] Blob 拉取重命名失败，降级为 a[download] 直链触发:", err);
      try {
        const a = document.createElement("a");
        a.href = cleanUrl;
        a.download = targetFileName;
        a.target = "_blank";
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        a.remove();
        showToast(`⚠️ 已打开视频直链，请按 Ctrl+S 保存并粘贴剪贴板中的路径/文件名 (${targetFileName})`, 6000);
        return true;
      } catch (_) {
        return false;
      }
    }
  }

  // 执行手动下载并自动重命名 (优先走插件后台目录+重命名通道，失败自动降级为页面 Blob 重命名下载)
  async function executeManualDownloadAndRename(videoUrl, task, rowIndex, lineIdx = 0) {
    const cleanUrl = String(videoUrl || "").trim().replace(/&amp;/g, "&");
    if (!cleanUrl) {
      showToast("❌ 视频链接为空，无法下载");
      return false;
    }
    const identity = parseTaskIdentity(task, rowIndex);
    const targetRelPath = resolveDownloadPath(task?.download_path, { image: task?.image, rowIndex, lineIndex: lineIdx });
    const flatFileName = identity.videoFileName;
    const copyText = identity.fullSavePath || flatFileName;

    try {
      await navigator.clipboard.writeText(copyText);
    } catch (_) {}

    const mediaId = mediaIdFromVideoUrl(cleanUrl) || task?.mediaBindings?.[lineIdx]?.mediaIds?.[0] || "manual";
    console.log(`[FlowUI] 📥 手动下载并重命名启动: 项目=${identity.projectShort}, 序号=#${identity.segmentNo}, 目标=${targetRelPath}, URL=${cleanUrl.substring(0, 90)}`);

    const res = await sendToExtension("download", {
      url: cleanUrl,
      filename: targetRelPath,
      task: {
        manual: true,
        flatFilename: flatFileName,
        rowIndex,
        lineIndex: lineIdx,
        taskId: task?.taskId || `manual_${rowIndex}`,
        submissionId: task?.mediaBindings?.[lineIdx]?.submissionId || `manual_sub_${Date.now()}`,
        mediaId,
        projectId: getStorageKey(),
        download_path: targetRelPath
      }
    });

    if (res && res.success) {
      if (task) {
        task.videoUrls = task.videoUrls || [];
        task.videoUrls[lineIdx] = cleanUrl;
        task.downloadIds = task.downloadIds || [];
        task.downloadIds[lineIdx] = res.downloadId;
        task.downloaded = task.downloaded || [];
        task.downloaded[lineIdx] = true;
        updateLineStatus(rowIndex, lineIdx, "success", `已手动下载: ${identity.videoFileName}`);
      }
      showToast(`✅ 已手动下载并重命名: [${identity.projectShort}] #${identity.segmentNo} ➔ ${identity.videoFileName}\n📋 保存路径已复制到剪贴板`, 4500);
      return true;
    }

    console.warn("[FlowUI] 后台扩展下载返回失败，启动页面级 Blob 重命名保底下载:", res?.error);
    return await triggerBrowserBlobRenameDownload(cleanUrl, flatFileName, task, rowIndex, lineIdx);
  }

  // 自动查找当前任务行在页面或缓存中对应的视频候选列表
  async function findTaskVideoCandidates(task, lineIdx = 0) {
    const candidates = [];
    const seenUrls = new Set();
    const addCandidate = (url, thumbSrc = "", promptText = "", source = "") => {
      const clean = String(url || "").trim().replace(/&amp;/g, "&");
      if (!clean || isStaticShowcaseAsset(clean)) return;
      const key = videoIdentity(clean);
      if (seenUrls.has(key)) return;
      seenUrls.add(key);
      candidates.push({ url: clean, mediaId: mediaIdFromVideoUrl(clean), thumbSrc, promptText, source });
    };

    // 1. 已记录在 task.videoUrls 中的直链
    if (task?.videoUrls?.[lineIdx]) {
      addCandidate(task.videoUrls[lineIdx], "", task.text || "", "任务已缓存直链");
    }

    // 2. 已记录在 mediaBindings 或全局拦截表中的直链
    const binding = task?.mediaBindings?.[lineIdx];
    const boundIds = [
      ...(Array.isArray(binding?.readyIds) ? binding.readyIds : []),
      ...(Array.isArray(binding?.mediaIds) ? binding.mediaIds : [])
    ];
    for (const mId of boundIds) {
      if (binding?.urls?.[mId]) addCandidate(binding.urls[mId], "", task.text || "", `绑定ID (${mId.slice(0, 8)})`);
      if (discoveredVideoUrlsById.has(mId)) addCandidate(discoveredVideoUrlsById.get(mId), "", task.text || "", `网络拦截ID (${mId.slice(0, 8)})`);
    }

    // 3. 根据任务独有提示词或 mediaId 在当前页面定位所属批次容器，并自动唤醒懒加载 <video>
    const container = findCurrentBatchContainer(task, lineIdx);
    if (container) {
      const tiles = Array.from(container.querySelectorAll("flow-video-tile"));
      if (tiles.length > 0) {
        tiles.forEach(t => triggerTileHover(t));
        await delay(380);
      }
      const promptEl = container.querySelector(".prompt-text, flow-expandable-prompt");
      const containerPrompt = promptEl ? (promptEl.innerText || promptEl.textContent || "").trim() : (task?.text || "");
      const videos = Array.from(container.querySelectorAll("video"));
      for (const v of videos) {
        const src = (v.getAttribute("src") || v.currentSrc || v.src || "").trim().replace(/&amp;/g, "&");
        const tile = typeof v.closest === "function" ? (v.closest("flow-video-tile") || v.closest("flow-grid-tile-container")) : null;
        const thumbImg = tile ? tile.querySelector("img.thumbnail, img[src*='flow.google.com/asb/'], img[src*='flow-content.google/image/']") : null;
        const thumbSrc = thumbImg ? (thumbImg.getAttribute("src") || thumbImg.src || "") : "";
        if (src) addCandidate(src, thumbSrc, containerPrompt, "页面提示词精准匹配");
      }
    }

    return candidates;
  }

  // 打开可视化「手动选择页面视频并重命名下载」弹窗
  async function openManualDownloadPickerModal(task, rowIndex, lineIdx = 0, preloadedCandidates = null) {
    const existingModal = document.getElementById("flow-manual-dl-modal");
    if (existingModal) existingModal.remove();

    const identity = parseTaskIdentity(task, rowIndex);
    const autoList = Array.isArray(preloadedCandidates) ? preloadedCandidates : await findTaskVideoCandidates(task, lineIdx);

    // 扫描页面上所有批次容器中的视频卡片，先唤醒可见区域的缩略图懒加载
    const allTiles = Array.from(document.querySelectorAll("flow-video-tile"));
    for (const tile of allTiles.slice(0, 12)) {
      const v = tile.querySelector("video");
      const hasSrc = v && (v.getAttribute("src") || v.currentSrc || v.src);
      if (!hasSrc) triggerTileHover(tile);
    }
    if (allTiles.length > 0) await delay(380);

    const pageVideos = [];
    const seenKeys = new Set();
    const autoKeys = new Set(autoList.map(c => videoIdentity(c.url)));

    // 先加入自动匹配的候选视频
    for (const item of autoList) {
      const k = videoIdentity(item.url);
      seenKeys.add(k);
      pageVideos.push({ ...item, isMatched: true });
    }

    // 再扫描页面上所有 video 元素
    const allVideoEls = Array.from(document.querySelectorAll("flow-video-tile video, .batch-container video, .virtual-item-container video, video"));
    for (const v of allVideoEls) {
      if (v.closest && v.closest("#flow-batch-overlay")) continue;
      const src = (v.getAttribute("src") || v.currentSrc || v.src || "").trim().replace(/&amp;/g, "&");
      if (!src || isStaticShowcaseAsset(src)) continue;
      const k = videoIdentity(src);
      if (seenKeys.has(k)) continue;
      seenKeys.add(k);

      const tile = typeof v.closest === "function" ? (v.closest("flow-video-tile") || v.closest("flow-grid-tile-container")) : null;
      const thumbImg = tile ? tile.querySelector("img.thumbnail, img[src*='flow.google.com/asb/'], img[src*='flow-content.google/image/']") : null;
      const thumbSrc = thumbImg ? (thumbImg.getAttribute("src") || thumbImg.src || "") : "";
      const batchContainer = typeof v.closest === "function" ? v.closest(".batch-container, .virtual-item-container") : null;
      const promptEl = batchContainer ? batchContainer.querySelector(".prompt-text, flow-expandable-prompt") : null;
      const promptText = promptEl ? (promptEl.innerText || promptEl.textContent || "").trim() : "";

      pageVideos.push({
        url: src,
        mediaId: mediaIdFromVideoUrl(src),
        thumbSrc,
        promptText,
        source: "当前页面视频",
        isMatched: autoKeys.has(k)
      });
    }

    const modal = document.createElement("div");
    modal.id = "flow-manual-dl-modal";
    modal.style.cssText = "position:fixed; inset:0; z-index:2147483646; background:rgba(9,13,22,0.78); backdrop-filter:blur(6px); display:flex; align-items:center; justify-content:center; padding:20px; font-family:var(--flow-font-body, system-ui, sans-serif);";

    const fullPathDisplay = identity.fullSavePath || `Flow/${identity.projectFull}/${identity.videoFileName}`;
    const cardsHtml = pageVideos.length > 0 ? pageVideos.map((item, idx) => `
      <div style="display:flex; align-items:center; gap:12px; padding:10px 12px; border-radius:8px; background:${item.isMatched ? 'rgba(16,185,129,0.14)' : 'rgba(30,41,59,0.75)'}; border:1px solid ${item.isMatched ? '#10b981' : 'rgba(148,163,184,0.22)'};">
        <div style="width:64px; height:84px; border-radius:6px; overflow:hidden; background:#0f172a; flex-shrink:0; display:flex; align-items:center; justify-content:center; border:1px solid rgba(255,255,255,0.1);">
          ${item.thumbSrc
            ? `<img src="${escapeHtmlAttr(item.thumbSrc)}" style="width:100%; height:100%; object-fit:cover;" />`
            : `<video src="${escapeHtmlAttr(item.url)}" muted preload="metadata" style="width:100%; height:100%; object-fit:cover;"></video>`}
        </div>
        <div style="flex:1; min-width:0; display:flex; flex-direction:column; gap:5px;">
          <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
            ${item.isMatched
              ? `<span style="background:#10b981; color:#fff; font-size:11px; font-weight:700; padding:2px 7px; border-radius:4px;">🎯 自动匹配本任务 (#${escapeHtmlAttr(identity.segmentNo)})</span>`
              : `<span style="background:rgba(148,163,184,0.2); color:#cbd5e1; font-size:11px; padding:2px 7px; border-radius:4px;">页面视频 #${idx + 1}</span>`}
            <span style="color:#94a3b8; font-size:11px;">${escapeHtmlAttr(item.source || "")}</span>
          </div>
          <div style="color:#e2e8f0; font-size:12px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;" title="${escapeHtmlAttr(item.promptText || "")}">
            ${escapeHtmlAttr(item.promptText || "(未提取到卡片提示词文本)")}
          </div>
        </div>
        <div style="display:flex; flex-direction:column; gap:6px; flex-shrink:0;">
          <button class="btn-modal-ext-dl" data-idx="${idx}" style="padding:6px 12px; border-radius:6px; border:none; background:#8b5cf6; color:#fff; font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap;">
            📥 下载并重命名为 ${escapeHtmlAttr(identity.videoFileName)}
          </button>
          <button class="btn-modal-blob-dl" data-idx="${idx}" style="padding:5px 10px; border-radius:6px; border:1px solid rgba(56,189,248,0.45); background:rgba(56,189,248,0.12); color:#38bdf8; font-size:11px; font-weight:600; cursor:pointer; white-space:nowrap;" title="若插件后台下载被浏览器拦截，点击此按钮直接用网页流式下载为 ${escapeHtmlAttr(identity.videoFileName)}">
            💾 浏览器直存 ${escapeHtmlAttr(identity.videoFileName)}
          </button>
        </div>
      </div>
    `).join("") : `
      <div style="text-align:center; padding:32px 16px; color:#94a3b8; font-size:13px;">
        当前页面暂未检测到已加载出视频直链的卡片。<br/>
        请先关闭此窗口，将鼠标移动到网页上已生成的视频缩略图上悬停 1 秒，再点击「📥 手动下载」。
      </div>
    `;

    setSafeHTML(modal, `
      <div style="width:740px; max-width:95vw; max-height:86vh; background:#0f172a; border:1px solid rgba(139,92,246,0.45); border-radius:12px; box-shadow:0 24px 60px rgba(0,0,0,0.65); display:flex; flex-direction:column; overflow:hidden; color:#f8fafc;">
        <div style="padding:14px 18px; background:rgba(30,41,59,0.9); border-bottom:1px solid rgba(148,163,184,0.18); display:flex; align-items:center; justify-content:space-between;">
          <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
            <span style="font-size:15px; font-weight:700; color:#f8fafc;">📥 手动下载并重命名</span>
            <span style="background:rgba(139,92,246,0.22); border:1px solid rgba(139,92,246,0.5); color:#c4b5fd; font-size:12px; font-weight:700; padding:2px 8px; border-radius:5px;">
              项目: ${escapeHtmlAttr(identity.projectShort)}
            </span>
            <span style="background:rgba(16,185,129,0.2); border:1px solid rgba(16,185,129,0.5); color:#6ee7b7; font-size:12px; font-weight:700; padding:2px 8px; border-radius:5px;">
              任务序号: #${escapeHtmlAttr(identity.segmentNo)} (${escapeHtmlAttr(identity.videoFileName)})${identity.batchNo ? ` · 第${identity.batchNo}批` : ""}
            </span>
          </div>
          <button id="btn-close-manual-dl-modal" style="background:transparent; border:none; color:#94a3b8; font-size:18px; cursor:pointer; padding:2px 6px;">✕</button>
        </div>

        <div style="padding:12px 18px; background:rgba(15,23,42,0.95); border-bottom:1px solid rgba(148,163,184,0.14); display:flex; flex-direction:column; gap:8px; font-size:12px;">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap;">
            <div style="color:#cbd5e1; min-width:0; flex:1; word-break:break-all;">
              <strong style="color:#94a3b8;">软件目标保存位置：</strong>
              <code style="color:#38bdf8; background:rgba(30,41,59,0.9); padding:2px 6px; border-radius:4px;">${escapeHtmlAttr(fullPathDisplay)}</code>
            </div>
            <div style="display:flex; gap:6px; flex-shrink:0;">
              <button id="btn-copy-full-save-path" style="padding:4px 9px; border-radius:5px; border:1px solid rgba(148,163,184,0.3); background:rgba(51,65,85,0.8); color:#e2e8f0; font-size:11px; cursor:pointer;">📋 复制完整保存路径</button>
              <button id="btn-copy-only-filename" style="padding:4px 9px; border-radius:5px; border:1px solid rgba(148,163,184,0.3); background:rgba(51,65,85,0.8); color:#e2e8f0; font-size:11px; cursor:pointer;">📋 仅复制 ${escapeHtmlAttr(identity.videoFileName)}</button>
            </div>
          </div>
          <div style="color:#94a3b8; font-size:11px;">
            💡 提示：点击下方按钮会自动将视频下载并重命名为 <strong style="color:#f8fafc;">${escapeHtmlAttr(identity.videoFileName)}</strong>。若浏览器弹出“另存为”窗口，可直接按 <strong style="color:#f8fafc;">Ctrl+V</strong> 粘贴上方已为您复制好的完整路径！
          </div>
        </div>

        <div style="padding:14px 18px; overflow-y:auto; display:flex; flex-direction:column; gap:10px; max-height:58vh;">
          ${cardsHtml}
        </div>
      </div>
    `);

    document.body.appendChild(modal);

    const closeBtn = modal.querySelector("#btn-close-manual-dl-modal");
    if (closeBtn) closeBtn.addEventListener("click", () => modal.remove());
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.remove();
    });

    const copyPathBtn = modal.querySelector("#btn-copy-full-save-path");
    if (copyPathBtn) {
      copyPathBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(fullPathDisplay).then(() => {
          showToast(`✅ 已复制完整保存路径: ${fullPathDisplay}`);
        }).catch(() => {});
      });
    }
    const copyNameBtn = modal.querySelector("#btn-copy-only-filename");
    if (copyNameBtn) {
      copyNameBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(identity.videoFileName).then(() => {
          showToast(`✅ 已复制目标文件名: ${identity.videoFileName}`);
        }).catch(() => {});
      });
    }

    modal.querySelectorAll(".btn-modal-ext-dl").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        const idx = parseInt(e.currentTarget.dataset.idx, 10);
        const targetItem = pageVideos[idx];
        if (!targetItem) return;
        await executeManualDownloadAndRename(targetItem.url, task, rowIndex, lineIdx);
        modal.remove();
      });
    });

    modal.querySelectorAll(".btn-modal-blob-dl").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        const idx = parseInt(e.currentTarget.dataset.idx, 10);
        const targetItem = pageVideos[idx];
        if (!targetItem) return;
        try { await navigator.clipboard.writeText(fullPathDisplay); } catch (_) {}
        await triggerBrowserBlobRenameDownload(targetItem.url, identity.videoFileName, task, rowIndex, lineIdx);
        modal.remove();
      });
    });
  }

  // 点击任务行「📥 手动下载」按钮的主入口
  async function handleManualDownloadForTask(task, rowIndex, lineIdx = 0, forcePicker = false) {
    const identity = parseTaskIdentity(task, rowIndex);
    const copyText = identity.fullSavePath || identity.videoFileName;
    try {
      await navigator.clipboard.writeText(copyText);
    } catch (_) {}

    const candidates = await findTaskVideoCandidates(task, lineIdx);
    // 如果精准找到了唯一匹配的本任务视频且未要求强制打开选择器，直接一键下载并重命名！
    if (!forcePicker && candidates.length === 1 && !task?.downloaded?.[lineIdx]) {
      const ok = await executeManualDownloadAndRename(candidates[0].url, task, rowIndex, lineIdx);
      if (ok) return;
    }
    // 若未自动匹配到唯一视频、或已下载过想重新选择、或按住 Shift 点击，则打开可视化页面视频选择与重命名弹窗
    await openManualDownloadPickerModal(task, rowIndex, lineIdx, candidates);
  }

  function renderTable() {
    const tbody = document.querySelector("#flow-tasks-table tbody");
    if (!tbody) return;
    tbody.replaceChildren();

    // 自动对任务列表中没有预览图的任务尝试结合页面图片进行关联补全
    const pageImages = getLoadedPageImages();
    if (pageImages.length > 0) {
      state.tasks.forEach(task => {
        if (!task.image?.src) {
          const taskName = task.image?.displayName || "";
          const localName = getFilenameFromPath(task.image?.localImagePath || task.local_image_path);
          const matched = pageImages.find(img => 
            (taskName && isImageNameMatch(taskName, img.displayName)) || 
            (localName && isImageNameMatch(localName, img.displayName))
          );
          if (matched) {
            task.image = {
              ...matched,
              src: matched.src,
              displayName: task.image?.displayName || matched.displayName,
              localImagePath: task.image?.localImagePath || task.local_image_path
            };
          }
        }
      });
    }

    if (!state.tasks.length) {
      setSafeHTML(tbody, `
        <tr>
          <td colspan="9" style="text-align: center; color: var(--flow-text-secondary); padding: 40px 0;">
            暂无任务。请点击“导入外部 JSON”粘贴任务数据，或点击“加载页面图片”直接导入当前网页上的原料。
          </td>
        </tr>
      `);
      return;
    }

    state.tasks.forEach((task, rowIndex) => {
      const tr = document.createElement("tr");
      const identity = parseTaskIdentity(task, rowIndex);

      // 0. 任务序号 / 所属项目简称列 (显示 01_用户名称前10字符 + 软件中的具体任务序号如 #08 / 08.mp4)
      const seqTd = document.createElement("td");
      seqTd.style.width = "145px";
      seqTd.style.textAlign = "center";
      seqTd.style.verticalAlign = "middle";
      const fullTooltip = `所属项目: ${identity.projectFull}\n软件任务序号: #${identity.segmentNo} (${identity.videoFileName})${identity.batchNo ? `\n所属批次: 第 ${identity.batchNo} 批` : ""}\n关联图片: ${task.image?.displayName || "无"}\n保存位置: ${identity.fullSavePath || "默认下载目录"}\n(点击可复制完整保存路径)`;
      setSafeHTML(seqTd, `
        <div class="cell-seq-project-box" title="${escapeHtmlAttr(fullTooltip)}" style="display:flex; flex-direction:column; align-items:center; gap:4px; padding:4px 2px; cursor:pointer; user-select:none;">
          <div style="display:flex; align-items:center; justify-content:center; gap:4px; flex-wrap:wrap;">
            <span style="background:linear-gradient(135deg, #8b5cf6, #6366f1); color:#fff; font-weight:800; font-size:12px; padding:2px 7px; border-radius:5px; letter-spacing:0.3px; box-shadow:0 2px 6px rgba(139,92,246,0.35);">
              序号 #${escapeHtmlAttr(identity.segmentNo)}
            </span>
            ${identity.batchNo ? `<span style="background:rgba(56,189,248,0.16); border:1px solid rgba(56,189,248,0.4); color:#38bdf8; font-size:10px; font-weight:700; padding:1px 5px; border-radius:4px;">第${identity.batchNo}批</span>` : ""}
          </div>
          <div style="font-size:11px; font-weight:700; color:#e2e8f0; max-width:136px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtmlAttr(identity.projectFull)}">
            ${escapeHtmlAttr(identity.projectShort)}
          </div>
          <div style="font-size:10px; color:#94a3b8; background:rgba(30,41,59,0.75); border:1px solid rgba(148,163,184,0.2); padding:1px 6px; border-radius:4px; display:inline-flex; align-items:center; gap:3px;">
            <span>🎬 ${escapeHtmlAttr(identity.videoFileName)}</span>
            <span style="color:#38bdf8;">📋</span>
          </div>
        </div>
      `);
      const seqBox = seqTd.querySelector(".cell-seq-project-box");
      if (seqBox) {
        seqBox.addEventListener("click", (e) => {
          e.stopPropagation();
          const copyTarget = identity.fullSavePath || identity.videoFileName;
          navigator.clipboard.writeText(copyTarget).then(() => {
            showToast(`📋 已复制 [${identity.projectShort} #${identity.segmentNo}] 保存路径:\n${copyTarget}`, 3500);
          }).catch(() => {});
        });
      }
      tr.appendChild(seqTd);
      
      // 1. 图片预览与名称展示列
      const imgTd = document.createElement("td");
      imgTd.style.width = "115px";
      imgTd.style.textAlign = "center";
      
      const imgPreviewSrc = task.image?.src || (task.image?.primaryMediaKey?.startsWith("http") ? task.image.primaryMediaKey : "");
      const taskDisplayName = task.image?.displayName || "未命名素材";

      if (imgPreviewSrc) {
        setSafeHTML(imgTd, `
          <div class="cell-image-container" title="点击可更换本地图片预览" data-row="${rowIndex}">
            <div class="cell-image-box has-image">
              <img class="cell-image-preview" src="${imgPreviewSrc}" alt="预览" />
              <div class="cell-image-hover-mask">更换</div>
            </div>
            <span class="cell-image-name" title="${taskDisplayName}">${taskDisplayName}</span>
          </div>
        `);
      } else {
        setSafeHTML(imgTd, `
          <div class="cell-image-container" title="未关联图像，点击可直接选择本地图片预览" data-row="${rowIndex}">
            <div class="cell-image-box no-image">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
                <circle cx="8.5" cy="8.5" r="1.5"></circle>
                <polyline points="21 15 16 10 5 21"></polyline>
              </svg>
              <span class="cell-image-pick-text">选图</span>
            </div>
            <span class="cell-image-name" title="${taskDisplayName}">${taskDisplayName}</span>
          </div>
        `);
      }

      // 绑定点击本地选图/换图预览功能
      const imgContainer = imgTd.querySelector(".cell-image-container");
      if (imgContainer) {
        imgContainer.addEventListener("click", () => {
          const fileInput = document.createElement("input");
          fileInput.type = "file";
          fileInput.accept = "image/*";
          fileInput.style.setProperty("display", "none", "important");
          fileInput.onchange = (e) => {
            const file = e.target.files[0];
            if (file) {
              const reader = new FileReader();
              reader.onload = (loadEvt) => {
                if (!task.image) task.image = {};
                task.image.src = loadEvt.target.result;
                if (!task.image.displayName || task.image.displayName === "未命名素材" || task.image.displayName.startsWith("素材图片_")) {
                  task.image.displayName = file.name;
                }
                saveState();
                renderTable();
                showToast(`已成功为任务加载图片: ${file.name}`);
              };
              reader.readAsDataURL(file);
            }
          };
          document.body.appendChild(fileInput);
          fileInput.click();
          fileInput.remove();
        });
      }
      tr.appendChild(imgTd);

      // 2. 文本输入列
      const textTd = document.createElement("td");
      const textarea = document.createElement("textarea");
      textarea.className = "cell-textarea";
      textarea.value = task.text;
      textarea.placeholder = "分行输入提示词...";
      textarea.addEventListener("change", (e) => {
        task.text = e.target.value;
        const linesCount = splitTextToLines(task.text).length;
        task.status = extendArray(task.status, linesCount, "pending");
        task.message = extendArray(task.message, linesCount, "");
        task.downloaded = extendArray(task.downloaded, linesCount, false);
        saveState();
        renderTableStatusCell(tr, task, rowIndex);
      });
      textTd.appendChild(textarea);
      tr.appendChild(textTd);

      // 3. 模版下拉列
      const presetTd = document.createElement("td");
      const presetSelect = document.createElement("select");
      presetSelect.className = "cell-select";
      state.presets.forEach(p => {
        const opt = document.createElement("option");
        opt.value = p.name;
        opt.innerText = p.name;
        presetSelect.appendChild(opt);
      });
      presetSelect.value = task.preset || "无模版(直接填入)";
      presetSelect.addEventListener("change", (e) => {
        task.preset = e.target.value;
        saveState();
      });
      presetTd.appendChild(presetSelect);
      tr.appendChild(presetTd);

      // 4. 素材类型列
      const typeTd = document.createElement("td");
      const typeSelect = document.createElement("select");
      typeSelect.className = "cell-select";
      setSafeHTML(typeSelect, `
        <option value="VIDEO_FRAMES">帧模式 (默认)</option>
        <option value="VIDEO_REFERENCES">素材模式</option>
      `);
      typeSelect.value = task.footageType;
      typeSelect.addEventListener("change", (e) => {
        task.footageType = e.target.value;
        saveState();
      });
      typeTd.appendChild(typeSelect);
      tr.appendChild(typeTd);

      const durationTd = document.createElement("td");
      const durationInput = document.createElement("input");
      durationInput.type = "number";
      durationInput.min = "0.1";
      durationInput.step = "any";
      durationInput.className = "cell-select";
      durationInput.style.width = "70px";
      durationInput.value = task.duration ?? "";
      durationInput.placeholder = "自动";
      durationInput.title = "按此秒数提交；当前 Flow 模型不支持时会报错";
      durationInput.addEventListener("change", () => {
        try { task.duration = normalizeDuration(durationInput.value); saveState(); }
        catch (err) { showToast(err.message); durationInput.value = task.duration ?? ""; }
      });
      durationTd.appendChild(durationInput);
      tr.appendChild(durationTd);

      // 5. 生成数数量列
      const countTd = document.createElement("td");
      const countSelect = document.createElement("select");
      countSelect.className = "cell-select";
      countSelect.style.width = "60px";
      countSelect.style.minWidth = "60px";
      [1, 2, 3, 4].forEach(v => {
        const opt = document.createElement("option");
        opt.value = v;
        opt.innerText = v;
        countSelect.appendChild(opt);
      });
      countSelect.value = task.count;
      countSelect.addEventListener("change", (e) => {
        task.count = parseInt(e.target.value, 10);
        saveState();
      });
      countTd.appendChild(countSelect);
      tr.appendChild(countTd);

      // 6. 状态与日志列
      const statusTd = document.createElement("td");
      statusTd.className = "status-cell-container";
      tr.appendChild(statusTd);
      renderTableStatusCell(tr, task, rowIndex);

      // 7. 操作列 (单项运行 + 手动下载并重命名 + 删除)
      const actionTd = document.createElement("td");
      actionTd.style.textAlign = "center";
      actionTd.style.width = "155px";
      actionTd.style.whiteSpace = "nowrap";

      const runBtn = document.createElement("button");
      runBtn.className = "flow-btn primary";
      runBtn.style.cssText = "padding: 3px 7px; font-size: 11px; margin-right: 4px; border-radius: 4px; display: inline-flex; align-items: center; gap: 3px; background: #10b981; border: none; color: #fff; cursor: pointer;";
      runBtn.title = "仅运行当前这一项任务，生成并下载完毕后即停止";
      setSafeHTML(runBtn, `
        <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
        <span>单跑</span>
      `);
      runBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (batchPromise) {
          showToast("已有生成队列正在执行中，请先停止后再启动");
          return;
        }
        startBatch(rowIndex);
      });
      actionTd.appendChild(runBtn);

      const manualDlBtn = document.createElement("button");
      manualDlBtn.className = "flow-btn secondary";
      manualDlBtn.style.cssText = "padding: 3px 7px; font-size: 11px; margin-right: 4px; border-radius: 4px; display: inline-flex; align-items: center; gap: 3px; background: #3b82f6; border: none; color: #fff; cursor: pointer;";
      manualDlBtn.title = `手动下载并重命名为 ${identity.videoFileName} (${identity.projectShort})\n同时自动复制完整保存路径到剪贴板；按住 Shift 点击可直接打开页面视频选择列表`;
      setSafeHTML(manualDlBtn, `
        <span>📥 手动下载</span>
      `);
      manualDlBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        handleManualDownloadForTask(task, rowIndex, 0, !!e.shiftKey);
      });
      actionTd.appendChild(manualDlBtn);

      const delBtn = document.createElement("button");
      delBtn.className = "cell-btn-delete";
      delBtn.title = "单独删除此项任务配置";
      setSafeHTML(delBtn, `
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
        <span>删除</span>
      `);
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (batchPromise && rowIndex === state.currentIndex) {
          showToast("当前任务正在生成中，请先停止队列再删除该任务");
          return;
        }
        const taskName = `${identity.projectShort} #${identity.segmentNo} (${task.image?.displayName || ""})`;
        if (state.currentIndex > rowIndex) {
          state.currentIndex--;
        } else if (state.currentIndex === rowIndex) {
          state.currentLineIndex = 0;
        }
        state.tasks.splice(rowIndex, 1);
        renderTable();
        saveState();
        showToast(`已删除任务: ${taskName}`);
      });
      actionTd.appendChild(delBtn);
      tr.appendChild(actionTd);

      tbody.appendChild(tr);
    });
  }

  function renderTableStatusCell(rowElement, task, rowIndex) {
    const container = rowElement.querySelector(".status-cell-container");
    if (!container) return;
    
    container.replaceChildren();
    const lines = splitTextToLines(task.text);
    const identity = parseTaskIdentity(task, rowIndex);
    
    if (lines.length === 0) {
      setSafeHTML(container, `<span style="color: var(--flow-text-secondary); font-size:12px;">待输入文本</span>`);
      return;
    }

    const wrapper = document.createElement("div");
    wrapper.style.cssText = "display:flex; flex-direction:column; gap:6px; max-height:86px; overflow-y:auto; padding-right:4px;";
    
    lines.forEach((line, lineIdx) => {
      const status = task.status[lineIdx] || "pending";
      const message = task.message[lineIdx] || "";
      
      const lineRow = document.createElement("div");
      lineRow.className = "status-line-row";
      
      let badgeHtml = "";
      if (status === "pending") {
        badgeHtml = `<span class="status-badge pending"><span style="width:6px; height:6px; border-radius:50%; background:#f59e0b; display:inline-block; box-shadow:0 0 6px #f59e0b; flex-shrink:0;"></span>等待</span>`;
      } else if (status === "generating") {
        badgeHtml = `<span class="status-badge generating"><span class="spinner"></span>生成中</span>`;
      } else if (status === "success") {
        badgeHtml = `<span class="status-badge success">✓ 成功</span>`;
      } else if (status === "failed") {
        badgeHtml = `<span class="status-badge failed" title="${escapeHtmlAttr(message)}">✗ 失败</span>`;
      }

      const seqPrefix = lines.length > 1 ? `#${identity.segmentNo}-${lineIdx + 1}` : `#${identity.segmentNo}`;
      const hasBoundVideo = !!(task.mediaBindings?.[lineIdx]?.schema === 2 && Array.isArray(task.mediaBindings?.[lineIdx]?.mediaIds) && task.mediaBindings[lineIdx].mediaIds.length > 0);
      setSafeHTML(lineRow, `
        <span class="status-line-text" title="${escapeHtmlAttr(`[${identity.projectShort} ${seqPrefix}] ${message ? message + ' | ' : ''}${line}`)}">${escapeHtmlAttr(seqPrefix)}: ${escapeHtmlAttr(line)}</span>
        <div class="status-line-actions">
          ${badgeHtml}
          ${status !== "pending" && status !== "generating" ? `
            <button class="flow-btn secondary btn-line-reset" data-line="${lineIdx}">${hasBoundVideo && status === "failed" ? "重试下载" : "重置"}</button>
          ` : ""}
        </div>
      `);

      // 绑定行重置功能
      const resetBtn = lineRow.querySelector(".btn-line-reset");
      if (resetBtn) {
        resetBtn.addEventListener("click", (e) => {
          const lIdx = parseInt(e.currentTarget.dataset.line, 10);
          const previousStatus = task.status[lIdx];
          task.status[lIdx] = "pending";
          const retryDownload = previousStatus !== "success" && !!(task.mediaBindings?.[lIdx]?.schema === 2 && Array.isArray(task.mediaBindings?.[lIdx]?.mediaIds) && task.mediaBindings[lIdx].mediaIds.length > 0);
          task.message[lIdx] = retryDownload ? "等待重新下载已生成的视频" : "已重置等待生成";
          task.downloaded[lIdx] = false;
          if (!retryDownload && task.videoUrls) task.videoUrls[lIdx] = null;
          if (!retryDownload && task.downloadIds) task.downloadIds[lIdx] = null;
          if (!retryDownload && task.mediaBindings) task.mediaBindings[lIdx] = null;
          saveState();
          renderTableStatusCell(rowElement, task, rowIndex);
          if (retryDownload) startBatch();
        });
      }

      wrapper.appendChild(lineRow);
    });

    container.appendChild(wrapper);
  }

  // ----------------------------------------------------
  // 7. 后台防休眠保活引擎与页面遮罩清理
  // ----------------------------------------------------
  let keepAliveAudioCtx = null;
  let keepAliveOsc = null;

  function enableTabKeepAlive() {
    try {
      if (!keepAliveAudioCtx) {
        const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
        if (AudioCtxClass) {
          keepAliveAudioCtx = new AudioCtxClass();
          keepAliveOsc = keepAliveAudioCtx.createOscillator();
          const gain = keepAliveAudioCtx.createGain();
          gain.gain.value = 0.00001; // 人耳绝对静音 (几乎为 0)
          keepAliveOsc.connect(gain);
          gain.connect(keepAliveAudioCtx.destination);
          keepAliveOsc.start();
          if (keepAliveAudioCtx.state === "suspended") {
            keepAliveAudioCtx.resume();
          }
          console.log("[FlowUI] 已激活后台防休眠引擎，Chrome 将以最高优先级持续执行后台任务！");
        }
      } else if (keepAliveAudioCtx.state === "suspended") {
        keepAliveAudioCtx.resume();
      }
    } catch (e) {
      console.warn("[FlowUI] 启用音频防休眠提示:", e);
    }
  }

  function disableTabKeepAlive() {
    try {
      if (keepAliveOsc) {
        keepAliveOsc.stop();
        keepAliveOsc.disconnect();
        keepAliveOsc = null;
      }
      if (keepAliveAudioCtx) {
        keepAliveAudioCtx.close();
        keepAliveAudioCtx = null;
      }
      console.log("[FlowUI] 已停用后台防休眠引擎");
    } catch (e) {}
  }

  // 清理 Flow 页面上的历史弹窗、菜单、全屏预览与 backdrop，保证操作视口清空
  async function cleanupPageOverlays() {
    try {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      const backdrops = document.querySelectorAll(".cdk-overlay-backdrop");
      backdrops.forEach(bd => {
        try { bd.click(); } catch (_) {}
      });

      // 若意外打开了素材/大图详情面板，自动点击返回或关闭按钮退出大图模式（兼容简中/繁中/英文）
      const detailExitBtn = document.querySelector(
        "button[aria-label='返回'], button[aria-label='Back'], button[aria-label='关闭'], button[aria-label='關閉'], button[aria-label='Close'], .asset-detail-back, flow-asset-detail button:first-child"
      );
      if (detailExitBtn && !detailExitBtn.disabled) {
        console.log("[FlowUI] 检测到大图/素材详情面板，自动点击退出");
        try { detailExitBtn.click(); } catch (_) {}
      }

      const mainContainer = document.querySelector("flow-project-page, .project-container, body");
      if (mainContainer) {
        mainContainer.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      }
      await delay(200);
    } catch (e) {
      console.warn("[FlowUI] 清理页面遮罩提示:", e);
    }
  }

  let isStopRequested = false;

  let batchPromise = null;
  function startBatch(singleRowIndex = null) {
    if (batchPromise) return batchPromise;
    isStopRequested = false;
    document.querySelectorAll("#flow-tasks-table input, #flow-tasks-table textarea, #flow-tasks-table select, #flow-tasks-table button").forEach(el => el.disabled = true);
    document.getElementById("btn-start-batch")?.style.setProperty("display", "none", "important");
    document.getElementById("btn-stop-batch")?.style.setProperty("display", "inline-flex", "important");
    batchPromise = runBatch(singleRowIndex).catch(err => {
      console.error("[Batch] 队列异常", err);
      showToast(`队列停止：${err.message}`, 6000);
    }).finally(() => {
      try {
        onBatchFinished();
        document.querySelectorAll("#flow-tasks-table input, #flow-tasks-table textarea, #flow-tasks-table select, #flow-tasks-table button").forEach(el => el.disabled = false);
      } finally {
        batchPromise = null;
        window.__lzxFlowTransport?.batchIdle?.();
      }
    });
    return batchPromise;
  }

  // ====================================================
  // 7.5 Google Vids 专属逐个任务自动化生成与下载引擎 (兼容简中/繁中/英文)
  // ====================================================
  async function ensureVidsSidebarAndAnimateTab() {
    // 1. 若存在初始模板欢迎弹窗，自动点击“生成 AI 视频片段”或按 Escape 关闭
    const welcomeDialog = document.querySelector('#insertabletemplates-dialog-notforstyling[role="dialog"]');
    if (welcomeDialog && welcomeDialog.offsetParent !== null && getComputedStyle(welcomeDialog).display !== "none") {
      const videogenEntryBtn = welcomeDialog.querySelector("button.appsDocsGettingStartedEntryPointSelectionViewButton.videogen");
      if (videogenEntryBtn) {
        console.log("[Vids] 检测到欢迎弹窗，自动点击 [生成 AI 视频片段] 入口...");
        simulateClick(videogenEntryBtn);
      } else {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      }
      await delay(700);
    }

    // 2. 确保右侧“AI 视频片段”侧边栏处于打开状态（兼容简中/繁中/英文）
    const isSidebarOpen = () => {
      const sb = document.querySelector(".appsDocsAiGenerativeaiVideoUiSidebarWizVideogensidebarSidebarContent, .appsDocsAiGenerativeaiVideoUiSidebarWizVideogensidebarWideSideSheet");
      return !!(sb && sb.offsetParent !== null && sb.getBoundingClientRect().width > 100);
    };

    if (!isSidebarOpen()) {
      const railBtn = document.getElementById("content-library-rail-video-generation-element") ||
                      document.querySelector(
                        '[aria-label="生成 AI 视频片段"], [aria-label="產生 AI 影片片段"], [aria-label="生成 AI 影片片段"], ' +
                        '[aria-label*="AI 视频"], [aria-label*="AI 影片"], [aria-label*="AI video" i], ' +
                        '[data-tooltip="AI 视频片段"], [data-tooltip="AI 影片片段"], [data-tooltip*="AI video" i]'
                      );
      if (railBtn) {
        console.log("[Vids] 正在点击右侧边栏按钮打开 [AI 视频片段] 面板...");
        simulateClick(railBtn);
        for (let w = 0; w < 12; w++) {
          await delay(500);
          if (isSidebarOpen()) break;
        }
      }
    }

    // 3. 确保切换到“添加动画”/“加入動畫”/“Animate”标签页（或第 2 个 Tab）
    let animateTab = null;
    const animateRe = /(添加动画|加入動畫|新增動畫|加上動畫|動畫|动画|Animate|Add animation|Image)/i;
    for (let w = 0; w < 8; w++) {
      const tabs = Array.from(document.querySelectorAll('button.collapsiblePromptBoxTabButton[role="tab"], button.collapsiblePromptBoxTabButton, .collapsiblePromptBoxContainerWithTabs button[role="tab"]'));
      animateTab = tabs.find(b => animateRe.test(b.textContent || "") || animateRe.test(b.getAttribute("aria-label") || "")) ||
                   (tabs.length >= 2 ? tabs[1] : null);
      if (animateTab) break;
      await delay(400);
    }

    if (animateTab && animateTab.getAttribute("aria-selected") !== "true") {
      console.log("[Vids] 正在切换到 [添加动画 / 加入動畫 / Animate] 标签页...");
      simulateClick(animateTab);
      await delay(500);
    }
  }

  async function ensureVidsPromptBoxExpanded() {
    for (let attempt = 0; attempt < 6; attempt++) {
      const container = document.querySelector(".collapsiblePromptBoxContainerWithTabs");
      const toggleBtn = document.querySelector("button.collapsiblePromptBoxToggleExpansionButton");
      const genBtn = document.querySelector("button.collapsiblePromptBoxGenerateButton");
      const genBtnVisible = !!(genBtn && genBtn.offsetParent !== null && genBtn.getBoundingClientRect().height > 0);
      const isExpandedClass = container ? container.classList.contains("collapsiblePromptBoxContainerWithTabsExpanded") : true;
      const toggleLabel = toggleBtn ? (toggleBtn.getAttribute("aria-label") || "").trim() : "";

      if (isExpandedClass && genBtnVisible && !/^(展开|展開|Expand)$/i.test(toggleLabel)) {
        return true;
      }
      if (toggleBtn && toggleBtn.offsetParent !== null) {
        console.log(`[Vids] 提示词框当前处于收起状态 (aria-label="${toggleLabel}")，点击展开...`);
        simulateClick(toggleBtn);
        await delay(500);
      } else if (container) {
        simulateClick(container);
        await delay(500);
      } else {
        await delay(400);
      }
    }
    return true;
  }

  async function expandAndClearVidsPromptBox() {
    // 1. 先确保提示词框已展开（Vids 每次提交生成后会自动收起提示词框）
    await ensureVidsPromptBoxExpanded();
    await delay(250);

    // 2. 点击“清除”/“Clear”按钮（同时清除上一次的提示词与已上传图片）
    const clearRe = /(清除|清空|Clear)/i;
    const clearBtn = document.querySelector("button.promptTextAreaMenuBarClearLabel") ||
                     Array.from(document.querySelectorAll(".promptTextAreaMenuBarRightActions button, .promptTextAreaMenuBarV2Container button"))
                       .find(b => clearRe.test((b.textContent || "").trim()) || clearRe.test(b.getAttribute("data-tooltip") || "") || clearRe.test(b.getAttribute("aria-label") || ""));
    if (clearBtn && clearBtn.offsetParent !== null && !clearBtn.disabled && clearBtn.getAttribute("aria-disabled") !== "true") {
      console.log("[Vids] 点击 [清除] 按钮清空上一任务的提示词与图片...");
      simulateClick(clearBtn);
      await delay(500);
    }

    // 3. 保底检查：若仍有残留图片预览，点击“移除图片”/“移除圖片”/“Remove image”按钮
    const removeImgBtn = document.querySelector(
      'button.removeInputButtonButton, .removeInputButtonContainer button, ' +
      'button[aria-label="移除图片"], button[aria-label="移除圖片"], button[aria-label="移除圖像"], button[aria-label*="Remove" i]'
    );
    if (removeImgBtn && removeImgBtn.offsetParent !== null) {
      console.log("[Vids] 点击 [移除图片] 按钮清理残留图片...");
      simulateClick(removeImgBtn);
      await delay(400);
    }

    // 4. 保底检查：若输入框仍有残留文字，清空输入框
    const editor = document.querySelector("div.promptTextAreaPromptRichTextInputInternal[contenteditable='true'], [jsname='ZeIRi'][role='textbox']");
    if (editor && (editor.textContent || "").trim().length > 0) {
      try {
        editor.focus();
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(editor);
        sel.removeAllRanges();
        sel.addRange(range);
        document.execCommand("delete", false, null);
      } catch (_) {}
      if ((editor.textContent || "").trim().length > 0) {
        editor.textContent = "";
      }
      editor.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "deleteContentBackward" }));
      await delay(250);
    }
  }

  async function syncVidsVideoSettings({ aspectRatio, resolution, duration }) {
    await ensureVidsPromptBoxExpanded();
    const settingsBtn = document.querySelector(
      "button.appsDocsAiGenerativeaiVideoUiSidebarWizVideogencreationviewMenubuttonsMenubuttonGenerationSettingsButton, " +
      "button[aria-label^='生成设置'], button[aria-label^='生成設定'], button[aria-label^='產生設定'], button[aria-label^='Generation settings' i]"
    );
    if (!settingsBtn) {
      console.warn("[Vids] 未找到 Vids 生成设置按钮，跳过设置同步");
      return;
    }

    const wantPortrait = !String(aspectRatio || "").includes("16_9") && !String(aspectRatio || "").includes("16:9");
    const targetAspectRe = wantPortrait
      ? /(竖屏|豎屏|直向|直式|9:16|Portrait)/i
      : /(横屏|橫屏|横向|橫向|橫式|16:9|Landscape)/i;
    const targetAspectLabel = wantPortrait ? "竖屏/直向(9:16)" : "横屏/橫向(16:9)";
    const targetResLabel = String(resolution || "").includes("1080") ? "1080p" : "720p";
    const targetDuration = Math.min(10, Math.max(3, Math.round(Number(duration) || 8)));

    const ariaSummary = settingsBtn.getAttribute("aria-label") || "";
    const textSummary = (settingsBtn.textContent || "").replace(/\s+/g, "");
    const aspectAlreadyOk = targetAspectRe.test(ariaSummary) || targetAspectRe.test(textSummary);
    const resAlreadyOk = ariaSummary.includes(targetResLabel) || textSummary.includes(targetResLabel);
    const durRe = new RegExp(`(^|\\D)${targetDuration}\\s*(秒|s|sec|seconds)(\\D|$)`, "i");
    const durAlreadyOk = durRe.test(ariaSummary) || durRe.test(textSummary);

    if (aspectAlreadyOk && resAlreadyOk && durAlreadyOk) {
      console.log(`[Vids] 当前生成设置 (${ariaSummary}) 已符合目标要求，无需打开设置菜单`);
      return;
    }

    console.log(`[Vids] 同步生成设置: 目标比例=${targetAspectLabel}, 分辨率=${targetResLabel}, 时长=${targetDuration}s (当前=${ariaSummary})`);
    simulateClick(settingsBtn);
    await delay(500);

    const popover = document.querySelector(".appsDocsAiGenerativeaiVideoUiSidebarWizVideogencreationviewMenubuttonsMenubuttonPopoverContainer");
    if (popover) {
      // (a) 宽高比：横屏 / 竖屏（兼容简中/繁中/英文）
      const aspectBtns = Array.from(popover.querySelectorAll(
        ".appsDocsAiGenerativeaiVideoUiSidebarWizVideogencreationviewMenubuttonsMenubuttonAspectRatioButtonGroup button[role='radio'], button[role='radio']"
      ));
      const targetAspectBtn = aspectBtns.find(b => targetAspectRe.test(b.textContent || "") || targetAspectRe.test(b.getAttribute("aria-label") || ""));
      if (targetAspectBtn && targetAspectBtn.getAttribute("aria-checked") !== "true") {
        console.log(`[Vids] 切换宽高比 -> ${targetAspectLabel}`);
        simulateClick(targetAspectBtn);
        await delay(300);
      }

      // (b) 分辨率：720p / 1080p
      const resSection = popover.querySelector(".appsDocsAiGenerativeaiVideoUiSidebarWizVideogencreationviewMenubuttonsMenubuttonResolutionSection");
      if (resSection) {
        const resDropBtn = resSection.querySelector("button");
        if (resDropBtn && !(resDropBtn.textContent || "").includes(targetResLabel)) {
          simulateClick(resDropBtn);
          await delay(350);
          const menuItems = Array.from(document.querySelectorAll("li[role='menuitemradio'], [role='menuitemradio']"));
          const targetItem = menuItems.find(li => (li.textContent || "").includes(targetResLabel));
          if (targetItem) {
            console.log(`[Vids] 切换分辨率 -> ${targetResLabel}`);
            simulateClick(targetItem);
            await delay(350);
          }
        }
      }

      // (c) 视频时长滑块 (3s - 10s)
      const sliderInput = popover.querySelector("input.javascriptMaterialdesignGm3WizSlider-slider__input[type='range'], input[type='range']");
      if (sliderInput && Number(sliderInput.value) !== targetDuration) {
        console.log(`[Vids] 调整视频时长滑块: ${sliderInput.value}s -> ${targetDuration}s`);
        try {
          const track = popover.querySelector(".javascriptMaterialdesignGm3WizSlider-slider__track");
          if (track) {
            const rect = track.getBoundingClientRect();
            if (rect.width > 20) {
              const ratio = (targetDuration - 3) / (10 - 3);
              const clientX = Math.round(rect.left + ratio * rect.width);
              const clientY = Math.round(rect.top + rect.height / 2);
              const pInit = { bubbles: true, cancelable: true, view: window, clientX, clientY, pointerId: 1, isPrimary: true };
              track.dispatchEvent(new PointerEvent("pointerdown", pInit));
              track.dispatchEvent(new PointerEvent("pointermove", pInit));
              track.dispatchEvent(new PointerEvent("pointerup", pInit));
              track.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window, clientX, clientY }));
              await delay(200);
            }
          }
          const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
          if (nativeSetter) {
            nativeSetter.call(sliderInput, String(targetDuration));
          } else {
            sliderInput.value = String(targetDuration);
          }
          sliderInput.setAttribute("value", String(targetDuration));
          sliderInput.setAttribute("aria-valuetext", `${targetDuration} 秒`);
          sliderInput.dispatchEvent(new Event("focus", { bubbles: true }));
          sliderInput.dispatchEvent(new Event("input", { bubbles: true }));
          sliderInput.dispatchEvent(new Event("change", { bubbles: true }));
          sliderInput.dispatchEvent(new Event("blur", { bubbles: true }));
          await delay(300);
        } catch (sErr) {
          console.warn("[Vids] 调整时长滑块异常:", sErr);
        }
      }
    }

    // 关闭设置菜单
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
    await delay(250);
  }

  function hasVidsUploadedImagePreview() {
    const img = document.querySelector("img.inputPreviewItemPreview, .inputPreviewItemPreviewContainer img");
    if (!img || img.offsetParent === null) return false;
    const src = img.getAttribute("src") || img.src || "";
    return src.length > 10;
  }

  function getVidsDropTargetPoint() {
    const candidates = [
      document.querySelector(".promptTextAreaPromptRichTextInput"),
      document.querySelector(".videoGenCreationViewFileInputsContainerV2"),
      document.querySelector(".collapsiblePromptBoxTabPanelContent"),
      document.querySelector(".collapsiblePromptBoxContainerWithTabs")
    ].filter(Boolean);

    for (const el of candidates) {
      const r = el.getBoundingClientRect();
      if (r.width > 50 && r.height > 20 && r.top >= 0 && r.bottom <= window.innerHeight) {
        return {
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + Math.min(r.height / 2, 48))
        };
      }
    }
    return { x: Math.round(window.innerWidth - 200), y: Math.round(window.innerHeight - 220) };
  }

  async function uploadSingleImageToVids(task) {
    const localPath = String(task.image?.localImagePath || task.local_image_path || "").trim();
    const imgSrc = String(task.image?.src || "").trim();
    const imgName = task.image?.displayName || getFilenameFromPath(localPath) || "image.png";

    if (!localPath && !imgSrc) {
      console.log("[Vids] 当前任务未指定图片，跳过图片上传");
      return true;
    }

    await ensureVidsPromptBoxExpanded();

    // 策略 1：使用后台 Chrome Debugger 原生文件拖放 (Input.dispatchDragEvent) 把本地路径图片直接拖入 Vids 提示词框
    if (localPath && /^([a-zA-Z]:[\\/]|\/|\\\\)/.test(localPath)) {
      for (let attempt = 0; attempt < 2; attempt++) {
        if (isStopRequested) return false;
        const pt = getVidsDropTargetPoint();
        console.log(`[Vids] 正在通过原生拖放上传本地图片 (尝试 ${attempt + 1}/2) @(${pt.x}, ${pt.y}): ${localPath}`);
        setOverlayHiddenForUpload(true);
        await delay(150);
        let res = null;
        try {
          res = await sendToExtension("nativeFileDrag", { paths: [localPath], x: pt.x, y: pt.y });
        } finally {
          setOverlayHiddenForUpload(false);
        }

        if (res && res.ok !== false && !res.error) {
          for (let w = 0; w < 18; w++) {
            if (isStopRequested) return false;
            await delay(400);
            if (hasVidsUploadedImagePreview()) {
              console.log(`[Vids] ✅ 本地图片上传成功，已检测到预览缩略图: ${imgName}`);
              await delay(600);
              return true;
            }
          }
        } else {
          console.warn("[Vids] 原生文件拖放返回异常:", res?.error);
        }
      }
    }

    // 策略 2：若存在 Base64 / Blob / 网络图片 src，通过 ClipboardEvent('paste') 与 DragEvent('drop') 注入
    if (imgSrc) {
      try {
        console.log("[Vids] 尝试通过剪贴板 Paste / Drop 注入图片数据...");
        let blob = null;
        if (imgSrc.startsWith("data:image/")) {
          const byteString = atob(imgSrc.split(",")[1]);
          const mimeString = imgSrc.split(",")[0].split(":")[1].split(";")[0];
          const ab = new ArrayBuffer(byteString.length);
          const ia = new Uint8Array(ab);
          for (let i = 0; i < byteString.length; i++) ia[i] = byteString.charCodeAt(i);
          blob = new Blob([ab], { type: mimeString });
        } else {
          const resp = await fetch(imgSrc);
          blob = await resp.blob();
        }
        if (blob) {
          const file = new File([blob], imgName, { type: blob.type || "image/png" });
          const dt = new DataTransfer();
          dt.items.add(file);

          const editor = document.querySelector("div.promptTextAreaPromptRichTextInputInternal[contenteditable='true']") ||
                         document.querySelector(".promptTextAreaPromptRichTextInput");
          if (editor) {
            editor.focus();
            editor.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt }));
            editor.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
          }
          for (let w = 0; w < 12; w++) {
            await delay(400);
            if (hasVidsUploadedImagePreview()) {
              console.log("[Vids] ✅ Paste/Drop 图片上传成功！");
              await delay(500);
              return true;
            }
          }
        }
      } catch (e) {
        console.warn("[Vids] Paste/Drop 注入图片失败:", e);
      }
    }

    return hasVidsUploadedImagePreview();
  }

  async function fillVidsPromptAndSubmit(finalPrompt) {
    await ensureVidsPromptBoxExpanded();
    const editor = document.querySelector("div.promptTextAreaPromptRichTextInputInternal[contenteditable='true']") ||
                   document.querySelector("[jsname='ZeIRi'][role='textbox']");
    if (!editor) {
      throw new Error("未找到 Google Vids 提示词输入框，请确认右侧 [AI 视频片段] 侧边栏已打开");
    }

    editor.focus();
    simulateClick(editor);
    await delay(150);

    let injected = false;
    try {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(editor);
      sel.removeAllRanges();
      sel.addRange(range);
      injected = document.execCommand("insertText", false, finalPrompt);
    } catch (e) {
      console.warn("[Vids] execCommand 注入提示词异常:", e);
    }

    if (!injected || (editor.textContent || "").trim() !== finalPrompt.trim()) {
      editor.textContent = finalPrompt;
    }
    editor.classList.remove("promptTextAreaPromptRichTextInputInternalIsEmpty");

    editor.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: finalPrompt }));
    editor.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText", data: finalPrompt }));
    editor.dispatchEvent(new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true }));
    editor.dispatchEvent(new Event("change", { bubbles: true }));
    await delay(400);

    // 等待生成按钮可用并点击（兼容简中/繁中/英文，严格排除顶部 Tab 按钮如「建立」）
    const findGenBtn = () => {
      const byClass = document.querySelector(
        "button.collapsiblePromptBoxGenerateButton, " +
        "button[data-idom-class*='collapsiblePromptBoxGenerateButton'], " +
        ".promptTextAreaMenuBarRightActions button.collapsiblePromptBoxGenerateButton, " +
        ".promptTextAreaMenuBarRightActions button:not(.promptTextAreaMenuBarClearLabel)"
      );
      if (byClass) return byClass;

      const touchDiv = document.querySelector(
        "button.collapsiblePromptBoxGenerateButton .javascriptMaterialdesignGm3WizIconButtonFilled-icon-button__touch, " +
        ".promptTextAreaMenuBarRightActions .javascriptMaterialdesignGm3WizIconButtonFilled-icon-button__touch, " +
        ".promptTextAreaMenuBarV2Container .javascriptMaterialdesignGm3WizIconButtonFilled-icon-button__touch"
      );
      if (touchDiv) {
        const parentBtn = touchDiv.closest("button");
        if (parentBtn) return parentBtn;
      }

      const candidates = Array.from(document.querySelectorAll(
        "button[aria-label='生成'], button[aria-label='產生'], button[aria-label='傳送'], button[aria-label='发送'], button[aria-label='Generate' i]"
      ));
      return candidates.find(b =>
        b.getAttribute("role") !== "tab" &&
        !b.classList.contains("collapsiblePromptBoxTabButton") &&
        !b.closest("[role='tablist']")
      ) || null;
    };

    let genBtn = null;
    for (let i = 0; i < 20; i++) {
      if (isStopRequested) throw new Error("用户已停止任务");
      genBtn = findGenBtn();
      if (genBtn && !genBtn.disabled && !genBtn.hasAttribute("disabled") && genBtn.getAttribute("aria-disabled") !== "true") {
        break;
      }
      if (editor) {
        editor.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText", data: finalPrompt }));
      }
      genBtn = null;
      await delay(350);
    }

    if (!genBtn) {
      genBtn = findGenBtn();
      if (!genBtn) throw new Error("未找到 Google Vids [生成] 按钮");
      genBtn.removeAttribute("disabled");
      genBtn.disabled = false;
      genBtn.setAttribute("aria-disabled", "false");
    }

    const touchTarget = genBtn.querySelector(".javascriptMaterialdesignGm3WizIconButtonFilled-icon-button__touch") || genBtn;
    console.log("[Vids] 🚀 点击 [生成] 按钮提交当前任务...", { genBtn, touchTarget });
    const rect = touchTarget.getBoundingClientRect();
    const cx = rect.width > 0 ? Math.round(rect.left + rect.width / 2) : 0;
    const cy = rect.height > 0 ? Math.round(rect.top + rect.height / 2) : 0;
    try {
      if (typeof PointerEvent !== "undefined") {
        touchTarget.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, pointerId: 1, isPrimary: true, button: 0, buttons: 1 }));
        touchTarget.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, pointerId: 1, isPrimary: true, button: 0, buttons: 0 }));
      }
      touchTarget.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1 }));
      touchTarget.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 0 }));
    } catch (_) {}
    if (typeof touchTarget.click === "function") {
      touchTarget.click();
    } else {
      genBtn.click();
    }
    await delay(450);

    // 保底 1：若 450ms 后按钮仍处于可用未提交状态且提示词框仍展开，直接在 button 本体上补发一次点击
    let stillBtn = findGenBtn();
    let stillExpanded = document.querySelector(".collapsiblePromptBoxContainerWithTabsExpanded");
    if (stillBtn && stillExpanded && !stillBtn.disabled && !stillBtn.hasAttribute("disabled") && stillBtn.getAttribute("aria-disabled") !== "true") {
      console.log("[Vids] 🔁 检测到 touch 层点击后按钮仍未进入提交态，在 button 本体补发 click...");
      stillBtn.click();
      await delay(450);
    }

    // 保底 2：若仍未提交，调用后台 chrome.debugger 发出真实硬件鼠标点击 (isTrusted=true)
    stillBtn = findGenBtn();
    stillExpanded = document.querySelector(".collapsiblePromptBoxContainerWithTabsExpanded");
    if (stillBtn && stillExpanded && !stillBtn.disabled && !stillBtn.hasAttribute("disabled") && stillBtn.getAttribute("aria-disabled") !== "true" && cx > 0 && cy > 0) {
      console.log(`[Vids] 🖱️ 启用内核级硬件点击 (nativeClick) 提交 @(${cx}, ${cy})...`);
      try {
        await sendToExtension("nativeClick", { x: cx, y: cy });
      } catch (_) {}
      await delay(400);
    }
  }

  async function runVidsBatch(singleRowIndex = null) {
    state.isSuspended = false;
    enableTabKeepAlive();

    const validTasks = state.tasks.filter(t => splitTextToLines(t.text).length > 0);
    if (!validTasks.length) {
      alert("无可执行生成任务！(任务列表为空或提示词未填)");
      return;
    }

    if (isStopRequested) return;
    state.isRunning = true;
    document.getElementById("btn-start-batch")?.style.setProperty("display", "none", "important");
    document.getElementById("btn-stop-batch")?.style.setProperty("display", "inline-flex", "important");

    // 在 Vids 模式下自动收起全屏面板遮罩，方便右侧侧边栏交互与实时查看生成过程
    closePanel();

    const isSingle = singleRowIndex !== null && typeof singleRowIndex === "number";
    showToast(isSingle ? `🎬 [Vids] 开始执行单项任务 #${singleRowIndex + 1}...` : `🎬 [Vids] 开始逐个任务生成队列 (共 ${state.tasks.length} 项)...`, 4000);

    // 1. 打开右侧 AI 视频片段侧边栏并切换到“添加动画”
    await ensureVidsSidebarAndAnimateTab();

    const startRow = isSingle ? singleRowIndex : (state.currentIndex || 0);
    const endRow = isSingle ? (singleRowIndex + 1) : state.tasks.length;

    for (let rIdx = startRow; rIdx < endRow; rIdx++) {
      state.currentIndex = rIdx;
      await saveState();

      const task = state.tasks[rIdx];
      const lines = splitTextToLines(task.text);
      if (!lines.length) continue;
      if (!task.image) task.image = { displayName: `任务_${rIdx + 1}`, primaryMediaKey: "" };

      for (let lIdx = state.currentLineIndex || 0; lIdx < lines.length; lIdx++) {
        state.currentLineIndex = lIdx;
        await saveState();

        if (isStopRequested) {
          showToast("🎬 用户中断了 Vids 批量任务！");
          onBatchFinished();
          return;
        }

        if (task.status[lIdx] === "success" || (Array.isArray(task.downloaded) && task.downloaded[lIdx])) {
          continue;
        }

        const lineText = lines[lIdx];
        const duration = normalizeDuration(task.duration, window.__lzxFlowTransport ? null : calculateDuration(lineText));
        const preset = state.presets.find(p => p.name === task.preset);
        const parsedPreset = parsePreset(preset?.content);
        const finalPrompt = `${parsedPreset.before || ""}\n${lineText || ""}\n${parsedPreset.after || ""}`.trim();

        const targetDownloadPath = resolveDownloadPath(task.download_path, {
          image: task.image,
          rowIndex: rIdx,
          lineIndex: lIdx
        });

        updateLineStatus(rIdx, lIdx, "pending", "正在展开并清空 Vids 输入框...");
        showToast(`🎬 [Vids ${rIdx + 1}/${state.tasks.length}] 准备任务: ${task.image?.displayName || `#${rIdx + 1}`}`, 3500);

        // Step A: 确保侧边栏与“添加动画”标签页打开，点击“展开”并点击“清除”
        await ensureVidsSidebarAndAnimateTab();
        await expandAndClearVidsPromptBox();

        // Step B: 同步生成设置 (宽高比、分辨率、时长 3-10s)
        try {
          await syncVidsVideoSettings({
            aspectRatio: task.aspectRatio || state.settings.aspectRatio,
            resolution: task.resolution || state.settings.resolution,
            duration: duration
          });
        } catch (settErr) {
          console.warn("[Vids] 同步生成设置提示:", settErr);
        }

        // Step C: 上传当前任务的单张图片
        if (task.image && (task.image.localImagePath || task.local_image_path || task.image.src)) {
          updateLineStatus(rIdx, lIdx, "pending", `正在上传图片: ${task.image.displayName || ""}...`);
          showToast(`📤 [Vids ${rIdx + 1}/${state.tasks.length}] 正在上传图片: ${task.image.displayName || ""}`, 3500);
          const upOk = await uploadSingleImageToVids(task);
          if (!upOk) {
            updateLineStatus(rIdx, lIdx, "failed", "图片上传到 Vids 失败或超时，未检测到图片预览");
            showToast(`❌ [Vids ${rIdx + 1}/${state.tasks.length}] 图片上传失败，跳过本项`, 5000);
            continue;
          }
        }

        // Step D: 拍摄提交前已有视频与错误卡片快照
        const existingGenIds = new Set(
          Array.from(document.querySelectorAll("[data-generation-id]"))
            .map(el => el.getAttribute("data-generation-id"))
            .filter(Boolean)
        );
        const existingVideoUrls = new Set(
          Array.from(document.querySelectorAll(".appsDocsAiGenerativeaiVideoUiSidebarWizVideogenerationthumbnailsContainer video, video"))
            .map(v => (v.getAttribute("src") || v.currentSrc || v.src || "").trim().replace(/&amp;/g, "&"))
            .filter(Boolean)
        );
        const existingFailedEls = new Set(
          Array.from(document.querySelectorAll(".appsDocsAiGenerativeaiVideoUiSidebarWizFailedvideogenerationthumbnailContainer"))
        );

        // Step E: 输入提示词并点击“生成”
        const submissionId = crypto.randomUUID();
        task.taskId = task.taskId || crypto.randomUUID();
        task.mediaBindings = task.mediaBindings || [];
        task.downloadIds = task.downloadIds || [];
        task.downloadIds[lIdx] = null;
        task.videoUrls = task.videoUrls || [];
        task.videoUrls[lIdx] = null;
        task.mediaBindings[lIdx] = {
          schema: 2,
          submissionId,
          taskId: task.taskId,
          mediaIds: [],
          readyIds: [],
          downloadPath: targetDownloadPath
        };
        await saveState();

        try {
          updateLineStatus(rIdx, lIdx, "generating", "正在填入提示词并点击生成...");
          await fillVidsPromptAndSubmit(finalPrompt);
          updateLineStatus(rIdx, lIdx, "generating", "已提交到 Google Vids，等待视频生成完成...");
          showToast(`🚀 [Vids ${rIdx + 1}/${state.tasks.length}] 已提交生成，正在等待视频渲染完成...`, 4000);
        } catch (subErr) {
          console.error("[Vids] 提交任务失败:", subErr);
          updateLineStatus(rIdx, lIdx, "failed", `提交失败: ${subErr.message}`);
          continue;
        }

        // Step F: 轮询等待新视频出现并下载完成
        const maxWaitSec = Math.max(180, Number(state.settings.maxWaitTime) || 360);
        const waitStartTime = Date.now();
        let isTaskCompleted = false;
        let downloadTriggered = false;

        while ((Date.now() - waitStartTime) / 1000 < maxWaitSec) {
          if (isStopRequested) break;
          const waitedSec = Math.round((Date.now() - waitStartTime) / 1000);

          // 1. 检查是否已有下载任务完成
          if (task.downloadIds?.[lIdx] && !task.downloaded[lIdx]) {
            const dlState = await sendToExtension("getDownloadStatus", { downloadId: task.downloadIds[lIdx] });
            if (dlState?.state === "complete") {
              task.downloaded[lIdx] = true;
              updateLineStatus(rIdx, lIdx, "success", `自动下载成功: ${targetDownloadPath}`);
              isTaskCompleted = true;
              break;
            } else if (dlState?.state === "interrupted") {
              updateLineStatus(rIdx, lIdx, "failed", `下载中断: ${dlState.error || "未知原因"}`);
              break;
            }
          }

          if (task.status[lIdx] === "success" && task.downloaded?.[lIdx]) {
            isTaskCompleted = true;
            break;
          }
          if (task.status[lIdx] === "failed") {
            break;
          }

          // 2. 检查是否新增了生成失败提示卡片（如违反条款拦截）
          if (!downloadTriggered) {
            const currentFailedEls = Array.from(document.querySelectorAll(".appsDocsAiGenerativeaiVideoUiSidebarWizFailedvideogenerationthumbnailContainer"));
            const newFailedEl = currentFailedEls.find(el => !existingFailedEls.has(el));
            if (newFailedEl) {
              const errMsgEl = newFailedEl.querySelector(".appsDocsAiGenerativeaiVideoUiSidebarWizFailedvideogenerationthumbnailErrorMessage");
              const errMsg = (errMsgEl ? errMsgEl.textContent : newFailedEl.textContent || "视频生成被拦截或失败").trim();
              console.warn(`[Vids] ❌ 检测到 Vids 生成失败卡片: ${errMsg}`);
              updateLineStatus(rIdx, lIdx, "failed", `Vids 生成失败: ${errMsg}`);
              showToast(`⚠️ [Vids ${rIdx + 1}/${state.tasks.length}] 生成失败: ${errMsg.substring(0, 45)}`, 5000);
              break;
            }
          }

          // 3. 扫描侧边栏视频列表，寻找新生成的 <video>
          if (!downloadTriggered) {
            let foundVideoUrl = null;
            let foundGenId = null;

            const thumbContainers = Array.from(document.querySelectorAll(
              ".appsDocsAiGenerativeaiVideoUiSidebarWizSuccessfulvideogenerationthumbnailVideoGenerationThumbnailWithPromptContainer[data-generation-id], " +
              ".appsDocsAiGenerativeaiVideoUiSidebarWizVideogenerationthumbnailsContainer [data-generation-id]"
            ));

            for (const c of thumbContainers) {
              const gId = c.getAttribute("data-generation-id") || "";
              const v = c.querySelector("video");
              const rawSrc = v ? (v.getAttribute("src") || v.currentSrc || v.src || "") : "";
              const cleanSrc = rawSrc.trim().replace(/&amp;/g, "&");
              if (cleanSrc && cleanSrc.startsWith("https://")) {
                if ((gId && !existingGenIds.has(gId)) || !existingVideoUrls.has(cleanSrc)) {
                  foundVideoUrl = cleanSrc;
                  foundGenId = gId || `vids_${Date.now()}`;
                  break;
                }
              }
            }

            // 兜底：直接扫描侧边栏内所有 video 标签
            if (!foundVideoUrl) {
              const allVideos = Array.from(document.querySelectorAll(
                ".appsDocsAiGenerativeaiVideoUiSidebarWizVideogenerationthumbnailsContainer video, " +
                "video.appsDocsAiGenerativeaiVideoUiSidebarWizSuccessfulvideogenerationthumbnailVideoGenerationThumbnail"
              ));
              for (const v of allVideos) {
                const rawSrc = v.getAttribute("src") || v.currentSrc || v.src || "";
                const cleanSrc = rawSrc.trim().replace(/&amp;/g, "&");
                if (cleanSrc && cleanSrc.startsWith("https://") && !existingVideoUrls.has(cleanSrc) && !downloadedUrlsSet.has(cleanSrc)) {
                  const parentGenId = v.closest("[data-generation-id]")?.getAttribute("data-generation-id");
                  foundVideoUrl = cleanSrc;
                  foundGenId = parentGenId || `vids_${Date.now()}`;
                  break;
                }
              }
            }

            if (foundVideoUrl && foundGenId) {
              console.log(`[Vids] 🎯 检测到新生成的视频! generationId=${foundGenId}, URL=${foundVideoUrl.substring(0, 100)}`);
              downloadedUrlsSet.add(foundVideoUrl);
              existingVideoUrls.add(foundVideoUrl);
              existingGenIds.add(foundGenId);

              const binding = task.mediaBindings[lIdx];
              binding.mediaIds = [foundGenId];
              binding.readyIds = [foundGenId];
              binding.urls = { [foundGenId]: foundVideoUrl };
              task.videoUrls[lIdx] = foundVideoUrl;
              await saveState();

              const dlRes = await sendToExtension("download", {
                url: foundVideoUrl,
                filename: targetDownloadPath,
                task: {
                  rowIndex: rIdx,
                  lineIndex: lIdx,
                  taskId: task.taskId,
                  submissionId: binding.submissionId,
                  mediaId: foundGenId,
                  projectId: getStorageKey(),
                  download_path: targetDownloadPath,
                  isVids: true
                }
              });

              if (dlRes?.success) {
                downloadTriggered = true;
                task.downloadIds[lIdx] = dlRes.downloadId;
                updateLineStatus(rIdx, lIdx, "generating", `视频已生成，正在下载保存至 ${targetDownloadPath}...`);
                showToast(`📥 [Vids ${rIdx + 1}/${state.tasks.length}] 视频生成完成，正在下载: ${targetDownloadPath}`, 3500);
                await saveState();
              } else {
                updateLineStatus(rIdx, lIdx, "failed", `视频下载失败: ${dlRes?.error || "未知错误"}`);
                break;
              }
            } else {
              updateLineStatus(rIdx, lIdx, "generating", `Google Vids 正在生成视频 (${waitedSec}s)...`);
            }
          }

          await delay(2000);
        }

        if (!isTaskCompleted && !isStopRequested && task.status[lIdx] !== "failed") {
          updateLineStatus(rIdx, lIdx, "failed", `Vids 生成或下载超时 (已等待 ${maxWaitSec} 秒)`);
        }

        state.currentLineIndex = 0;
        await saveState();

        if (isStopRequested) {
          showToast("🎬 用户中断了 Vids 批量任务！");
          onBatchFinished();
          return;
        }

        if (isSingle) {
          showToast(`🎬 [Vids] 单项任务 #${rIdx + 1} 执行完成！`, 4000);
          onBatchFinished();
          return;
        }

        // 任务间隔等待 (严格遵循软件规则设置或面板设置，支持 0 秒及任意秒数)
        const rawInterval = (task.interval !== undefined && task.interval !== null) ? Number(task.interval) : Number(state.settings.interval);
        const intervalSec = (Number.isFinite(rawInterval) && rawInterval >= 0) ? rawInterval : 5;
        const intervalRand = (task.interval !== undefined && task.interval !== null) ? 0 : Math.max(0, Number(state.settings.intervalRandom) || 0);
        const waitTime = Math.round(intervalSec + Math.random() * intervalRand);
        if (waitTime > 0) {
          const targetEndTime = Date.now() + waitTime * 1000;
          while (Date.now() < targetEndTime) {
            if (isStopRequested) break;
            const rem = Math.max(1, Math.round((targetEndTime - Date.now()) / 1000));
            showToast(`✅ [Vids ${rIdx + 1}/${state.tasks.length}] 已完成！${rem} 秒后自动展开清空并开始下一项...`, 1500);
            await delay(1000);
          }
        }
      }
    }

    showToast("🎉 [Vids] 所有队列任务的逐个生成与自动下载已全部完成！", 6000);
    onBatchFinished();
  }

  async function runBatch(singleRowIndex = null) {
    if (isVidsPage()) {
      return await runVidsBatch(singleRowIndex);
    }
    state.isSuspended = false;
    if (alarmDialogEl) alarmDialogEl.style.setProperty("display", "none", "important");
    if (alarmTimer) {
      clearInterval(alarmTimer);
      alarmTimer = null;
    }
    
    // 激活后台防休眠引擎，保证失焦/后台平稳高频运行
    enableTabKeepAlive();

    // 启动安全限制监测
    startActivityCheck();

    // 校验页面生成组件是否就绪 (ProseMirror 富文本框与生成按钮)
    let editor = document.querySelector("flow-rich-text-editor div.ProseMirror") || document.querySelector("div.ProseMirror[contenteditable='true']");
    let genBtn = findFlowGenerateButton();

    if (!editor || !genBtn) {
      console.log("[FlowUI] 正在等待页面 ProseMirror 提示词框及生成按钮就绪...");
      for (let w = 0; w < 6; w++) {
        await delay(500);
        editor = document.querySelector("flow-rich-text-editor div.ProseMirror") || document.querySelector("div.ProseMirror[contenteditable='true']");
        genBtn = findFlowGenerateButton();
        if (editor && genBtn) break;
      }
    }

    if (!editor || !genBtn) {
      if (!location.href.includes("/project/")) {
        alert("请先进入具体的 Flow 项目页面 (例如 https://flow.google.com/project/...) 再启动批量生成！");
        return;
      }
      console.warn("[FlowUI] 提示词框或生成按钮未完全定位，将在执行时进行动态查找...");
    }

    const validTasks = state.tasks.filter(t => splitTextToLines(t.text).length > 0);
    if (!validTasks.length) {
      alert("无可执行生成任务！(任务列表为空或提示词未填)");
      return;
    }

    if (isStopRequested) return;

    // 生成前自动把缺失的本地图片上传到 Flow（已存在的图片会自动跳过）
    try {
      const isSingleRun = singleRowIndex !== null && typeof singleRowIndex === "number";
      const needUpload = getTasksNeedingUpload(isSingleRun ? singleRowIndex : null);
      if (needUpload.length) {
        console.log(`[Batch] 检测到 ${needUpload.length} 张图片尚未上传到 Flow，先执行原生拖放上传`);
        const up = await uploadLocalImagesToFlow({ rowFilter: isSingleRun ? singleRowIndex : null, silent: true });
        if (!up.ok && up.uploaded === 0 && up.missing.length) {
          showToast(`⚠️ 图片未能上传到 Flow，相关任务可能失败：${up.missing.slice(0, 3).join("、")}`, 6000);
        }
      }
    } catch (upErr) {
      console.warn("[Batch] 自动上传本地图片异常（继续执行生成）:", upErr);
    }
    if (isStopRequested) return;
    state.isRunning = true;
    
    document.getElementById("btn-start-batch").style.setProperty("display", "none", "important");
    document.getElementById("btn-stop-batch").style.setProperty("display", "inline-flex", "important");
    
    const isSingle = singleRowIndex !== null && typeof singleRowIndex === "number";
    showToast(isSingle ? `开始执行单项任务 #${singleRowIndex + 1}...` : "开始自动化批量生成队列...");
    
    const intervalSec = state.settings.interval;
    const intervalRand = state.settings.intervalRandom;

    const startRow = isSingle ? singleRowIndex : (state.currentIndex || 0);
    const endRow = isSingle ? (singleRowIndex + 1) : state.tasks.length;

    // 读取存储中的断点索引继续运行，实现断点续跑
    for (let rIdx = startRow; rIdx < endRow; rIdx++) {
      state.currentIndex = rIdx;
      await saveState();

      let task = state.tasks[rIdx];
      const lines = splitTextToLines(task.text);
      if (!lines.length) continue;
      if (!task.image) task.image = { displayName: `任务_${rIdx + 1}`, primaryMediaKey: "" };

      for (let lIdx = state.currentLineIndex || 0; lIdx < lines.length; lIdx++) {
        task = state.tasks[rIdx] || task;
        state.currentLineIndex = lIdx;
        await saveState();

        if (isStopRequested) {
          showToast("用户中断了批量任务！");
          onBatchFinished();
          return;
        }

        // 跳过已经成功的行
        if (task.status[lIdx] === "success" || (Array.isArray(task.downloaded) && task.downloaded[lIdx])) continue;

        state.activeTasksQueue = [];
        state.taskRecord = {};
        window.currentProcess = null;
        snapshotCurrentDomMedia();
        const lineText = lines[lIdx];
        // 允许单个任务有设定好的 duration，否则动态计算
        const duration = normalizeDuration(task.duration, window.__lzxFlowTransport ? null : calculateDuration(lineText));
        const preset = state.presets.find(p => p.name === task.preset);
        const parsedPreset = parsePreset(preset?.content);

        updateLineStatus(rIdx, lIdx, "pending", "排队准备提交...");
        
        let submitOk = false;
        if (task.mediaBindings?.[lIdx]?.schema === 2 && Array.isArray(task.mediaBindings?.[lIdx]?.mediaIds) && task.mediaBindings[lIdx].mediaIds.length > 0) {
          submitOk = await retrySavedVideo(task, rIdx, lIdx);
        } else if (task.mediaBindings?.[lIdx]) {
          task.mediaBindings[lIdx] = null;
        }
        for (let subTry = 0; (window.__lzxFlowTransport ? task.mediaBindings?.[lIdx]?.schema !== 2 : !(task.mediaBindings?.[lIdx]?.schema === 2 && task.mediaBindings?.[lIdx]?.mediaIds?.length > 0)) && subTry < (window.__lzxFlowTransport ? 1 : 2); subTry++) {
          try {
            if (isStopRequested) return;
            console.log(`[Batch] 自动提交: Row=${rIdx}, Line=${lIdx}, Text=${lineText}, Duration=${duration}`);
            
            await submitTaskToPage({
              image: task.image,
              lineText: lineText,
              presetBefore: parsedPreset.before,
              presetAfter: parsedPreset.after,
              footageType: task.footageType,
              count: task.count,
              duration: duration,
              rowIndex: rIdx,
              lineIndex: lIdx,
              download_path: task.download_path,
              resolution: task.resolution,
              aspectRatio: task.aspectRatio,
              videoModel: task.videoModel
            });

            if (!task.downloaded[lIdx] && !isStopRequested) updateLineStatus(rIdx, lIdx, "generating", "已提交到 Flow，等待对应媒体 ID 生成完成...");
            state.retryCount = 0;
            await saveState();
            submitOk = true;
            break;
            
          } catch (err) {
            console.error(`[Batch] 第 ${subTry + 1} 次提交触发异常:`, err);
            if (err.configurationError) {
              updateLineStatus(rIdx, lIdx, "failed", `未提交生成：${err.message}`);
              break;
            }
            if (subTry === 0 && !window.__lzxFlowTransport) {
              updateLineStatus(rIdx, lIdx, "pending", `提交受阻: ${err.message}，正在等待 3 秒后重试...`);
              await delay(3000);
            } else {
              updateLineStatus(rIdx, lIdx, "failed", "提交失败: " + err.message);
              await saveState();
            }
          }
        }

        if (!submitOk) {
          console.warn(`[Batch] 任务 (Row=${rIdx}, Line=${lIdx}) 提交失败，自动流转至下一项`);
          continue;
        }

        // ====================================================
        // 核心等待逻辑：高精度状态检测 + 视频生成完毕后立刻自动下载 + 严格下载完成后再倒计时流转
        // ====================================================
        const maxWaitSec = Math.max(120, Number(state.settings.maxWaitTime) || 300);
        let waitedSec = 0;
        let isTaskCompleted = false;
        let downloadTriggered = false;
        let nativeHoverCount = 0;
        const waitStartTime = Date.now();
        while ((Date.now() - waitStartTime) / 1000 < maxWaitSec) {
          if (isStopRequested) break;
          if (state.isSuspended) { await delay(500); continue; }

          // 始终获取最新内存引用，严防中途面板重载或存储同步导致引用分离
          task = state.tasks[rIdx] || task;
          const binding = task.mediaBindings?.[lIdx];
          console.log(`[Batch] ⏳ 等待中 (${Math.round(waitedSec)}s/${maxWaitSec}s): Row=${rIdx}, Line=${lIdx}, status=${task.status[lIdx]}, downloaded=${task.downloaded[lIdx]}, downloadId=${task.downloadIds?.[lIdx] || '无'}, mediaIds=${JSON.stringify(binding?.mediaIds || [])}, readyIds=${JSON.stringify(binding?.readyIds || [])}`);

          if (task.downloadIds?.[lIdx] && !task.downloaded[lIdx]) {
            console.log(`[Batch] 🔍 正在查询后台下载状态: downloadId=${task.downloadIds[lIdx]}...`);
            const downloadState = await sendToExtension("getDownloadStatus", { downloadId: task.downloadIds[lIdx] });
            console.log(`[Batch] 📥 后台下载状态返回: downloadId=${task.downloadIds[lIdx]}, state=${downloadState?.state}, error=${downloadState?.error || '无'}`);
            if (downloadState?.state === "complete") {
              task.downloadIds[lIdx] = null;
              task.downloaded[lIdx] = true;
              updateLineStatus(rIdx, lIdx, "success", "视频下载已完成");
            } else if (downloadState?.state === "interrupted") {
              task.downloadIds[lIdx] = null;
              downloadTriggered = false;
              if (binding) {
                binding.downloadFailCount = (binding.downloadFailCount || 0) + 1;
                if (binding.downloadFailCount < 3) {
                  console.warn(`[Batch] ⚠️ 下载中断 (${downloadState.error})，正在刷新签名直链自动重试 (${binding.downloadFailCount}/3)...`);
                  binding.urls = {};
                  updateLineStatus(rIdx, lIdx, "generating", `下载中断 (${downloadState.error || "未知原因"})，正在刷新视频直链重试 (${binding.downloadFailCount}/3)...`);
                } else {
                  updateLineStatus(rIdx, lIdx, "failed", `视频已生成，下载中断: ${downloadState.error || "未知原因"}。可重试下载。`);
                }
              } else {
                updateLineStatus(rIdx, lIdx, "failed", `视频已生成，下载中断: ${downloadState.error || "未知原因"}。可重试下载。`);
              }
            }
          }
          if (!task.downloadIds?.[lIdx] && downloadTriggered && !task.downloaded[lIdx]) {
            downloadTriggered = false;
          }
          if (task.status[lIdx] === "failed") {
            console.warn(`[Batch] ⚠️ 当前行状态已为 failed，退出等待循环: Row=${rIdx}, Line=${lIdx}, message=${task.message[lIdx]}`);
            break;
          }
          // 1. 检查该任务行是否已经完成下载 (status 变为 success 或 downloaded[lIdx] 变为 true)
          if (task.status[lIdx] === "success" || (Array.isArray(task.downloaded) && task.downloaded[lIdx])) {
            if (task.status[lIdx] !== "success") {
              updateLineStatus(rIdx, lIdx, "success", "视频下载已完成");
            }
            isTaskCompleted = true;
            console.log(`[Batch] 任务已确认生成并下载成功！(Row=${rIdx}, Line=${lIdx}, 耗时=${Math.round(waitedSec)}s)`);
            break;
          }

          let isVideoGenerated = false;

          const boundMediaIds = binding?.mediaIds || [];

          // 2. 检查是否有已就绪的本任务媒体直链 (来自 flow-proxy RPC 拦截缓存或 DOMWatcher)
          let targetVideoUrl = null;
          for (const mId of boundMediaIds) {
            const candidateUrl = (binding.urls?.[mId] && isValidFlowVideoUrl(binding.urls[mId]))
              ? binding.urls[mId]
              : discoveredVideoUrlsById.get(mId);
            if (candidateUrl && isValidFlowVideoUrl(candidateUrl)) {
              targetVideoUrl = candidateUrl;
              isVideoGenerated = true;
              if (!binding.readyIds.includes(mId)) binding.readyIds.push(mId);
              binding.urls = binding.urls || {};
              binding.urls[mId] = candidateUrl;
              console.log(`[Batch] 🎯 找到当前任务已绑定的有效签名直链 (mediaId=${mId}): ${targetVideoUrl.substring(0, 80)}`);
              break;
            }
          }

          // 3. 扫描并悬停页面视频图块与新生成的视频缩略图 (<img class="thumbnail" alt="生成的视频缩略图">)
          const targetBatchContainer = findCurrentBatchContainer(task, lIdx);
          const allVideoTiles = [
            ...(targetBatchContainer ? Array.from(targetBatchContainer.querySelectorAll("flow-video-tile, flow-grid-tile-container:not(:has(flow-image-tile))")) : []),
            ...Array.from(document.querySelectorAll("flow-video-tile, flow-grid-tile-container:not(:has(flow-image-tile))"))
          ];
          const uniqueTiles = [...new Set(allVideoTiles)];
          for (const tile of uniqueTiles) {
            triggerTileHover(tile);
          }

          // 3.5 检测提交后新渲染的视频缩略图 <img class="thumbnail" alt="生成的视频缩略图" src="https://flow-content.google/image/<uuid>?...">
          if (!isVideoGenerated && binding) {
            const thumbImgs = Array.from(document.querySelectorAll("img[src*='flow-content.google/image/']")).filter(isVideoThumbnailImg);
            for (const img of thumbImgs) {
              const imgSrc = (img.getAttribute("src") || img.src || "").trim().replace(/&amp;/g, "&");
              const thumbId = mediaIdFromImageThumbnailUrl(imgSrc);
              if (!thumbId) continue;
              const isTargetThumb = (boundMediaIds.length > 0 && boundMediaIds.includes(thumbId)) ||
                                    (boundMediaIds.length === 0 && !wasVideoSeen(`https://flow-content.google/video/${thumbId}`));
              if (isTargetThumb) {
                if (boundMediaIds.length === 0) {
                  console.log(`[Batch] 🎯 从新生成的视频缩略图 <img class="thumbnail"> 识别并绑定任务媒体 ID: ${thumbId}`);
                  binding.mediaIds = [thumbId];
                  boundMediaIds.push(thumbId);
                  await saveState();
                }
                const cachedVideoUrl = discoveredVideoUrlsById.get(thumbId);
                if (cachedVideoUrl && isValidFlowVideoUrl(cachedVideoUrl)) {
                  console.log(`[Batch] 🎉 从网络缓存直接命中缩略图 (${thumbId}) 对应的完整签名视频直链!`);
                  if (!binding.readyIds.includes(thumbId)) binding.readyIds.push(thumbId);
                  binding.urls = binding.urls || {};
                  binding.urls[thumbId] = cachedVideoUrl;
                  targetVideoUrl = cachedVideoUrl;
                  isVideoGenerated = true;
                  break;
                }
                // 唤醒缩略图所在卡片的懒加载 <video>
                triggerTileHover(img);
                if (nativeHoverCount < 4) {
                  try {
                    const rect = img.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                      nativeHoverCount++;
                      const hx = Math.round(rect.left + rect.width / 2);
                      const hy = Math.round(rect.top + rect.height / 2);
                      console.log(`[Batch] 🖱️ 对新视频缩略图 (${thumbId}) 发送硬件级鼠标悬停 @(${hx}, ${hy}) 以唤醒 <video> 标签...`);
                      await sendToExtension("nativeHover", { x: hx, y: hy });
                      await delay(350);
                    }
                  } catch (_) {}
                }
              }
            }
          }

          // 4. 校验全页面 DOM 视频：优先检查 targetBatchContainer，同时兜底扫描全页面所有 <video>，绝不因容器误判漏掉新视频
          if (!isVideoGenerated) {
            const candidateVideos = [
              ...(targetBatchContainer ? Array.from(targetBatchContainer.querySelectorAll("video")) : []),
              ...Array.from(document.querySelectorAll("flow-video-tile video, flow-grid-tile-container video, video"))
            ];
            const uniqueVideos = [...new Set(candidateVideos)];

            for (const v of uniqueVideos) {
              const rawSrc = (typeof v.getAttribute === "function" ? v.getAttribute("src") : null) || v.currentSrc || v.src;
              const src = (rawSrc || "").trim().replace(/&amp;/g, "&");
              if (src && !isStaticShowcaseAsset(src) && isValidFlowVideoUrl(src)) {
                const vidId = mediaIdFromVideoUrl(src);
                if (vidId) discoveredVideoUrlsById.set(vidId, src);
                const isFlowProjectVideo = /^https:\/\/flow-content\.google\/video\//i.test(src) || /^https:\/\/flow\.google\.com\/asb\//i.test(src);
                const inVideoTile = typeof v.closest === "function" ? !!(v.closest("flow-video-tile") || (v.closest("flow-grid-tile-container") && !v.closest("flow-image-tile"))) : true;
                if (vidId && boundMediaIds.length > 0 && boundMediaIds.includes(vidId)) {
                  console.log(`[Batch] 🎯 DOM 视频匹配到当前任务 boundMediaId=${vidId}: ${src.substring(0, 80)}`);
                  isVideoGenerated = true;
                  targetVideoUrl = src;
                  if (!binding.readyIds.includes(vidId)) binding.readyIds.push(vidId);
                  binding.urls = binding.urls || {};
                  binding.urls[vidId] = src;
                  break;
                } else if (vidId && (!binding.readyIds || binding.readyIds.length === 0) && !wasVideoSeen(src, v) && isFlowProjectVideo && inVideoTile) {
                  console.log(`[Batch] 🎯 DOM 发现提交后新生成的 Flow 视频 (mediaId=${vidId})，自动关联绑定到当前任务: ${src.substring(0, 80)}`);
                  binding.mediaIds = Array.isArray(binding.mediaIds) ? binding.mediaIds : [];
                  if (!binding.mediaIds.includes(vidId)) binding.mediaIds.push(vidId);
                  if (!binding.readyIds.includes(vidId)) binding.readyIds.push(vidId);
                  binding.urls = binding.urls || {};
                  binding.urls[vidId] = src;
                  await saveState();
                  isVideoGenerated = true;
                  targetVideoUrl = src;
                  break;
                }
              }
            }
          }

          // 5. 视频生成就绪后，调度自动下载
          if (isVideoGenerated && targetVideoUrl) {
            if (task.downloadIds?.[lIdx]) {
              downloadTriggered = true;
            } else if (!downloadTriggered && !(Array.isArray(task.downloaded) && task.downloaded[lIdx])) {
              console.log("[Batch] 🚀 当前任务视频已就绪，发起自动下载:", targetVideoUrl.substring(0, 80));
              const dlOk = await triggerAutoDownload(targetVideoUrl, mediaIdFromVideoUrl(targetVideoUrl));
              console.log("[Batch] triggerAutoDownload 执行结果:", dlOk);
              if (dlOk || task.downloadIds?.[lIdx]) {
                downloadTriggered = true;
                updateLineStatus(rIdx, lIdx, "generating", `视频已就绪，已发起下载，正在等待保存文件 (${Math.round(waitedSec)}s)...`);
              }
            }
          } else {
            if (!task.downloaded[lIdx]) {
              const waitHint = boundMediaIds.length > 0 ? 
                `已分配服务端 ID [${boundMediaIds.join(', ')}]，渲染生成中 (${Math.round(waitedSec)}s)...` : 
                `正在生成中，已等待 ${Math.round(waitedSec)}s (完成后将自动下载)...`;
              updateLineStatus(rIdx, lIdx, "generating", waitHint);
            }
          }

          await delay(2000);
          waitedSec = (Date.now() - waitStartTime) / 1000;

          // 4. 下载完成严格校验
          if (task.status[lIdx] === "success" && (Array.isArray(task.downloaded) && task.downloaded[lIdx])) {
            isTaskCompleted = true;
            break;
          }
        }



        if (!isTaskCompleted && !isStopRequested && task.status[lIdx] !== "failed") {
          console.warn(`[Batch] 任务生成等待超时 (${maxWaitSec}s)`);
          updateLineStatus(rIdx, lIdx, "failed", `生成或下载超时 (等待超过 ${maxWaitSec} 秒)`);
        }

        window.currentProcess = null;
        state.activeTasksQueue = [];
        state.taskRecord = {};
        // 清理行内继续的指针，方便下一个任务从 0 行开始跑
        state.currentLineIndex = 0;
        await saveState();

        if (isStopRequested) {
          showToast("用户中断了批量任务！");
          onBatchFinished();
          return;
        }

        // 如果是单任务生成模式，该任务生成并下载完成后立即停止，不等待间隔，不进入下一项
        if (singleRowIndex !== null && typeof singleRowIndex === "number") {
          showToast(`单项任务 #${rIdx + 1} 生成与下载已完成！`);
          onBatchFinished();
          return;
        }

        // The desktop owns the next batch. Its final job has no next-item interval.
        if (window.__lzxFlowTransport && isTaskCompleted && rIdx === endRow - 1 && lIdx === lines.length - 1) return;

        // 任务下载完成后的批量间隔延时 (严格按照规则设置或当前设定执行停顿，基于绝对时间戳保证失焦/后台准确走完)
        const baseInterval = (task.interval !== undefined && task.interval !== null && !isNaN(Number(task.interval)))
          ? Number(task.interval)
          : (state.settings.interval !== undefined ? Number(state.settings.interval) : 5);
        const intervalSec = Math.max(0, baseInterval);
        const intervalRand = (task.interval !== undefined && task.interval !== null)
          ? 0
          : Math.max(0, Number(state.settings.intervalRandom) || 0);
        const waitTime = Math.round(intervalSec + Math.random() * intervalRand);
        if (waitTime > 0) {
          const targetEndTime = Date.now() + waitTime * 1000;
          console.log(`[Batch] 当前任务已完成，等待间隔延迟: ${waitTime} 秒后进入下一项`);

          while (Date.now() < targetEndTime) {
            if (isStopRequested) break;
            const remainingSec = Math.max(1, Math.round((targetEndTime - Date.now()) / 1000));
            showToast(`当前视频已完成！将在 ${remainingSec} 秒后开始下一个任务...`, 1500);
            await delay(1000);
          }
        } else {
          console.log(`[Batch] 当前任务已完成，间隔设置为 0 秒，立即进入下一项`);
        }

        // 倒计时结束，在流转进入下一项前清理可能残留的遮罩与弹窗
        await cleanupPageOverlays();
      }
    }

    if (singleRowIndex !== null && typeof singleRowIndex === "number") {
      showToast(`单项任务执行已完成！`);
    } else {
      showToast("所有队列任务的批量生成提交已完成！");
    }
    onBatchFinished();
  }

  function stopBatch() {
    isStopRequested = true;
    stopActivityCheck();
    disableTabKeepAlive();
    state.isRunning = false;
    if (alarmTimer) { clearInterval(alarmTimer); alarmTimer = null; }
    saveState();
    showToast("正在停止队列，已提交到 Flow 的生成不会取消...");
  }

  function onBatchFinished() {
    stopActivityCheck();
    disableTabKeepAlive();
    
    state.isRunning = false;
    state.currentIndex = 0;
    state.currentLineIndex = 0;
    state.retryCount = 0;
    
    document.getElementById("btn-start-batch").style.setProperty("display", "inline-flex", "important");
    document.getElementById("btn-stop-batch").style.setProperty("display", "none", "important");
    
    window.currentProcess = null;
    state.activeTasksQueue = [];
    state.taskRecord = {};
    sendToExtension("clearActiveTask", {});
    saveState();
  }

  // ----------------------------------------------------
  // 自动化参数同步：模式、视频类型(帧/素材)、尺寸比例、分辨率、输出数量、时长
  // ----------------------------------------------------
  function durationError(message) {
    const error = new Error(message);
    error.configurationError = true;
    return error;
  }

  function findDurationGroup() {
    const overlay = document.querySelector(".cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay");
    if (!overlay) return null;
    const groups = Array.from(overlay.querySelectorAll("flow-toggles[aria-label], [role='radiogroup'][aria-label], select[aria-label], flow-toggles"));
    return groups.find(group => /时长|時長|秒数|秒數|长度|長度|duration|length/i.test(group.getAttribute("aria-label") || "")) ||
           groups.find(group => {
             const btns = Array.from(group.querySelectorAll("mat-button-toggle, [role='radio'], option"));
             return btns.length >= 2 && btns.some(b => /^\s*\d+\s*(秒|s|sec|seconds)\s*$/i.test(b.textContent || ""));
           }) || null;
  }

  async function applyDurationSetting(value) {
    const target = normalizeDuration(value);
    if (target == null) return;
    let group;
    for (let attempt = 0; attempt < 10; attempt++) {
      group = findDurationGroup();
      if (group) break;
      if (isStopRequested) throw durationError("任务已停止");
      await delay(200);
    }
    if (!group) {
      const panelOpen = document.querySelector(".cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay");
      if (panelOpen) {
        if (window.__lzxFlowTransport) throw durationError("当前模型没有可确认的时长选项，请使用模型默认时长");
        console.warn(`[FlowUI] 当前模型设置面板中无时长选项（可能为固定时长模型），跳过 ${target} 秒设置`);
        return;
      }
      throw durationError(`找不到视频时长选项，无法确认 ${target} 秒；请检查当前模型的设置面板`);
    }
    const options = () => Array.from(group.querySelectorAll("mat-button-toggle, [role='radio'], option"));
    const secondsOf = option => {
      try { return normalizeDuration(option.textContent.trim()); } catch (_) { return null; }
    };
    const selected = option => option.selected === true || option.classList.contains("mat-button-toggle-checked") ||
      option.getAttribute("aria-checked") === "true" || option.querySelector("button")?.getAttribute("aria-checked") === "true" ||
      option.querySelector("button")?.getAttribute("aria-pressed") === "true";

    let effectiveTarget = target;
    let option = options().find(item => secondsOf(item) === effectiveTarget);
    if (!option) {
      if (window.__lzxFlowTransport) throw durationError(`当前模型不支持 ${target} 秒，请选择模型默认或支持的时长`);
      const availableOpts = options().map(item => ({ item, sec: secondsOf(item) })).filter(x => x.sec != null);
      if (availableOpts.length > 0) {
        availableOpts.sort((a, b) => Math.abs(a.sec - target) - Math.abs(b.sec - target));
        option = availableOpts[0].item;
        effectiveTarget = availableOpts[0].sec;
        console.warn(`[FlowUI] 当前模型不支持 ${target} 秒，已自动匹配最接近的可用时长: ${effectiveTarget} 秒`);
      } else {
        throw durationError(`当前模型不支持 ${target} 秒；可选时长：${options().map(secondsOf).filter(v => v != null).join("、") || "未识别"}`);
      }
    }
    if (!selected(option)) {
      if (group.tagName === "SELECT") {
        group.value = option.value;
        group.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        const button = option.querySelector("button") || option;
        if (button.disabled || button.getAttribute("aria-disabled") === "true") throw durationError(`${effectiveTarget} 秒选项不可用`);
        button.click();
      }
    }
    for (let attempt = 0; attempt < 10; attempt++) {
      group = findDurationGroup();
      if (group && options().some(item => secondsOf(item) === effectiveTarget && selected(item))) return;
      if (isStopRequested) throw durationError("任务已停止");
      await delay(200);
    }
    throw durationError(`点击后未确认选中 ${effectiveTarget} 秒，已阻止生成`);
  }

  // ----------------------------------------------------
  // 自动切换生成模型 (在设置面板内部操作)
  // ----------------------------------------------------
  const MODEL_DISPLAY_NAMES = {
    "omni_1_1_flash": "Omni 1.1 Flash",
    "veo_3_1_lite": "Veo 3.1 - Lite",
    "veo_3_1_fast": "Veo 3.1 - Fast",
    "veo_3_1_quality": "Veo 3.1 - Quality",
    "veo_3_1_lite_low_priority": "Veo 3.1 - Lite [Lower Priority]",
    "veo_fast": "Veo 3.1 - Fast",
    "veo_2": "Veo 2",
    "VIDEO_REFERENCES": "VIDEO_REFERENCES"
  };

  function isModelNameMatch(candidateText, targetDisplayName, targetModelId) {
    if (!candidateText || !targetDisplayName) return false;
    const candLower = candidateText.toLowerCase();
    const targLower = targetDisplayName.toLowerCase();
    // 严格区分普通版与 [Lower Priority] 低优先级版本
    const candIsLowPrio = /lower\s*priority|low\s*priority|低优先级|低優先/i.test(candLower);
    const targIsLowPrio = targetModelId === "veo_3_1_lite_low_priority" || /lower\s*priority|low\s*priority|低优先级|低優先/i.test(targLower);
    if (candIsLowPrio !== targIsLowPrio) return false;

    const stripTags = (s) => s
      .replace(/\[.*?\]/g, "")
      .replace(/测试版|測試版|beta|arrow_drop_down|check/gi, "")
      .replace(/[\s\-_.]+/g, "")
      .trim();

    const candNorm = stripTags(candLower);
    const targNorm = stripTags(targLower);
    if (!candNorm || !targNorm) return false;
    return candNorm === targNorm || candNorm.includes(targNorm) || targNorm.includes(candNorm);
  }

  // 在设置面板内部切换模型 (overlay = flow-prompt-box-settings 面板元素，兼容简中/繁中/英文)
  async function switchModelInPanel(overlay, targetModelId) {
    const targetDisplayName = MODEL_DISPLAY_NAMES[targetModelId] || targetModelId;
    console.log(`[FlowUI] 🔄 面板内模型切换: 目标="${targetDisplayName}" (ID=${targetModelId})`);

    const modelBtnSelector = 'button[aria-label="选择模型系列"], button[aria-label="選取模型系列"], button[aria-label*="模型"], button[aria-label*="model" i], button.mat-mdc-menu-trigger';

    // 1. 在设置面板内查找模型选择器按钮
    let triggerBtn = overlay.querySelector(modelBtnSelector);
    if (!triggerBtn) {
      // 回退: 通过 .model-select-trigger-content 子元素
      const triggerContent = overlay.querySelector('.model-select-trigger-content');
      triggerBtn = triggerContent?.closest('button');
      if (triggerBtn) console.log("[FlowUI] 通过 .model-select-trigger-content 回退找到模型按钮");
    }
    if (!triggerBtn) {
      // 回退: 在整个页面中查找 (可能在 overlay 外层)
      triggerBtn = document.querySelector('flow-prompt-box-settings ' + modelBtnSelector) ||
                   document.querySelector('button[aria-label="选择模型系列"], button[aria-label="選取模型系列"], button[aria-label*="model family" i]');
      if (!triggerBtn) {
        const triggerContent = document.querySelector('.model-select-trigger-content');
        triggerBtn = triggerContent?.closest('button');
      }
      if (triggerBtn) console.log("[FlowUI] 在页面全局找到模型按钮");
    }

    if (!triggerBtn) {
      console.warn("[FlowUI] ❌ 未找到模型选择按钮，跳过模型切换");
      return false;
    }

    // 2. 读取当前模型名称
    const currentModelSpan = triggerBtn.querySelector('.model-select-trigger-content');
    const currentModelText = (currentModelSpan?.textContent || triggerBtn.textContent || "")
      .replace(/arrow_drop_down/gi, "").trim();
    console.log(`[FlowUI] 当前页面模型: "${currentModelText}"`);

    // 3. 判断是否需要切换
    if (isModelNameMatch(currentModelText, targetDisplayName, targetModelId)) {
      console.log(`[FlowUI] ✅ 当前模型已经是 "${currentModelText}"，无需切换`);
      return false; // 没有切换
    }

    console.log(`[FlowUI] ⚠️ 当前模型 "${currentModelText}" ≠ 目标 "${targetDisplayName}"，正在自动切换...`);

    // 4. 点击触发按钮打开下拉菜单
    console.log("[FlowUI] 点击模型选择器按钮...");
    triggerBtn.click();
    await delay(800);

    // 5. 在下拉菜单中查找目标模型选项
    let targetMenuItem = null;

    // 策略 A: 查找 span.label 文本
    const labelElements = document.querySelectorAll('.cdk-overlay-container span.label, .cdk-overlay-container .item-text .label, .cdk-overlay-container [role="menuitem"] span');
    console.log(`[FlowUI] 下拉菜单中找到 ${labelElements.length} 个 label 元素`);
    for (const label of labelElements) {
      const labelText = (label.textContent || "").trim();
      if (isModelNameMatch(labelText, targetDisplayName, targetModelId)) {
        // 向上查找可点击的按钮
        targetMenuItem = label.closest('button') || label.closest('flow-menu-item')?.querySelector('button') || label.closest('[role="menuitem"]') || label;
        console.log(`[FlowUI] ✅ 找到目标: "${labelText}" -> ${targetMenuItem.tagName}`);
        break;
      }
    }

    // 策略 B: 遍历菜单按钮
    if (!targetMenuItem) {
      const allMenuBtns = document.querySelectorAll('.cdk-overlay-container button, .cdk-overlay-container [role="menuitem"]');
      for (const btn of allMenuBtns) {
        const btnText = (btn.textContent || "").trim();
        if (isModelNameMatch(btnText, targetDisplayName, targetModelId)) {
          targetMenuItem = btn;
          console.log(`[FlowUI] ✅ 策略 B 找到: "${btnText.substring(0, 50)}"`);
          break;
        }
      }
    }

    if (targetMenuItem) {
      console.log(`[FlowUI] 🎯 正在点击目标模型...`);
      targetMenuItem.click();
      await delay(1500); // 等待模型切换后页面/面板更新
      console.log(`[FlowUI] ✅ 模型切换完成!`);
      return true; // 模型已切换
    } else {
      console.warn(`[FlowUI] ❌ 下拉菜单中未找到 "${targetDisplayName}"，关闭菜单`);
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      await delay(300);
      return false;
    }
  }

  async function syncPageVideoSettings(settings) {
    const { resolution, aspectRatio, videoModel, footageType, count, duration } = settings;
    const targetCount = count || 1;
    const targetRes = resolution || "VIDEO_RESOLUTION_720P";
    const targetAspect = aspectRatio || "ASPECT_RATIO_9_16";
    const targetFootage = footageType || "VIDEO_FRAMES";

    console.log(`[FlowUI] 开始同步视频设置: 模式=${targetFootage}, 比例=${targetAspect}, 分辨率=${targetRes}, 数量=${targetCount}, 时长=${duration || '默认'}, 模型=${videoModel || '默认'}`);

    // 0-B. 尝试全局 store 兜底设置
    try {
      if (window.promptBoxStore && typeof window.promptBoxStore.setState === "function") {
        window.promptBoxStore.setState({
          videoResolution: targetRes,
          selectedVideoResolution: targetRes,
          resolution: targetRes,
          aspectRatio: targetAspect === "ASPECT_RATIO_9_16" ? "PORTRAIT" : (targetAspect === "ASPECT_RATIO_1_1" ? "SQUARE" : "LANDSCAPE"),
          outputsPerPrompt: targetCount,
          videoModelFamilyId: videoModel || "omni_1_1_flash",
          mode: targetFootage
        });
      }
    } catch (storeErr) {
      console.warn("[FlowUI] promptBoxStore 兜底更新提示:", storeErr);
    }

    const findSettingsTriggerBtn = () =>
      document.querySelector("button.settings-trigger-button") ||
      document.querySelector("div.submit-controls button:not(.generate-icon-button)") ||
      document.querySelector("button[aria-label*='设置'], button[aria-label*='設定'], button[aria-label*='Settings' i]");

    // 1. 定位页面底部的设置按钮 (模型切换后页面可能重新渲染，需要重试)
    let settingsBtn = null;
    for (let retry = 0; retry < 8; retry++) {
      settingsBtn = findSettingsTriggerBtn();
      if (settingsBtn) break;
      console.log(`[FlowUI] 等待设置按钮出现... (${retry + 1}/8)`);
      await delay(500);
    }
    
    if (!settingsBtn) {
      throw durationError("未找到 Flow 设置按钮，无法确认任务秒数");
    }

    // 检查按钮文本摘要是否已经完全符合预期（兼容简中/繁中/英文，如 `x 1` vs `x1`）
    const summaryText = (settingsBtn.textContent || "").toLowerCase();
    const summaryCompact = summaryText.replace(/\s+/g, "");
    const resKey = targetRes.includes("360") ? "360p" : (targetRes.includes("1080") ? "1080p" : "720p");
    const aspectKey = targetAspect.includes("9_16") ? "crop_9_16" : (targetAspect.includes("1_1") ? "crop_1_1" : "crop_16_9");
    const aspectTextKey = targetAspect.includes("9_16") ? "9:16" : (targetAspect.includes("1_1") ? "1:1" : "16:9");
    const countKey = `x${targetCount}`;

    const resMatched = summaryCompact.includes(resKey);
    const aspectMatched = summaryCompact.includes(aspectKey) || summaryCompact.includes(aspectTextKey);
    const countMatched = summaryCompact.includes(countKey);
    const modelMatched = !videoModel || isModelNameMatch(summaryText, MODEL_DISPLAY_NAMES[videoModel] || videoModel, videoModel);
    const footageMatched = targetFootage === "VIDEO_FRAMES"
      ? /(帧|影格|frame|crop_free)/i.test(summaryText)
      : /(素材|原料|ingredient|reference|chrome_extension)/i.test(summaryText);

    // 如果摘要已经完美满足（分辨率、比例、数量、模型、模式全部一致且无强制秒数校验），才跳过设置弹窗
    if (duration == null && resMatched && aspectMatched && countMatched && modelMatched && footageMatched) {
      console.log(`[FlowUI] 当前设置摘要 (${settingsBtn.textContent.trim()}) 已满足要求，无需重复开启设置弹窗`);
      return;
    }

    // 2. 点击设置按钮弹出 CDK Overlay 设置面板 (带重试)
    let overlay = null;
    for (let retry = 0; retry < 3; retry++) {
      console.log(`[FlowUI] 正在点击设置按钮弹出设置面板... (尝试 ${retry + 1}/3)`);
      // 每次重试都重新查找按钮（防止 DOM 被替换）
      const freshBtn = findSettingsTriggerBtn() || settingsBtn;
      freshBtn.click();
      
      // 等待面板出现
      for (let wait = 0; wait < 8; wait++) {
        await delay(400);
        overlay = document.querySelector(".cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay");
        if (overlay) break;
      }
      if (overlay) break;
      console.warn(`[FlowUI] 第 ${retry + 1} 次点击后设置面板未出现，重试...`);
      await delay(500);
    }
    
    if (!overlay) {
      throw durationError("未打开 Flow 设置面板，无法设置任务秒数");
    }

    // 3. 通用切换助手：支持简中/繁中/英文多语言 aria-label 列表，并自带图标/文字全组扫描兜底
    function setToggle(ariaLabels, matcher) {
      if (window.__lzxFlowTransport) {
        overlay = window.__lzxFlowTransport.settingsPanel();
        if (!overlay) throw Object.assign(new Error("设置面板正在重建，自动等待后重新核对；未提交生成。"), {code: "FLOW_SETTINGS_NOT_READY"});
      }
      const labelList = Array.isArray(ariaLabels) ? ariaLabels : [ariaLabels];
      let toggleGroup = null;
      for (const lbl of labelList) {
        toggleGroup = overlay.querySelector(`flow-toggles[aria-label="${lbl}"]`) ||
                      overlay.querySelector(`flow-toggles[aria-label*="${lbl}" i]`);
        if (toggleGroup) break;
      }

      // 兜底：若 aria-label 因界面语言未知未命中，扫描面板内所有 flow-toggles 寻找包含匹配项的组
      const candidateGroups = toggleGroup
        ? [toggleGroup]
        : Array.from(overlay.querySelectorAll("flow-toggles, mat-button-toggle-group"));

      for (const grp of candidateGroups) {
        const toggles = grp.querySelectorAll("mat-button-toggle");
        for (const t of toggles) {
          const clone = t.cloneNode(true);
          clone.querySelectorAll("mat-icon").forEach(ic => ic.remove());
          const pureText = (clone.textContent || "").trim();
          const fullText = (t.textContent || "").trim();
          const icon = (t.querySelector("mat-icon")?.textContent || "").trim();
          const isChecked = t.classList.contains("mat-button-toggle-checked") ||
                            t.querySelector("button")?.getAttribute("aria-checked") === "true";

          const isMatch = typeof matcher === "function"
            ? (matcher(pureText, icon, t) || matcher(fullText, icon, t))
            : (pureText.includes(matcher) || fullText.includes(matcher) || icon.includes(matcher));
          if (isMatch) {
            if (!isChecked) {
              const btn = t.querySelector("button.mat-button-toggle-button") || t.querySelector("button") || t;
              console.log(`[FlowUI] 切换设置 [${labelList[0]}] -> ${pureText || icon}`);
              btn.click();
            } else {
              console.log(`[FlowUI] 设置 [${labelList[0]}] 已经是目标状态: ${pureText || icon}`);
            }
            return true;
          }
        }
      }
      console.warn(`[FlowUI] 设置面板内未找到切换组: [${labelList.join(" / ")}]`);
      return false;
    }

    // 3.1 先确保顶层模式是“视频 / 影片 / Video”，这样模型选择器才会显示视频生成模型
    setToggle(["模式", "Mode"], (text, icon) => /(视频|影片|video)/i.test(text) || icon === "videocam");
    await delay(150);

    // 3.2 在设置面板内部自动切换模型 (模型选择器在面板里)
    if (videoModel) {
      const modelSwitched = await switchModelInPanel(overlay, videoModel);
      if (modelSwitched) {
        // 模型切换后设置面板可能已经关闭或重新渲染，需要重新打开
        console.log("[FlowUI] 模型已切换，检查设置面板是否需要重新打开...");
        overlay = document.querySelector(".cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay");
        if (!overlay) {
          console.log("[FlowUI] 设置面板已关闭，重新打开...");
          for (let retry = 0; retry < 3; retry++) {
            const freshBtn = findSettingsTriggerBtn();
            if (freshBtn) freshBtn.click();
            for (let wait = 0; wait < 8; wait++) {
              await delay(400);
              overlay = document.querySelector(".cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay");
              if (overlay) break;
            }
            if (overlay) break;
            await delay(500);
          }
          if (!overlay) {
            throw durationError("模型切换后无法重新打开设置面板");
          }
        }
        console.log("[FlowUI] 设置面板已就绪，继续配置其他参数...");
      }
    }

    // (a) 模式: 再次确认切换为“视频 / 影片 / Video”
    setToggle(["模式", "Mode"], (text, icon) => /(视频|影片|video)/i.test(text) || icon === "videocam");
    await delay(150);

    // (b) 视频类型: 切换为“帧 / 影格 / Frames”或“素材 / Ingredients”
    if (targetFootage === "VIDEO_FRAMES") {
      setToggle(["视频类型", "影片類型", "Video type", "Type"], (text, icon) => /(帧|影格|frame)/i.test(text) || icon === "crop_free");
    } else {
      setToggle(["视频类型", "影片類型", "Video type", "Type"], (text, icon) => /(素材|原料|ingredient|reference)/i.test(text) || icon === "chrome_extension");
    }
    await delay(150);

    // (c) 宽高比 / 顯示比例 / Aspect ratio: 切换为“9:16”或“16:9”
    if (targetAspect.includes("9_16")) {
      setToggle(["宽高比", "顯示比例", "長寬比", "Aspect ratio"], (text, icon) => text.includes("9:16") || icon === "crop_9_16");
    } else if (targetAspect.includes("1_1")) {
      setToggle(["宽高比", "顯示比例", "長寬比", "Aspect ratio"], (text, icon) => text.includes("1:1") || icon === "crop_1_1");
    } else {
      setToggle(["宽高比", "顯示比例", "長寬比", "Aspect ratio"], (text, icon) => text.includes("16:9") || icon === "crop_16_9");
    }
    await delay(150);

    // (d) 视频分辨率 / 影片解析度 / Video resolution: 切换为 720p 或 360p
    setToggle(["视频分辨率", "影片解析度", "解析度", "分辨率", "Video resolution", "Resolution"], (text) => text.toLowerCase().includes(resKey));
    await delay(150);

    // (e) 输出数量 / 生成數量 / Outputs: 强制切换为目标数量 (兼容 `x1` 与 `x 1`)
    setToggle(["输出数量", "生成數量", "輸出數量", "Outputs", "Count"], (text) => text.replace(/\s+/g, "").toLowerCase() === `x${targetCount}`);
    await delay(150);

    // 选择时长（若当前模型不支持目标秒数则自动匹配最近可用秒数）
    try {
      await applyDurationSetting(duration);
    } catch (err) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      throw err;
    }

    if (window.__lzxFlowTransport) {
      await window.__lzxFlowTransport.waitForSettings(settings);
    }

    // 4. 关闭设置面板
    console.log("[FlowUI] 完成设置配置，关闭设置弹窗...");
    try {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      await delay(150);
      if (document.querySelector(".settings-content-overlay")) {
        settingsBtn.click();
      }
    } catch (e) {}

    await delay(300);
  }

  // 模拟真实的用户单击操作（仅执行一次完整的 down/up/click 流程，杜绝重复连击）
  function simulateClick(el) {
    if (!el) return;
    try { if (typeof el.focus === "function") el.focus(); } catch (_) {}
    try {
      if (typeof PointerEvent !== "undefined") {
        el.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, view: window }));
        el.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, view: window }));
      }
    } catch (_) {}
    // 关键：严格只触发一次真实的 click，避免多次提交！
    if (typeof el.click === "function") {
      el.click();
    } else {
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    }
  }

  // 安全派发鼠标/指针事件（避免 click 事件被二次重复触发）
  function safeDispatchMouse(el, type) {
    if (!el) return;
    if (type === "click") {
      simulateClick(el);
      return;
    }
    try {
      if (typeof PointerEvent !== "undefined" && type.startsWith("pointer")) {
        el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, view: window }));
      } else if (typeof MouseEvent !== "undefined") {
        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
      }
    } catch (_) {}
  }

  // ----------------------------------------------------
  // 自动化关联素材原料：首帧注入 (帧模式) 或素材模式
  // ----------------------------------------------------
  async function attachImageToPage(image, footageType) {
    if (!image) return;
    const imgName = image.displayName || "";
    const imgSrc = image.src || "";
    const imgKey = image.primaryMediaKey || image.id || "";
    const isFrameMode = footageType === "VIDEO_FRAMES";

    console.log(`[FlowUI] 正在关联素材: Name="${imgName}", Key="${imgKey}", Mode="${footageType}"`);

    // 0. 尝试全局 store 注入兜底 (兼容 Flow 早先架构)
    try {
      if (window.promptBoxStore && typeof window.promptBoxStore.getState === "function") {
        const store = window.promptBoxStore.getState();
        if (store.actions && typeof store.actions.addImageIngredient === "function") {
          console.log("[FlowUI] 通过 promptBoxStore.addImageIngredient 注入素材原料");
          store.actions.addImageIngredient({
            imageId: imgKey || imgName,
            preferredIngredientType: isFrameMode ? "FIRST_FRAME" : "REFERENCE",
            source: "PLUS_BUTTON"
          });
          await delay(300);
          return;
        }
      }
    } catch (storeErr) {
      console.warn("[FlowUI] promptBoxStore.addImageIngredient 失败:", storeErr);
    }

    // 1. 核心通道：通过 flow-ingredient-bar 的“开始”帧槽位注入 (帧模式)
    const ingredientBar = document.querySelector("flow-ingredient-bar.prompt-ingredient-bar, .prompt-ingredient-bar, flow-ingredient-bar");
    if (ingredientBar) {
      const allTriggers = Array.from(ingredientBar.querySelectorAll(".frame-trigger"));
      const isEndSlot = (el) => /(结束|結束|\bend\b)/i.test(((el?.textContent || "") + " " + (el?.getAttribute?.("aria-label") || "")).trim());
      const startTrigger = allTriggers.find(t => !isEndSlot(t)) || null;
      if (startTrigger) {
        // 如果首帧已有内容且匹配当前图片，无需重复添加
        const triggerImg = startTrigger.querySelector("img");
        const triggerTitle = extractTileDisplayName(startTrigger, triggerImg);
        const triggerText = startTrigger.textContent || "";
        if (imgName && (isImageNameMatch(imgName, triggerTitle) || isImageNameMatch(imgName, triggerText))) {
          console.log("[FlowUI] 首帧槽位中已存在匹配图片，无需重复注入:", imgName);
          return;
        }

        // 点击“开始”按钮（button.empty-chip 或 button）打开媒体选择面板（严禁误点“结束”帧按钮）
        const startBtn = startTrigger.querySelector("button.empty-chip") || startTrigger.querySelector("button");
        if (startBtn && !isEndSlot(startBtn)) {
          console.log("[FlowUI] 点击首帧触发器按钮 [开始] 弹出媒体选择面板...");
          simulateClick(startBtn);

          // 轮询等待媒体弹窗展开 (最多等待 3 秒)
          let overlayPanel = null;
          for (let w = 0; w < 20; w++) {
            await delay(150);
            const panels = Array.from(document.querySelectorAll(
              ".cdk-overlay-pane:not(:empty), .cdk-overlay-container .cdk-overlay-pane, [role='dialog'], [role='menu'], mat-dialog-container"
            )).filter(p => p.offsetWidth > 0 && p.offsetHeight > 0);
            if (panels.length) {
              overlayPanel = panels[panels.length - 1];
              const items = overlayPanel.querySelectorAll("flow-grid-tile-container, flow-image-tile, flow-tile, flow-media-tile, button:has(img), img");
              if (items.length > 0) break;
            }
          }

          if (overlayPanel) {
            console.log("[FlowUI] 成功检测到素材选择弹层面板，开始匹配素材...");

            // 在弹层中查找匹配的图片素材卡片
            const candidateItems = Array.from(overlayPanel.querySelectorAll(
              "flow-grid-tile-container, flow-image-tile, flow-tile, flow-media-tile, " +
              "[data-tile-id], [data-media-id], .media-item, [role='option'], mat-option, " +
              ".tile-row > div, button:has(img), div:has(> img)"
            ));

            let matchedCard = null;
            for (const item of candidateItems) {
              const itemImg = item.querySelector("img");
              const itemDisplayName = extractTileDisplayName(item, itemImg);
              const itemTitle = item.getAttribute("title") || item.querySelector("[title]")?.getAttribute("title") || "";
              const itemAlt = itemImg?.getAttribute("alt") || "";
              const itemAria = item.getAttribute("aria-label") || "";
              const itemText = item.textContent || "";
              const itemId = itemImg?.dataset?.mediaId || item.dataset?.mediaId || item.dataset?.tileId || "";

              if (imgKey && itemId && (imgKey === itemId || itemId.includes(imgKey) || imgKey.includes(itemId))) {
                matchedCard = item;
                break;
              }
              if (imgName && (isImageNameMatch(imgName, itemDisplayName) || isImageNameMatch(imgName, itemTitle) || isImageNameMatch(imgName, itemAlt) || isImageNameMatch(imgName, itemAria) || isImageNameMatch(imgName, itemText))) {
                matchedCard = item;
                break;
              }
            }

            // 如果名字未精确匹配，尝试在所有带 img 的卡片中寻找文件名子串
            if (!matchedCard) {
              const allImgTiles = Array.from(overlayPanel.querySelectorAll("flow-grid-tile-container, flow-image-tile, button:has(img), div:has(> img)"));
              for (const tile of allImgTiles) {
                const tName = extractTileDisplayName(tile, tile.querySelector("img"));
                if (imgName && tName && isImageNameMatch(imgName, tName)) {
                  matchedCard = tile;
                  break;
                }
              }
            }

            // 如果仍未匹配，且弹窗中只有少量图片，兜底选取第一张图片素材
            if (!matchedCard && candidateItems.length > 0) {
              const firstTile = overlayPanel.querySelector("flow-grid-tile-container:has(img), flow-image-tile, button:has(img)");
              if (firstTile && candidateItems.length <= 2) {
                console.log("[FlowUI] 弹窗中候选较少，选用第一张素材:", firstTile);
                matchedCard = firstTile;
              }
            }

            if (matchedCard) {
              console.log(`[FlowUI] 🎯 在首帧媒体面板中匹配到素材 [${imgName}]，触发选中点击！`);
              const clickTarget = matchedCard.querySelector("button, img, .container") || matchedCard;
              simulateClick(clickTarget);

              await delay(400);

              // 关键！自动检测并点击弹层中的“确认”、“确定”、“选择”、“添加”、“完成”按钮（彻底解放用户手动点击！）
              const allButtons = Array.from(document.querySelectorAll(
                ".cdk-overlay-container button, mat-dialog-actions button, .mat-mdc-dialog-actions button, .cdk-overlay-pane button"
              )).filter(btn => {
                if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return false;
                const t = (btn.textContent || "").trim();
                const aria = (btn.getAttribute("aria-label") || "").trim();
                if (/^(取消|关闭|關閉|Cancel|Close|Dismiss)$/i.test(t) || /^(取消|关闭|關閉|Cancel|Close|Dismiss)$/i.test(aria)) return false;
                if (/(确认|确定|確認|確定|选择|選取|選擇|添加|新增|完成|使用|插入|首帧|初始影格|Confirm|Select|Add|Done|OK|Apply|Insert)/i.test(t) ||
                    /(确认|确定|確認|確定|选择|選取|選擇|添加|新增|完成|使用|插入|首帧|初始影格|Confirm|Select|Add|Done|OK|Apply|Insert)/i.test(aria)) return true;
                if (btn.closest("mat-dialog-actions, .mat-mdc-dialog-actions") && (btn.classList.contains("primary") || btn.classList.contains("flow-button-primary") || btn.classList.contains("mat-mdc-unelevated-button") || btn.classList.contains("mat-primary"))) return true;
                return false;
              });

              if (allButtons.length > 0) {
                const actionBtn = allButtons.find(b => /(确认|确定|確認|確定|选择|選取|選擇|添加|新增|完成|使用|首帧|初始影格|Confirm|Select|Add|Done|OK|Apply)/i.test((b.textContent || "") + (b.getAttribute("aria-label") || ""))) || allButtons[allButtons.length - 1];
                console.log(`[FlowUI] 🎯 自动点击媒体选择弹窗确认按钮: "${(actionBtn.textContent || actionBtn.getAttribute('aria-label') || '').trim()}"`);
                simulateClick(actionBtn);
                await delay(500);
              }

              await delay(300);
              if (verifyImageAttached(image, footageType)) {
                console.log("[FlowUI] ✅ 首帧素材挂载成功！");
                return;
              }
            }

            // 如果弹窗仍未关闭，按 Escape 关闭弹层
            const stillOpen = !!document.querySelector(".cdk-overlay-pane:not(:empty) [role='dialog'], mat-dialog-container");
            if (stillOpen) {
              document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
              await delay(250);
            }
          }
        }
      }
    }

    // 2. 备选通道：输入框左下角加号菜单 (flow-add-menu)
    const addMenuBtn = document.querySelector("flow-add-menu button.add-menu-trigger, flow-add-menu button, button.add-menu-trigger, button.add-ingredient-button");
    if (addMenuBtn) {
      console.log("[FlowUI] 尝试点击输入框左下角加号菜单添加素材...");
      simulateClick(addMenuBtn);
      await delay(350);

      const menuPanels = Array.from(document.querySelectorAll(".cdk-overlay-pane, .flow-add-menu-dropdown-panel, .mat-mdc-menu-panel, [role='menu']"));
      for (const panel of menuPanels) {
        const options = Array.from(panel.querySelectorAll("button, [role='menuitem'], [role='option'], .flow-add-menu-dropdown-option-text, span"));
        let clickedOpt = false;
        for (const opt of options) {
          const optText = opt.textContent || "";
          if (imgName && isImageNameMatch(imgName, optText)) {
            console.log(`[FlowUI] 在素材菜单中找到匹配素材: ${optText.trim()}，点击添加`);
            simulateClick(opt);
            clickedOpt = true;
            await delay(300);
            break;
          }
        }
        if (clickedOpt) {
          await delay(300);
          return;
        }
      }
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      await delay(200);
    }

    // 3. 备选策略：Base64 图片文件拖拽/粘贴注入
    if (imgSrc && imgSrc.startsWith("data:image/")) {
      try {
        console.log("[FlowUI] 尝试通过原生 Clipboard/Paste 事件注入图片 File...");
        const targetEditor = document.querySelector(".prompt-ingredient-bar .frame-trigger:first-of-type") ||
                             document.querySelector("flow-rich-text-editor div.ProseMirror") || 
                             document.querySelector("div.ProseMirror[contenteditable='true']");
        if (targetEditor) {
          const byteString = atob(imgSrc.split(",")[1]);
          const mimeString = imgSrc.split(",")[0].split(":")[1].split(";")[0];
          const ab = new ArrayBuffer(byteString.length);
          const ia = new Uint8Array(ab);
          for (let i = 0; i < byteString.length; i++) {
            ia[i] = byteString.charCodeAt(i);
          }
          const blob = new Blob([ab], { type: mimeString });
          const file = new File([blob], imgName || "ingredient.png", { type: mimeString });

          const dt = new DataTransfer();
          dt.items.add(file);

          const dropEvent = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt });
          targetEditor.dispatchEvent(dropEvent);

          const pasteEvent = new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt });
          targetEditor.dispatchEvent(pasteEvent);
          console.log("[FlowUI] 已向目标区域派发 File 注入事件");
          await delay(400);
        }
      } catch (fileErr) {
        console.warn("[FlowUI] File drop/paste 注入异常:", fileErr);
      }
    }
  }

  // 校验当前页面输入区域是否已成功挂载所要求的素材图片
  function verifyImageAttached(image, footageType) {
    if (!image || (!image.displayName && !image.src && !image.primaryMediaKey)) {
      return true; // 未指定图片（纯文本生成任务），直接放行
    }

    const isFrameMode = footageType === "VIDEO_FRAMES";

    // 1. 优先检查 flow.promptBoxStore 全局状态（如可用）
    try {
      if (window.promptBoxStore && typeof window.promptBoxStore.getState === "function") {
        const store = window.promptBoxStore.getState();
        if (store) {
          const ingredients = store.ingredients || store.imageIngredients || store.frames || [];
          if (Array.isArray(ingredients) && ingredients.length > 0) {
            return true;
          }
          if (store.startFrame || store.firstFrame || (store.frames && (store.frames.start || store.frames.first))) {
            return true;
          }
        }
      }
    } catch (e) {
      console.warn("[FlowUI] 检查 promptBoxStore 异常:", e);
    }

    // 2. 检查 DOM: flow-ingredient-bar / .prompt-ingredient-bar
    const ingredientBar = document.querySelector("flow-ingredient-bar.prompt-ingredient-bar, .prompt-ingredient-bar, flow-ingredient-bar");
    if (ingredientBar) {
      // 优先检查 ingredientBar 内是否已存在任何已挂载的图片缩略图或素材胶囊（注意排除空的“结束”槽位干扰）
      const hasChipOrImg = !!ingredientBar.querySelector(
        "img, flow-tile, flow-media-tile, flow-ingredient-chip, .ingredient-chip, mat-chip, .chip-container, button[aria-label*='移除'], button[aria-label*='删除'], button[aria-label*='刪除'], button[aria-label*='Remove' i], button[aria-label*='Clear' i], .clear-button"
      );
      if (hasChipOrImg) return true;

      // 帧模式下检查首帧槽位 (.frame-trigger，严格排除“结束/End”帧槽位)
      if (isFrameMode) {
        const allTriggers = Array.from(ingredientBar.querySelectorAll(".frame-trigger"));
        const isEndSlot = (el) => /(结束|結束|\bend\b)/i.test(((el?.textContent || "") + " " + (el?.getAttribute?.("aria-label") || "")).trim());
        const startTrigger = allTriggers.find(t => !isEndSlot(t)) || null;
        if (startTrigger) {
          const emptyBtn = startTrigger.querySelector("button.empty-chip");
          const hasImg = !!startTrigger.querySelector("img, flow-tile, flow-media-tile, [role='img']");
          const hasNonEmptyButton = !!startTrigger.querySelector("button:not(.empty-chip)");
          const triggerTitle = extractTileDisplayName(startTrigger, startTrigger.querySelector("img"));
          const triggerText = startTrigger.textContent || "";
          const matchesName = image.displayName && (isImageNameMatch(image.displayName, triggerTitle) || isImageNameMatch(image.displayName, triggerText));

          if (hasImg || hasNonEmptyButton || (!emptyBtn && triggerText.trim() !== "" && !triggerText.includes("开始") && !triggerText.includes("開始") && !/start/i.test(triggerText)) || matchesName) {
            return true;
          }
          if (emptyBtn) {
            return false;
          }
        }
      }

      const barTitle = extractTileDisplayName(ingredientBar, ingredientBar.querySelector("img"));
      const barText = ingredientBar.textContent || "";
      if (image.displayName && (isImageNameMatch(image.displayName, barTitle) || isImageNameMatch(image.displayName, barText))) {
        return true;
      }
    }

    // 3. 检查富文本编辑器 (flow-rich-text-editor) 是否内嵌素材胶囊
    const editorChips = document.querySelectorAll("flow-rich-text-editor [data-chip-type], flow-rich-text-editor .ingredient-chip, flow-rich-text-editor img");
    if (editorChips.length > 0) {
      return true;
    }

    return false;
  }

  // 精准定位 Google Flow 底部提示词框的发送/生成按钮 (包含 <mat-icon>arrow_forward</mat-icon> 或 generate-icon-button)
  // 严格按优先级查找，绝不使用包含宽泛 [aria-label*='开始'] 的单次 querySelector，防止误匹配首帧按钮或顶部导航栏按钮
  function findFlowGenerateButton() {
    const isVisible = (el) => {
      if (!el || !el.isConnected) return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden" && getComputedStyle(el).display !== "none";
    };

    const editor = document.querySelector("flow-rich-text-editor div.ProseMirror") ||
                   document.querySelector("div.ProseMirror[contenteditable='true']");
    const promptRoot = editor?.closest("flow-prompt-box, .prompt-box-container, .prompt-box, form") ||
                       document.querySelector("flow-prompt-box, .prompt-box-container, .prompt-box") ||
                       document;

    // 优先级 1: 明确带有 generate-icon-button 类名或位于 flow-generate-icon-button 内的 button
    const directCandidates = [
      ...Array.from(promptRoot.querySelectorAll("button.generate-icon-button, flow-generate-icon-button button")),
      ...Array.from(document.querySelectorAll("button.generate-icon-button, flow-generate-icon-button button"))
    ];
    const visDirect = directCandidates.find(b => isVisible(b) && Array.from(b.querySelectorAll("mat-icon, .google-symbols")).some(ic => (ic.textContent || "").trim() === "arrow_forward")) ||
                      directCandidates.find(isVisible) ||
                      directCandidates[0];
    if (visDirect) return visDirect;

    // 优先级 2: 通过内部 <mat-icon class="... google-symbols ...">arrow_forward</mat-icon> 精准反查父级 button（跨简/繁/英完全通用）
    const arrowIcons = Array.from(promptRoot.querySelectorAll("mat-icon, .google-symbols")).filter(ic => (ic.textContent || "").trim() === "arrow_forward");
    for (const ic of arrowIcons) {
      const btn = ic.closest("button");
      if (btn && isVisible(btn)) return btn;
    }

    const globalArrowBtns = Array.from(document.querySelectorAll("mat-icon, .google-symbols"))
      .filter(ic => (ic.textContent || "").trim() === "arrow_forward")
      .map(ic => ic.closest("button"))
      .filter(btn => btn && isVisible(btn));
    if (globalArrowBtns.length > 0) {
      // 选择页面最底部（提示词框所在位置）的 arrow_forward 按钮
      globalArrowBtns.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
      return globalArrowBtns[0];
    }

    // 优先级 3: 精确匹配生成按钮的 aria-label 或 type="submit"（绝不模糊匹配“开始”，防止误点首帧槽位）
    const exactSelectors = [
      "button[type='submit'][aria-label='开始生成']",
      "button[type='submit'][aria-label='開始產生']",
      "button[type='submit'][aria-label='開始生成']",
      "button[aria-label='开始生成']",
      "button[aria-label='開始產生']",
      "button[aria-label='開始生成']",
      "button[aria-label='Start generating' i]",
      "button[aria-label='Generate' i]",
      "button[type='submit']"
    ];
    for (const sel of exactSelectors) {
      const found = Array.from(promptRoot.querySelectorAll(sel)).filter(isVisible);
      if (found.length > 0) return found[found.length - 1];
    }

    return null;
  }

  // 注入提示词至 ProseMirror 并触发 Angular 提交
  async function submitTaskToPage(params) {
    const { image, lineText, presetBefore, presetAfter, footageType, count, duration, rowIndex, lineIndex, resolution, aspectRatio, videoModel } = params;
    
    // 0. 同步视频尺寸比例、分辨率、视频类型(帧/素材)、输出数量与生成模型
    const targetRes = resolution || state.settings.resolution || "VIDEO_RESOLUTION_720P";
    const targetAspect = aspectRatio || state.settings.aspectRatio || "ASPECT_RATIO_9_16";
    const targetModel = (videoModel && videoModel !== "VIDEO_REFERENCES" ? videoModel : null) || state.settings.videoModel || "omni_1_1_flash";
    const targetFootage = footageType || state.settings.footageType || "VIDEO_FRAMES";
    const targetCount = count || state.settings.defaultCount || 1;

    // 关键修复 1：先同步页面设置（切换到目标模式如“帧”模式），确保 flow-ingredient-bar 显示出首帧槽位，且设置切换不会清除刚挂载的图片！
    try {
      await syncPageVideoSettings({
        resolution: targetRes,
        aspectRatio: targetAspect,
        videoModel: targetModel,
        footageType: targetFootage,
        count: targetCount,
        duration: duration
      });
    } catch (settErr) {
      throw settErr;
    }

    // 提交前清理历史弹窗与遮罩，确保输入框与按钮处于可交互视口
    await cleanupPageOverlays();

    // 关键修复 2：在目标模式设置好后，再挂载参考图片/首帧素材原料！
    if (image && (image.displayName || image.src || image.primaryMediaKey)) {
      try {
        await attachImageToPage(image, targetFootage);
      } catch (imgErr) {
        console.warn("[FlowUI] 注入图片原料异常 (继续执行):", imgErr);
      }
    }

    // 3. 定位 ProseMirror 输入框
    let editor = document.querySelector("flow-rich-text-editor div.ProseMirror") || 
                 document.querySelector("div.ProseMirror[contenteditable='true']");
    if (!editor) {
      for (let i = 0; i < 10; i++) {
        await delay(300);
        editor = document.querySelector("flow-rich-text-editor div.ProseMirror") || 
                 document.querySelector("div.ProseMirror[contenteditable='true']");
        if (editor) break;
      }
    }

    if (!editor) {
      throw new Error("页面上未找到提示词输入框 (flow-rich-text-editor ProseMirror)");
    }

    // 4. 拼装拼接后的提示词
    const finalPrompt = `${presetBefore || ""}
${lineText || ""}
${presetAfter || ""}`.trim();
    params.finalPrompt = finalPrompt;
    console.log(`[FlowUI] 正在填入提示词 (第 ${rowIndex + 1} 个任务, 子行 ${lineIndex + 1}): "${finalPrompt.substring(0, 60)}..."`);

    // 5. 注入文本到 ProseMirror 编辑器 (支持多行段落，绝不让 ProseMirror 报错)
    try {
      editor.focus();
    } catch (e) {}

    let textInjected = false;

    // 策略 A: ProseMirror View 原生多行段落事务注入
    if (editor.pmViewDesc && editor.pmViewDesc.view) {
      try {
        const view = editor.pmViewDesc.view;
        const schema = view.state.schema;
        const lines = finalPrompt.split("\n");
        const pType = schema.nodes.paragraph;
        const paragraphs = lines.map(line => {
          return line ? pType.create(null, schema.text(line)) : pType.create();
        });
        const docNode = schema.nodes.doc.create(null, paragraphs);
        const tr = view.state.tr.replaceWith(0, view.state.doc.content.size, docNode.content);
        view.dispatch(tr);
        textInjected = true;
        console.log("[FlowUI] 已通过 ProseMirror EditorView.dispatch 原生注入多行段落文本");
      } catch (pmErr) {
        console.warn("[FlowUI] ProseMirror View dispatch 异常，转入 DOM 注入:", pmErr);
      }
    }

    // 策略 B: DOM 原生选中与 execCommand
    if (!textInjected) {
      try {
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(editor);
        sel.removeAllRanges();
        sel.addRange(range);
        textInjected = document.execCommand("insertText", false, finalPrompt);
      } catch (domErr) {
        console.warn("[FlowUI] execCommand 注入失败:", domErr);
      }
    }

    // 策略 C: 结构化段落替换（比较去空白后的文本，避免多行段落因不含 \n 而误覆盖 ProseMirror 原生节点）
    const normEditorText = (editor.textContent || "").replace(/\s+/g, "");
    const normFinalPrompt = finalPrompt.replace(/\s+/g, "");
    if (!textInjected || normEditorText !== normFinalPrompt) {
      if (typeof editor.replaceChildren === "function") {
        const paragraphs = finalPrompt.split("\n").map(l => {
          const p = document.createElement("p");
          p.textContent = l || "";
          return p;
        });
        editor.replaceChildren(...paragraphs);
      } else if (typeof editor.appendChild === "function") {
        editor.innerHTML = "";
        const lines = finalPrompt.split("\n");
        lines.forEach(l => {
          const p = document.createElement("p");
          p.textContent = l || "";
          editor.appendChild(p);
        });
      } else {
        editor.textContent = finalPrompt;
      }
    }

    // 派发全面的事件确保 Angular 表单状态更新为 valid
    editor.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertFromPaste", data: finalPrompt }));
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    editor.dispatchEvent(new Event("change", { bubbles: true }));
    window.promptBoxStore?.getState?.().actions?.setText?.(finalPrompt);
    window.promptBoxStore?.getState?.().actions?.setPrompt?.(finalPrompt);
    await delay(300);

    // 6. 提交前图片挂载状态二次复核与补救
    if (image && (image.displayName || image.src || image.primaryMediaKey)) {
      let isMounted = verifyImageAttached(image, targetFootage);
      if (!isMounted) {
        console.warn(`[FlowUI] ⚠️ 提交前首次检查发现素材 [${image.displayName || "图片"}] 尚未挂载，等待后复检...`);
        await delay(400);
        isMounted = verifyImageAttached(image, targetFootage);
      }
      if (!isMounted) {
        console.warn(`[FlowUI] ⚠️ 正在执行二次挂载素材重试...`);
        await attachImageToPage(image, targetFootage);
        await delay(600);
        isMounted = verifyImageAttached(image, targetFootage);
      }
      if (isMounted) {
        console.log(`[FlowUI] ✅ 提交前图片挂载校验通过: [${image.displayName || "素材图片"}] 已正确挂载到输入框！`);
      } else {
        console.warn(`[FlowUI] ⚠️ 挂载检测未直接捕获到芯片，继续执行发送...`);
      }
    }

    // 7. 查找生成按钮 (button.generate-icon-button / <mat-icon>arrow_forward</mat-icon>) 并确保可点击发送
    const isBtnReady = (b) => b && !b.disabled && !b.hasAttribute("disabled") && b.getAttribute("aria-disabled") !== "true" && !b.classList.contains("mat-mdc-button-disabled") && !b.classList.contains("flow-icon-button-disabled");
    let button;
    for (let i = 0; i < 40; i++) {
      if (isStopRequested) throw new Error("用户已停止任务");
      button = findFlowGenerateButton();
      if (!state.isSuspended && isBtnReady(button)) break;
      // 若按钮仍 disabled，尝试重新派发输入事件激活
      if (button && !isBtnReady(button)) {
        editor.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText", data: finalPrompt }));
        editor.dispatchEvent(new Event("input", { bubbles: true }));
        editor.dispatchEvent(new Event("change", { bubbles: true }));
      }
      button = null;
      await delay(500);
    }
    if (!button) {
      button = findFlowGenerateButton();
      if (button) {
        if (window.__lzxFlowTransport) throw durationError("生成按钮未就绪，请检查网站提示后重试");
        console.warn("[FlowUI] ⚠️ 生成按钮超时未就绪，强制解除 disabled 限制触发点击");
        button.removeAttribute("disabled");
        button.disabled = false;
        button.setAttribute("aria-disabled", "false");
        button.classList.remove("mat-mdc-button-disabled", "flow-icon-button-disabled");
      } else {
        throw new Error("Flow 生成按钮尚未就绪，请检查页面提示");
      }
    }

    await sendToExtension("setActiveTask", {
      download_path: resolveDownloadPath(params.download_path, params),
      rowIndex, lineIndex, projectId: getStorageKey()
    });
    if (isStopRequested) throw new Error("用户已停止任务");
    params.submissionId = crypto.randomUUID();
    const task = state.tasks[rowIndex];
    task.taskId = task.taskId || crypto.randomUUID();
    task.mediaBindings = task.mediaBindings || [];
    task.downloadIds = task.downloadIds || [];
    task.downloadIds[lineIndex] = null;
    task.videoUrls = task.videoUrls || [];
    task.videoUrls[lineIndex] = null;
    task.mediaBindings[lineIndex] = {
      schema: 2, submissionId: params.submissionId, taskId: task.taskId,
      mediaIds: [], readyIds: [], downloadPath: resolveDownloadPath(params.download_path, params)
    };

    await saveState();
    if (isStopRequested) throw new Error("用户已停止任务");
    snapshotCurrentDomMedia();
    window.currentProcess = params;
    state.activeTasksQueue = [params];

    // 重新获取最新 DOM 引用，防止异步等待期间按钮被 Angular 重绘替换
    button = findFlowGenerateButton() || button;
    const arrowIcon = Array.from(button.querySelectorAll("mat-icon, .google-symbols")).find(ic => (ic.textContent || "").trim() === "arrow_forward") || button.querySelector("mat-icon");
    const clickEl = arrowIcon || button;
    const rect = clickEl.getBoundingClientRect();
    const cx = rect.width > 0 ? Math.round(rect.left + rect.width / 2) : 0;
    const cy = rect.height > 0 ? Math.round(rect.top + rect.height / 2) : 0;

    console.log("[FlowUI] 🚀 正在触发生成按钮单次点击发送 (目标 button & mat-icon arrow_forward):", { button, arrowIcon, cx, cy });
    try {
      if (typeof PointerEvent !== "undefined") {
        clickEl.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, pointerId: 1, isPrimary: true, button: 0, buttons: 1 }));
        clickEl.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, pointerId: 1, isPrimary: true, button: 0, buttons: 0 }));
      }
      clickEl.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1 }));
      clickEl.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 0 }));
    } catch (_) {}
    button.click();
    await delay(1800);

    // 仅当首次点击 1.8s 后完全未发出任何生成请求 (generateRequestSent !== true) 且无绑定 ID 时，才执行单次兜底点击，严禁连续点击 3 次
    const boundIds = task.mediaBindings?.[lineIndex]?.mediaIds || [];
    if (!window.__lzxFlowTransport && !params.generateRequestSent && boundIds.length === 0 && !isFlowCurrentlyGenerating()) {
      const curEditor = document.querySelector("flow-rich-text-editor div.ProseMirror") || document.querySelector("div.ProseMirror[contenteditable='true']");
      const editorHasText = curEditor && (curEditor.textContent || "").trim().length > 0;
      const curBtn = findFlowGenerateButton();
      if (editorHasText && curBtn && isBtnReady(curBtn)) {
        console.log("[FlowUI] 🔁 首次点击 1.8s 后未检测到生成网络请求，执行单次兜底点击...");
        curBtn.click();
        await delay(600);
      }
    }
  }

  function getProjectTiles() {
    return Array.from(document.querySelectorAll("flow-grid-tile-container:has(flow-video-tile video)"));
  }

  function isFlowCurrentlyGenerating() {
    return !!document.querySelector("flow-generate-icon-button mat-spinner, flow-generate-icon-button .mat-mdc-progress-spinner");
  }


  function resolveDownloadPath(downloadPath, processInfo = {}) {
    if (window.__lzxFlowTransport && /^FlowBridge\/[a-f0-9]{32}\/result\.mp4$/.test(downloadPath)) return downloadPath;
    if (typeof downloadPath === "string" && downloadPath.trim()) {
      const parts = downloadPath.trim().replace(/\\/g, "/").replace(/^[a-zA-Z]:\//, "").split("/").filter(Boolean);
      const filename = sanitizePathName(parts.pop());
      const downloadsIndex = parts.indexOf("downloads");
      const folder = downloadsIndex > 0 ? parts[downloadsIndex - 1] : parts[parts.length - 1];
      return `Flow/${sanitizePathName(folder || getProjectName())}/${filename}`;
    }
    const name = sanitizePathName((processInfo.image?.displayName || `task_${(processInfo.rowIndex || 0) + 1}`).replace(/\.\w*$/, ""));
    return `Flow/${sanitizePathName(getProjectName())}/${name}-${(processInfo.lineIndex || 0) + 1}.mp4`;
  }

  function isStaticShowcaseAsset(url) {
    return !url || /\/website\/flow\/|\/flow_camera\/|\/showcase\/|gstatic\.com|recaptcha|(?:left|right|center|high)\.mp4/i.test(url);
  }

  // 校验 Flow CDN 视频 URL 是否携带完整签名参数 (防止被 \\u003d 截断导致下载成 .xml 报错)
  function isValidFlowVideoUrl(value) {
    if (!value || typeof value !== "string") return false;
    const clean = value.trim().replace(/&amp;/g, "&");
    try {
      const u = new URL(clean);
      if (u.hostname === "flow-content.google") {
        if (!/^\/video\/[a-zA-Z0-9_-]+$/.test(u.pathname)) return false;
        const exp = u.searchParams.get("Expires");
        const sig = u.searchParams.get("Signature");
        return !!(exp && /^\d+$/.test(exp) && sig && sig.length >= 8);
      }
      if (u.hostname === "flow.google.com" && u.pathname.startsWith("/asb/")) return true;
      if (u.hostname === "labs.google" && u.pathname === "/fx/api/trpc/media.getMediaUrlRedirect") return true;
    } catch (_) {}
    return false;
  }

  function videoIdentity(url) {
    try {
      const parsed = new URL(url);
      if (parsed.hostname === "flow-content.google" && parsed.pathname.startsWith("/video/")) return parsed.origin + parsed.pathname;
      if (parsed.hostname === "flow.google.com" && parsed.pathname.startsWith("/asb/")) return parsed.origin + parsed.pathname;
    } catch (_) {}
    return url;
  }

  function mediaIdFromVideoUrl(value) {
    try {
      const url = new URL(value);
      if (url.hostname === "flow-content.google") return url.pathname.match(/^\/video\/([a-zA-Z0-9_-]+)$/)?.[1] || null;
      if (url.hostname === "flow.google.com" && url.pathname.startsWith("/asb/")) {
        return url.pathname.match(/^\/asb\/([a-zA-Z0-9_-]+)/)?.[1] || null;
      }
      if (url.hostname === "labs.google" && url.pathname === "/fx/api/trpc/media.getMediaUrlRedirect") {
        const name = url.searchParams.get("name") || "";
        return name.match(/(?:^|\/)media\/([a-zA-Z0-9_-]+)$/)?.[1] || (/^[a-zA-Z0-9_-]+$/.test(name) ? name : null);
      }
    } catch (_) {}
    return null;
  }

  // 从视频缩略图 <img class="thumbnail" alt="生成的视频缩略图" src="https://flow-content.google/image/<uuid>?..."> 或 /asb/<token> 提取视频 ID
  function mediaIdFromImageThumbnailUrl(value) {
    if (!value || typeof value !== "string") return null;
    try {
      const url = new URL(value.trim().replace(/&amp;/g, "&"));
      if (url.hostname === "flow-content.google") {
        return url.pathname.match(/^\/image\/([a-zA-Z0-9_-]+)$/)?.[1] || null;
      }
      if (url.hostname === "flow.google.com" && url.pathname.startsWith("/asb/")) {
        return url.pathname.match(/^\/asb\/([a-zA-Z0-9_-]+)/)?.[1] || null;
      }
    } catch (_) {}
    return null;
  }

  function isVideoThumbnailImg(imgEl) {
    if (!imgEl || imgEl.tagName !== "IMG") return false;
    if (typeof imgEl.closest === "function" && imgEl.closest("flow-image-tile")) return false;
    const alt = (imgEl.getAttribute("alt") || "").trim();
    const cls = (imgEl.className || "").toString();
    if (/视频缩略图|影片縮圖|video\s*thumbnail/i.test(alt)) return true;
    if (cls.includes("thumbnail") && typeof imgEl.closest === "function" && (imgEl.closest("flow-video-tile") || imgEl.closest("flow-grid-tile-container:not(:has(flow-image-tile))"))) {
      return true;
    }
    return false;
  }

  // 在每次提交前拍摄当前页面所有已存在的 <video> 与视频缩略图 <img> 快照，防止把旧视频误判为新生成视频
  function snapshotCurrentDomMedia() {
    document.querySelectorAll("video").forEach(v => {
      const src = (v.getAttribute("src") || v.currentSrc || v.src || "").trim().replace(/&amp;/g, "&");
      if (src) {
        downloadedUrlsSet.add(src);
        const vId = mediaIdFromVideoUrl(src);
        if (vId) {
          downloadedUrlsSet.add(`https://flow-content.google/video/${vId}`);
          downloadedUrlsSet.add(`https://flow.google.com/asb/${vId}`);
        }
      }
    });
    document.querySelectorAll("flow-video-tile img, img.thumbnail, img[src*='flow-content.google/image/'], img[src*='flow.google.com/asb/']").forEach(img => {
      if (isVideoThumbnailImg(img)) {
        const src = (img.getAttribute("src") || img.src || "").trim().replace(/&amp;/g, "&");
        if (src) downloadedUrlsSet.add(src);
        const mId = mediaIdFromImageThumbnailUrl(src);
        if (mId) {
          downloadedUrlsSet.add(`https://flow-content.google/video/${mId}`);
          downloadedUrlsSet.add(`https://flow.google.com/asb/${mId}`);
        }
      }
    });
  }

  function wasVideoSeen(url, videoEl = null) {
    const identity = videoIdentity(url);
    if (Array.from(downloadedUrlsSet).some(seen => videoIdentity(seen) === identity)) return true;
    const mId = mediaIdFromVideoUrl(url);
    if (mId) {
      const bound = findMediaBinding(mId);
      if (bound && (bound.rowIndex !== state.currentIndex || bound.lineIndex !== state.currentLineIndex || bound.task?.downloaded?.[bound.lineIndex])) {
        return true;
      }
    }
    // 针对 /asb/ 缩略图与视频 ID 不同的情况：检查该 <video> 所属 flow-video-tile 的缩略图是否已在旧快照集合中
    const el = videoEl || Array.from(document.querySelectorAll("flow-video-tile video, flow-grid-tile-container video, video")).find(v => {
      const vSrc = (v.getAttribute("src") || v.currentSrc || v.src || "").trim().replace(/&amp;/g, "&");
      return vSrc && videoIdentity(vSrc) === identity;
    });
    if (el && typeof el.closest === "function") {
      const tile = el.closest("flow-video-tile") || el.closest("flow-grid-tile-container");
      if (tile) {
        const thumbImg = tile.querySelector("img.thumbnail, img[src*='flow.google.com/asb/'], img[src*='flow-content.google/image/']");
        const thumbSrc = thumbImg ? (thumbImg.getAttribute("src") || thumbImg.src || "").trim().replace(/&amp;/g, "&") : "";
        if (thumbSrc && Array.from(downloadedUrlsSet).some(seen => videoIdentity(seen) === videoIdentity(thumbSrc))) {
          return true;
        }
      }
    }
    return false;
  }

  function findMediaBinding(mediaId) {
    if (!mediaId) return null;
    for (let rowIndex = 0; rowIndex < state.tasks.length; rowIndex++) {
      const task = state.tasks[rowIndex];
      for (let lineIndex = 0; lineIndex < (task.mediaBindings || []).length; lineIndex++) {
        const binding = task.mediaBindings[lineIndex];
        if (binding?.schema === 2 && binding.mediaIds.includes(mediaId)) return { task, binding, rowIndex, lineIndex };
      }
    }
    return null;
  }

  // 定位属于当前任务行的批次容器 (基于 mediaId 强绑定与行独占提示词特征匹配，避免预设模板词误匹配旧容器)
  function findCurrentBatchContainer(activeTask, lineIndex) {
    const activeProcess = window.currentProcess;
    const binding = activeTask?.mediaBindings?.[lineIndex];
    const boundIds = binding?.mediaIds || [];
    const containers = Array.from(document.querySelectorAll(".batch-container, .virtual-item-container"));
    if (!containers.length) return null;

    // 优先策略 1: 如果容器内的 video 或视频缩略图 img 包含当前任务绑定的 mediaId，100% 确认为本任务容器！
    if (boundIds.length > 0) {
      for (const container of containers) {
        const videos = Array.from(container.querySelectorAll("video"));
        for (const v of videos) {
          const src = (typeof v.getAttribute === "function" ? v.getAttribute("src") : null) || v.currentSrc || v.src;
          const vId = mediaIdFromVideoUrl(src);
          if (vId && boundIds.includes(vId)) {
            console.log(`[FlowUI] 🎯 [findCurrentBatchContainer] 根据 video mediaId (${vId}) 精准锁定当前任务批次容器`);
            return container;
          }
        }
        const thumbs = Array.from(container.querySelectorAll("img.thumbnail, img[src*='flow-content.google/image/'], img[src*='flow.google.com/asb/']"));
        for (const img of thumbs) {
          const imgId = mediaIdFromImageThumbnailUrl(img.getAttribute("src") || img.src);
          if (imgId && boundIds.includes(imgId)) {
            console.log(`[FlowUI] 🎯 [findCurrentBatchContainer] 根据缩略图 mediaId (${imgId}) 精准锁定当前任务批次容器`);
            return container;
          }
        }
      }
    }

    // 优先策略 2: 使用当前行独有的 lineText (而非包含公共预设模板的 finalPrompt) 进行精准比对
    const uniqueLineText = (activeProcess?.lineText || activeTask?.lines?.[lineIndex] || "").trim();
    if (uniqueLineText) {
      const cleanLine = uniqueLineText.replace(/\s+/g, "");
      if (cleanLine.length >= 4) {
        for (const container of containers) {
          const promptEl = container.querySelector(".prompt-text, flow-expandable-prompt");
          const domText = (promptEl ? (promptEl.innerText || promptEl.textContent || "") : "").replace(/\s+/g, "");
          if (domText && domText.includes(cleanLine)) {
            console.log("[FlowUI] 🎯 [findCurrentBatchContainer] 根据当前行独占文案特征精准锁定当前任务批次容器");
            return container;
          }
        }
      }
    }

    // 严禁盲目返回 containers[0]，若当前任务的新批次尚未生成，必须返回 null 继续等待！
    return null;
  }

  async function triggerDownloadFromNewestTile() {
    console.log("[FlowUI] 🔍 triggerDownloadFromNewestTile 开始扫描当前任务批次 video 元素...");
    const activeTask = state.tasks[state.currentIndex];
    const targetBatchContainer = findCurrentBatchContainer(activeTask, state.currentLineIndex);

    const targetTiles = targetBatchContainer ? 
      Array.from(targetBatchContainer.querySelectorAll("flow-video-tile")) : 
      Array.from(document.querySelectorAll("flow-video-tile"));
    if (targetTiles.length > 0) {
      triggerTileHover(targetTiles[0]);
      await delay(300);
    }
    const videoList = targetBatchContainer ? 
      Array.from(targetBatchContainer.querySelectorAll("video")) : 
      Array.from(document.querySelectorAll("flow-video-tile video, video"));
    console.log(`[FlowUI] triggerDownloadFromNewestTile 找到 ${videoList.length} 个 video 元素`);
    for (const video of videoList) {
      const src = (typeof video.getAttribute === "function" ? video.getAttribute("src") : null) || video.currentSrc || video.src;
      console.log(`[FlowUI] 尝试对 video 触发下载: src=${(src || '').substring(0, 80)}`);
      if (src && isValidFlowVideoUrl(src) && await triggerAutoDownload(src)) return true;
    }

    // 备用通道: 点击当前批次专属的原生批量下载按钮
    const dlSelector = "button[aria-label='批量下载'], button[aria-label='批次下載'], button[aria-label*='下载'], button[aria-label*='下載'], button[aria-label*='Download' i]";
    const newestDownloadBtn = targetBatchContainer ? 
      targetBatchContainer.querySelector(dlSelector) : 
      document.querySelector(`.batch-container ${dlSelector}, flow-batch-info ${dlSelector}, ${dlSelector}`);
    if (newestDownloadBtn && !newestDownloadBtn.disabled) {
      console.log("[FlowUI] 备用通道: 尝试点击当前批次专属的 [批量下载] 按钮...");
      newestDownloadBtn.click();
      return true;
    }
    return false;
  }

  async function retrySavedVideo(task, rowIndex, lineIndex, downloadOnly = false) {
    const binding = task.mediaBindings?.[lineIndex];
    console.log(`[FlowUI] [retrySavedVideo] 开始重试任务行: Row=${rowIndex}, Line=${lineIndex}, binding=`, binding);
    if (binding?.schema !== 2 || !binding.mediaIds?.length) {
      if (window.__lzxFlowTransport && binding?.schema === 2) {
        updateLineStatus(rowIndex, lineIndex, "generating", "保留原提交等待媒体编号，不清除绑定、不重新生成。");
        return !downloadOnly;
      }
      console.warn(`[FlowUI] [retrySavedVideo] 尚无已绑定的 mediaIds (mediaIds.length=${binding?.mediaIds?.length || 0})，清除空绑定以重新提交或认领...`);
      if (task.mediaBindings) task.mediaBindings[lineIndex] = null;
      return false;
    }
    binding.downloading = false;
    if (task.downloadIds?.[lineIndex]) {
      const existing = await sendToExtension("getDownloadStatus", { downloadId: task.downloadIds[lineIndex] });
      console.log(`[FlowUI] [retrySavedVideo] 检查已有 downloadId=${task.downloadIds[lineIndex]} 状态:`, existing);
      if (existing?.state === "complete") {
        task.downloaded[lineIndex] = true;
        updateLineStatus(rowIndex, lineIndex, "success", "对应视频下载已完成");
        return true;
      }
      if (existing?.state === "in_progress") {
        updateLineStatus(rowIndex, lineIndex, "generating", "等待对应 ID 的下载完成...");
        return true;
      }
      task.downloadIds[lineIndex] = null;
    }
    binding.downloadInitiated = false;
    const mediaId = binding.readyIds?.[0] || binding.mediaIds?.[0];
    let url = binding.urls?.[mediaId];
    if (url && !isValidFlowVideoUrl(url)) {
      console.warn(`[FlowUI] [retrySavedVideo] 清除已损坏/缺少签名的旧缓存 URL: ${url}`);
      delete binding.urls[mediaId];
      url = null;
    }
    if (!url && mediaId && discoveredVideoUrlsById.has(mediaId)) {
      const cached = discoveredVideoUrlsById.get(mediaId);
      if (isValidFlowVideoUrl(cached)) {
        url = cached;
        binding.urls = binding.urls || {};
        binding.urls[mediaId] = cached;
      }
    }
    for (const video of document.querySelectorAll("flow-video-tile video, flow-grid-tile-container video, video")) {
      const candidate = (video.getAttribute("src") || video.currentSrc || video.src || "").trim().replace(/&amp;/g, "&");
      if (candidate && isValidFlowVideoUrl(candidate) && mediaIdFromVideoUrl(candidate) === mediaId) {
        console.log(`[FlowUI] [retrySavedVideo] 从 DOM 视频图块找到最新完整签名 URL: ${candidate.substring(0, 80)}`);
        url = candidate;
        discoveredVideoUrlsById.set(mediaId, candidate);
        if (!binding.readyIds.includes(mediaId)) binding.readyIds.push(mediaId);
        binding.urls = binding.urls || {};
        binding.urls[mediaId] = candidate;
      }
    }
    console.log(`[FlowUI] [retrySavedVideo] readyMediaId=${mediaId}, boundUrl=${url}`);
    if (!mediaId || !url || !isValidFlowVideoUrl(url)) {
      console.warn(`[FlowUI] [retrySavedVideo] 缺少就绪或有效签名的 url (mediaId=${mediaId}, url=${url})，尝试悬停对应缩略图并等待完整直链...`);
      for (const img of document.querySelectorAll("img.thumbnail, img[src*='flow-content.google/image/'], img[src*='flow.google.com/asb/']")) {
        if (mediaIdFromImageThumbnailUrl(img.getAttribute("src") || img.src) === mediaId || isVideoThumbnailImg(img)) {
          triggerTileHover(img);
        }
      }
      updateLineStatus(rowIndex, lineIndex, "generating", "已取得媒体 ID，等待获取完整签名视频直链...");
      return !downloadOnly;
    }
    updateLineStatus(rowIndex, lineIndex, "generating", "重试下载已确认完成的媒体 ID...");
    return triggerAutoDownload(url, mediaId, downloadOnly ? binding : null);
  }

  async function triggerAutoDownload(videoUrl, mediaName, retryBinding = null) {
    if (!state.isRunning && !retryBinding) {
      console.warn("[FlowUI] [AutoDownload] 🚫 拒绝下载: state.isRunning 为 false (批量任务已停止)");
      return false;
    }
    if (typeof videoUrl !== "string") {
      console.warn("[FlowUI] [AutoDownload] 🚫 拒绝下载: videoUrl 不是有效字符串", videoUrl);
      return false;
    }
    const cleanUrl = videoUrl.trim().replace(/&amp;/g, "&");
    if (!isValidFlowVideoUrl(cleanUrl)) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 视频 URL 缺少完整签名参数 (防止下载成错误 .xml)! URL: ${cleanUrl}`);
      return false;
    }
    const mediaId = mediaIdFromVideoUrl(cleanUrl);
    if (mediaId) discoveredVideoUrlsById.set(mediaId, cleanUrl);
    console.log(`[FlowUI] [AutoDownload] 🔍 尝试自动下载: videoUrl=${cleanUrl.substring(0, 100)}, mediaId=${mediaId}, 传入mediaName=${mediaName || '未指定'}`);
    if (!mediaId) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 无法从 URL 解析出有效的 mediaId! URL: ${cleanUrl}`);
      return false;
    }
    if (mediaName && mediaName !== mediaId) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 传入的 mediaName (${mediaName}) 与从 URL 解析出的 mediaId (${mediaId}) 不一致!`);
      return false;
    }
    let match = findMediaBinding(mediaId);
    if (retryBinding && (match?.binding !== retryBinding || retryBinding.schema !== 2)) return false;
    if (!window.__lzxFlowTransport && !match && state.isRunning) {
      const activeTask = state.tasks[state.currentIndex];
      const activeBinding = activeTask?.mediaBindings?.[state.currentLineIndex];
      const isGenerating = activeTask?.status?.[state.currentLineIndex] === "generating" && !activeTask?.downloaded?.[state.currentLineIndex];
      if (isGenerating && activeBinding && (!activeBinding.readyIds || activeBinding.readyIds.length === 0) && !isStaticShowcaseAsset(cleanUrl)) {
        const isFlowProjectVideoUrl = /^https:\/\/flow-content\.google\/video\/[a-zA-Z0-9_-]+/i.test(cleanUrl) || /^https:\/\/flow\.google\.com\/asb\/[a-zA-Z0-9_-]+/i.test(cleanUrl);
        const matchingVideoEl = Array.from(document.querySelectorAll("flow-video-tile video, flow-grid-tile-container video, video")).find(v => {
          if (!v) return false;
          const vSrc = (typeof v.getAttribute === "function" ? v.getAttribute("src") : null) || v.currentSrc || v.src;
          const hasTileParent = isFlowProjectVideoUrl || (typeof v.closest === "function" ? !!(v.closest("flow-video-tile") || (v.closest("flow-grid-tile-container") && !v.closest("flow-image-tile"))) : true);
          return mediaIdFromVideoUrl(vSrc) === mediaId && hasTileParent;
        });
        if (!wasVideoSeen(cleanUrl, matchingVideoEl) && (matchingVideoEl || isFlowProjectVideoUrl)) {
          console.log(`[FlowUI] 🎯 [AutoDownload] 验证新生成的 Flow 视频属于当前任务! 自动关联绑定: mediaId=${mediaId}, Row=${state.currentIndex}, Line=${state.currentLineIndex}`);
          activeBinding.mediaIds = Array.isArray(activeBinding.mediaIds) ? activeBinding.mediaIds : [];
          if (!activeBinding.mediaIds.includes(mediaId)) activeBinding.mediaIds.push(mediaId);
          if (!activeBinding.readyIds.includes(mediaId)) activeBinding.readyIds.push(mediaId);
          activeBinding.urls = activeBinding.urls || {};
          activeBinding.urls[mediaId] = cleanUrl;
          await saveState();
          match = { task: activeTask, binding: activeBinding, rowIndex: state.currentIndex, lineIndex: state.currentLineIndex };
        }
      }
    }
    if (!match) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 未在任何任务行中找到已绑定 mediaId=${mediaId} 的记录! 当前任务绑定列表:`, state.tasks.map((t, i) => ({ rowIndex: i, bindings: t.mediaBindings })));
      return false;
    }
    const { task, binding, rowIndex, lineIndex } = match;
    if (window.__lzxFlowTransport && binding.source !== "generation_response") return false;
    console.log(`[FlowUI] [AutoDownload] 🎯 匹配到任务行: Row=${rowIndex}, Line=${lineIndex}, 关联图片=[${task.image?.displayName || '未命名'}], 目标文件名=${binding.downloadPath}`);
    if (rowIndex !== state.currentIndex || lineIndex !== state.currentLineIndex) {
      console.log(`[FlowUI] [AutoDownload] ℹ️ 提示: 此就绪视频属于任务 Row=${rowIndex}, Line=${lineIndex} (当前队列处理指针为 Row=${state.currentIndex}, Line=${state.currentLineIndex})，继续为该任务行独立执行下载保存，绝不丢弃！`);
    }
    if (!binding.readyIds.includes(mediaId)) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: mediaId (${mediaId}) 尚未就绪 (不在 readyIds 中)! 当前 readyIds:`, binding.readyIds);
      return false;
    }
    if (binding.downloading || binding.downloadInitiated) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 任务已发起下载或正在下载中 (downloading=${!!binding.downloading}, downloadInitiated=${!!binding.downloadInitiated})`);
      return false;
    }
    if (inFlightOrCompletedDownloads.has(`media:${mediaId}`) || (binding.submissionId && inFlightOrCompletedDownloads.has(`sub:${binding.submissionId}`))) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 媒体 (${mediaId}) 已在下载队列或已完成下载，跳过重复触发`);
      return false;
    }
    if (task.downloaded?.[lineIndex]) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 任务行已被标记为已下载完成 (downloaded=true)`);
      return false;
    }
    if (task.downloadIds?.[lineIndex]) {
      console.warn(`[FlowUI] [AutoDownload] 🚫 拒绝下载: 任务行已有活跃下载 ID (${task.downloadIds[lineIndex]})`);
      return false;
    }
    if (!binding.downloadPath) {
      console.error(`[FlowUI] [AutoDownload] ❌ 失败: 此任务缺少已绑定的下载文件名 (downloadPath 为空)`);
      updateLineStatus(rowIndex, lineIndex, "failed", "此任务缺少已绑定的下载文件名，已停止下载");
      return false;
    }
    binding.downloading = true;
    binding.downloadInitiated = true;
    inFlightOrCompletedDownloads.add(`media:${mediaId}`);
    if (binding.submissionId) inFlightOrCompletedDownloads.add(`sub:${binding.submissionId}`);
    downloadedUrlsSet.add(cleanUrl);
    try {
      task.videoUrls = task.videoUrls || [];
      task.videoUrls[lineIndex] = cleanUrl;
      await saveState();
      console.log(`[FlowUI] [AutoDownload] 🚀 发送下载请求到后台: URL=${cleanUrl}, Filename=${binding.downloadPath}, MediaId=${mediaId}`);
      const result = await sendToExtension("download", {
        url: cleanUrl, filename: binding.downloadPath,
        task: { rowIndex, lineIndex, taskId: task.taskId, submissionId: binding.submissionId,
          mediaId, projectId: getStorageKey(), download_path: binding.downloadPath }
      });
      console.log("[FlowUI] [AutoDownload] 后台下载响应结果:", result);
      if (!result?.success) {
        binding.downloadInitiated = false;
        inFlightOrCompletedDownloads.delete(`media:${mediaId}`);
        if (binding.submissionId) inFlightOrCompletedDownloads.delete(`sub:${binding.submissionId}`);
        console.error(`[FlowUI] [AutoDownload] ❌ 对应视频下载失败: ${result?.error || "无下载响应"}`);
        updateLineStatus(rowIndex, lineIndex, "failed", `对应视频下载失败: ${result?.error || "无下载响应"}`);
        return false;
      }
      task.downloadIds = task.downloadIds || [];
      if (!task.downloaded?.[lineIndex]) {
        task.downloadIds[lineIndex] = result.downloadId;
      }
      // 同步把该视频卡片所属的缩略图 URL 登记进 downloadedUrlsSet，防止 /asb/ 缩略图与视频 ID 不同时被后续任务重复识别
      try {
        const vEl = Array.from(document.querySelectorAll("flow-video-tile video, flow-grid-tile-container video, video")).find(v => {
          const vSrc = (v.getAttribute("src") || v.currentSrc || v.src || "").trim().replace(/&amp;/g, "&");
          return vSrc && mediaIdFromVideoUrl(vSrc) === mediaId;
        });
        if (vEl) {
          try {
            vEl.removeAttribute("autoplay");
            if (!vEl.paused) vEl.pause();
          } catch (_) {}
        }
        const tile = vEl && typeof vEl.closest === "function" ? (vEl.closest("flow-video-tile") || vEl.closest("flow-grid-tile-container")) : null;
        const thumbImg = tile ? tile.querySelector("img.thumbnail, img[src*='flow.google.com/asb/'], img[src*='flow-content.google/image/']") : null;
        const thumbSrc = thumbImg ? (thumbImg.getAttribute("src") || thumbImg.src || "").trim().replace(/&amp;/g, "&") : "";
        if (thumbSrc) downloadedUrlsSet.add(thumbSrc);
      } catch (_) {}
      console.log(`[FlowUI] [AutoDownload] ✅ 下载成功启动! downloadId=${result.downloadId}`);
      if (!task.downloaded?.[lineIndex] && task.status?.[lineIndex] !== "failed") updateLineStatus(rowIndex, lineIndex, "generating", `正在下载 ${binding.downloadPath}`);
      await saveState();
      return true;
    } catch (err) {
      binding.downloadInitiated = false;
      inFlightOrCompletedDownloads.delete(`media:${mediaId}`);
      if (binding.submissionId) inFlightOrCompletedDownloads.delete(`sub:${binding.submissionId}`);
      throw err;
    } finally { binding.downloading = false; }
  }

  // 记录每个视频图块的悬停唤醒次数与时间戳，避免对已加载/已下载的旧视频重复悬停和后台解码播放
  const tileHoverHistory = new WeakMap();

  function pauseVideoQuietly(videoEl) {
    if (!videoEl) return;
    try {
      videoEl.removeAttribute("autoplay");
      if (!videoEl.paused) videoEl.pause();
    } catch (_) {}
  }

  // 模拟真实用户鼠标悬停，仅针对尚未加载出 <video src> 的新生成视频卡片唤醒懒加载预览机制 (严禁滚动页面、严禁唤醒旧视频)
  function triggerTileHover(tile) {
    if (!tile) return;
    // 严禁触碰任何图片卡片
    if (tile.tagName === "FLOW-IMAGE-TILE" || (tile.querySelector && tile.querySelector("flow-image-tile")) || (typeof tile.closest === "function" && tile.closest("flow-image-tile"))) {
      return;
    }

    const videoTile = tile.tagName === "FLOW-VIDEO-TILE"
      ? tile
      : ((typeof tile.closest === "function" && tile.closest("flow-video-tile")) || (tile.querySelector && tile.querySelector("flow-video-tile")) || tile);
    const gridContainer = typeof videoTile.closest === "function" ? videoTile.closest("flow-grid-tile-container") : null;

    // 1. 如果该卡片内部已经渲染出有效签名直链的 <video>，说明懒加载早已完成，无需再派发悬停事件；
    //    若该视频已在已下载/初始快照集合中，顺便暂停其后台播放以释放 GPU/CPU 解码占用
    const existingVideo = videoTile.querySelector && videoTile.querySelector("video");
    if (existingVideo) {
      const vSrc = (existingVideo.getAttribute("src") || existingVideo.currentSrc || existingVideo.src || "").trim().replace(/&amp;/g, "&");
      if (vSrc && isValidFlowVideoUrl(vSrc)) {
        const vId = mediaIdFromVideoUrl(vSrc);
        if (downloadedUrlsSet.has(vSrc) || (vId && inFlightOrCompletedDownloads.has(`media:${vId}`)) || wasVideoSeen(vSrc, existingVideo)) {
          pauseVideoQuietly(existingVideo);
        }
        return;
      }
    }

    const thumbImg = videoTile.tagName === "IMG" ? videoTile : (videoTile.querySelector && videoTile.querySelector("img.thumbnail, img[src*='flow-content.google/image/'], img[src*='flow.google.com/asb/']"));
    const thumbSrc = thumbImg ? (thumbImg.getAttribute("src") || thumbImg.src || "").trim().replace(/&amp;/g, "&") : "";

    // 2. 如果缩略图本身已在已下载快照中，或其对应的 mediaId 已经拿到视频直链 / 已下载，直接跳过
    if (thumbSrc) {
      if (downloadedUrlsSet.has(thumbSrc)) return;
      const thumbId = mediaIdFromImageThumbnailUrl(thumbSrc);
      if (thumbId && (inFlightOrCompletedDownloads.has(`media:${thumbId}`) || discoveredVideoUrlsById.has(thumbId))) {
        return;
      }
    }

    // 3. 对同一个未就绪的新卡片做节流限制（间隔 >= 2.5s，且同一缩略图最多尝试唤醒 5 次），防止高频重排和事件风暴
    const now = Date.now();
    const histKey = gridContainer || videoTile;
    const prev = tileHoverHistory.get(histKey);
    if (prev && prev.thumbSrc === thumbSrc) {
      if (now - prev.lastAt < 2500 || prev.count >= 5) return;
      prev.count += 1;
      prev.lastAt = now;
    } else {
      tileHoverHistory.set(histKey, { thumbSrc, count: 1, lastAt: now });
    }

    const elementsToTrigger = [
      gridContainer,
      videoTile,
      thumbImg,
      videoTile.querySelector && videoTile.querySelector(".pre-hover-overlay")
    ].filter(Boolean);

    let clientX = 100, clientY = 100;
    try {
      const targetForRect = thumbImg || videoTile;
      const rect = targetForRect.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        clientX = Math.round(rect.left + rect.width / 2);
        clientY = Math.round(rect.top + rect.height / 2);
      }
    } catch (_) {}

    const eventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX,
      clientY,
      screenX: clientX,
      screenY: clientY,
      buttons: 0
    };

    const pointerInit = {
      ...eventInit,
      pointerId: 1,
      width: 1,
      height: 1,
      pressure: 0,
      isPrimary: true,
      pointerType: "mouse"
    };

    const createEvt = (type, init) => {
      try {
        if (type.startsWith("pointer") && typeof PointerEvent === "function") {
          return new PointerEvent(type, init);
        }
        if (typeof MouseEvent === "function") {
          return new MouseEvent(type, init);
        }
      } catch (_) {}
      return { type, bubbles: true, cancelable: true, ...init };
    };

    for (const el of [...new Set(elementsToTrigger)]) {
      try {
        if (typeof PointerEvent === "function" || (typeof window !== "undefined" && typeof window.PointerEvent === "function")) {
          el.dispatchEvent(createEvt("pointerover", pointerInit));
          el.dispatchEvent(createEvt("pointerenter", pointerInit));
          el.dispatchEvent(createEvt("pointermove", pointerInit));
        }
        el.dispatchEvent(createEvt("mouseover", eventInit));
        el.dispatchEvent(createEvt("mouseenter", eventInit));
        el.dispatchEvent(createEvt("mousemove", eventInit));
      } catch (_) {}
    }
  }

  // DOM 视频观察器：监听页面 <video> 标签及视频缩略图 <img class="thumbnail"> 渲染
  function setupDomVideoWatcher() {
    console.log("[FlowUI] 启动 DOM 级视频渲染观察器...");

    function checkVideoElement(videoEl) {
      if (!videoEl || (!state.isRunning && !window.__lzxFlowTransport?.hasPendingResults?.())) return;
      const rawUrl = videoEl.getAttribute("src") || videoEl.currentSrc || videoEl.src;
      const renderedUrl = (rawUrl || "").trim().replace(/&amp;/g, "&");
      if (!renderedUrl || !isValidFlowVideoUrl(renderedUrl)) return;
      const renderedId = mediaIdFromVideoUrl(renderedUrl);
      // 如果该视频属于已下载完成或初始历史快照，立即暂停其后台播放并静默跳过，彻底消除旧视频循环播放导致的卡顿
      if (downloadedUrlsSet.has(renderedUrl) || (renderedId && inFlightOrCompletedDownloads.has(`media:${renderedId}`))) {
        pauseVideoQuietly(videoEl);
        return;
      }
      if (renderedId) discoveredVideoUrlsById.set(renderedId, renderedUrl);
      let bound = findMediaBinding(renderedId);
      // 如果该任务视频已经下载完成或正在下载中，直接忽略，避免生命周期事件造成重复日志
      if (bound && (bound.task?.downloaded?.[bound.lineIndex] || bound.task?.downloadIds?.[bound.lineIndex] || bound.binding?.downloading || bound.binding?.downloadInitiated)) {
        pauseVideoQuietly(videoEl);
        return;
      }

      const isFlowProjectVideoUrl = /^https:\/\/flow-content\.google\/video\/[a-zA-Z0-9_-]+/i.test(renderedUrl) || /^https:\/\/flow\.google\.com\/asb\/[a-zA-Z0-9_-]+/i.test(renderedUrl);
      const inTile = isFlowProjectVideoUrl || !!(videoEl.closest("flow-video-tile") || videoEl.closest("flow-grid-tile-container") || videoEl.closest("[data-tile-id]"));

      // 核心救砖兜底：若网络 RPC 绑定的 UUID 与 DOM 实际视频直链 ID 不同 (如 /asb/ANqvLO...) 或 mediaIds 为空，
      // 只要当前任务尚未取得 readyIds 且 DOM 中新渲染出了提交前未见过的项目视频卡片，自动关联到当前任务！
      if (!window.__lzxFlowTransport && !bound && state.isRunning && renderedId && inTile && renderedUrl) {
        if (!isStaticShowcaseAsset(renderedUrl) && !wasVideoSeen(renderedUrl, videoEl)) {
          const activeTask = state.tasks[state.currentIndex];
          const activeBinding = activeTask?.mediaBindings?.[state.currentLineIndex];
          const isGenerating = activeTask?.status?.[state.currentLineIndex] === "generating" && !activeTask?.downloaded?.[state.currentLineIndex];

          if (isGenerating && activeBinding && (!activeBinding.readyIds || activeBinding.readyIds.length === 0)) {
            console.log(`[FlowUI] 🎯 [DOMWatcher] 识别到当前生成任务对应的新视频图块! 自动关联绑定: mediaId=${renderedId}, Row=${state.currentIndex}, Line=${state.currentLineIndex}`);
            activeBinding.mediaIds = Array.isArray(activeBinding.mediaIds) ? activeBinding.mediaIds : [];
            if (!activeBinding.mediaIds.includes(renderedId)) activeBinding.mediaIds.push(renderedId);
            if (!activeBinding.readyIds.includes(renderedId)) activeBinding.readyIds.push(renderedId);
            activeBinding.urls = activeBinding.urls || {};
            activeBinding.urls[renderedId] = renderedUrl;
            saveState();
            bound = { task: activeTask, binding: activeBinding, rowIndex: state.currentIndex, lineIndex: state.currentLineIndex };
          }
        }
      }

      if (bound && (videoEl.readyState >= 1 || videoEl.currentTime > 0 || isFlowProjectVideoUrl) && !videoEl.error && inTile) {
        console.log(`[FlowUI] [DOMWatcher] 视频元素已就绪 (readyState=${videoEl.readyState})，更新 readyId 并启动下载: renderedId=${renderedId}`);
        if (!bound.binding.readyIds.includes(renderedId)) bound.binding.readyIds.push(renderedId);
        if (window.__lzxFlowTransport && bound.task === state.tasks[state.currentIndex]) {
          window.__lzxFlowTransport.locateResult?.(videoEl.closest("flow-video-tile") || videoEl.closest("flow-grid-tile-container"), renderedId, bound.task.taskId);
        }
        bound.binding.urls = bound.binding.urls || {};
        bound.binding.urls[renderedId] = renderedUrl;
        triggerAutoDownload(renderedUrl, renderedId, !state.isRunning && window.__lzxFlowTransport ? bound.binding : null);
      }

      if (!bound && (renderedUrl.startsWith("http://") || renderedUrl.startsWith("https://"))) {
        if (!isStaticShowcaseAsset(renderedUrl) && !wasVideoSeen(renderedUrl, videoEl)) {
          const activeTask = state.tasks[state.currentIndex];
          const activeBinding = activeTask?.mediaBindings?.[state.currentLineIndex];
          const hasUnboundActiveWork = activeTask &&
            activeTask.status?.[state.currentLineIndex] === "generating" &&
            !activeTask.downloaded?.[state.currentLineIndex] &&
            !activeTask.downloadIds?.[state.currentLineIndex] &&
            (!activeBinding?.readyIds || activeBinding.readyIds.length === 0);
          if (hasUnboundActiveWork) {
            console.log("[FlowUI] DOM 观察器嗅探到新生成的视频元素:", renderedUrl.substring(0, 100));
            triggerAutoDownload(renderedUrl);
          }
        } else {
          pauseVideoQuietly(videoEl);
        }
      }
    }

    for (const eventName of ["loadedmetadata", "loadeddata"]) {
      document.addEventListener(eventName, event => {
        if (event.target?.tagName === "VIDEO" && (state.isRunning || window.__lzxFlowTransport?.hasPendingResults?.())) {
          checkVideoElement(event.target);
        }
      }, true);
    }
    // 监听 DOM 树中新增的 <video> 或新视频卡片
    const observer = new MutationObserver((mutations) => {
      if (!state.isRunning && !window.__lzxFlowTransport?.hasPendingResults?.()) return;
      for (const m of mutations) {
        if (m.addedNodes) {
          for (const node of m.addedNodes) {
            if (node.nodeType === Node.ELEMENT_NODE) {
              if (node.tagName === "VIDEO") {
                checkVideoElement(node);
              } else if (node.tagName === "FLOW-VIDEO-TILE" || (node.tagName === "IMG" && isVideoThumbnailImg(node))) {
                triggerTileHover(node);
              } else if (node.querySelector) {
                const v = node.querySelector("video");
                if (v) checkVideoElement(v);
                const tile = node.querySelector("flow-video-tile");
                if (tile) triggerTileHover(tile);
              }
            }
          }
        }
        if (m.type === "attributes" && m.target && m.target.tagName === "VIDEO") {
          checkVideoElement(m.target);
        }
      }
    });

    observer.observe(document.documentElement || document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["src"]
    });

    // 周期性保底扫描 (每 4 秒，且仅在未由 batchPromise 主循环轮询时触发，避免双重全表扫描)
    setInterval(() => {
      if ((!state.isRunning && !window.__lzxFlowTransport?.hasPendingResults?.()) || batchPromise) return;
      const hasActiveWork = (state.activeTasksQueue && state.activeTasksQueue.length > 0) ||
                            (state.tasks && state.tasks.some(t => t.status && t.status.some(s => s === "generating")));
      if (hasActiveWork) {
        document.querySelectorAll("flow-video-tile").forEach(triggerTileHover);
        document.querySelectorAll("flow-video-tile video, video").forEach(checkVideoElement);
      }
    }, 4000);
  }

  function setupEvents() {
    window.addEventListener("flow-task-media-bound", event => {
      const { submissionId, mediaIds, source } = event.detail || {};
      if (window.__lzxFlowTransport && source !== "generation_response") return;
      const process = window.currentProcess?.submissionId === submissionId ? window.currentProcess :
        window.__lzxFlowTransport?.findSubmission?.(submissionId);
      console.log("[FlowUI] 📥 收到 flow-task-media-bound 事件:", { submissionId, mediaIds, source, currentProcessSubId: process?.submissionId, isRunning: state.isRunning });
      if (!state.isRunning && !window.__lzxFlowTransport) {
        console.warn("[FlowUI] [media-bound] 忽略: state.isRunning 为 false");
        return;
      }
      if (!process) {
        console.warn("[FlowUI] [media-bound] 忽略: window.currentProcess 为空");
        return;
      }
      if (process.submissionId !== submissionId) {
        console.warn(`[FlowUI] [media-bound] 忽略: submissionId 不匹配 (当前=${process.submissionId}, 事件=${submissionId})`);
        return;
      }
      const taskRow = state.tasks[process.rowIndex];
      const binding = taskRow?.mediaBindings?.[process.lineIndex];
      if (binding?.submissionId !== submissionId) {
        console.warn(`[FlowUI] [media-bound] 忽略: 存储的 binding.submissionId (${binding?.submissionId}) 与事件 (${submissionId}) 不匹配`);
        return;
      }
      if (taskRow?.downloaded?.[process.lineIndex] || taskRow?.downloadIds?.[process.lineIndex] || binding?.downloading) {
        console.log("[FlowUI] [media-bound] 忽略: 当前任务已在下载或已完成下载");
        return;
      }
      if (source === "poll" && Array.isArray(binding.mediaIds) && binding.mediaIds.length > 0) {
        return;
      }
      if (window.__lzxFlowTransport) {
        if (binding.source === "generation_response" && binding.mediaIds?.length &&
            (binding.mediaIds.length !== mediaIds.length || binding.mediaIds.some(id => !mediaIds.includes(id)))) return;
        binding.source = "generation_response";
        binding.readyIds = (binding.readyIds || []).filter(id => mediaIds.includes(id));
        binding.urls = Object.fromEntries(Object.entries(binding.urls || {}).filter(([id]) => mediaIds.includes(id)));
      }
      binding.mediaIds = [...new Set(mediaIds.filter(id => typeof id === "string" && /^[a-zA-Z0-9_-]+$/.test(id)))];
      console.log(`[FlowUI] ✅ [media-bound] 成功绑定媒体 ID 到任务: Row=${process.rowIndex}, Line=${process.lineIndex}, mediaIds=`, binding.mediaIds);
      saveState();
    });
    window.addEventListener("flow-task-binding-missing", event => {
      const process = window.currentProcess;
      console.log("[FlowUI] ℹ️ 收到 flow-task-binding-missing 提示 (保持 generating 状态继续通过轮询帧/DOM 认领):", event.detail);
      if (!state.isRunning || !process || process.submissionId !== event.detail?.submissionId) {
        return;
      }
      window.__lzxFlowTransport?.bindingMissing?.(process);
    });
    window.addEventListener("flow-task-media-ready", async event => {
      const { mediaId, url } = event.detail || {};
      const cleanUrl = (url || "").trim().replace(/&amp;/g, "&");
      console.log(`[FlowUI] 📥 收到 flow-task-media-ready 事件: mediaId=${mediaId}, url=${cleanUrl.substring(0, 100)}`);
      if (!isValidFlowVideoUrl(cleanUrl)) {
        console.warn(`[FlowUI] [media-ready] 忽略未携带完整签名的视频 URL: ${cleanUrl}`);
        return;
      }
      const parsedId = mediaIdFromVideoUrl(cleanUrl);
      if (parsedId && parsedId !== mediaId) {
        console.warn(`[FlowUI] [media-ready] URL 解析出的 ID (${parsedId}) 与 mediaId (${mediaId}) 不一致! 忽略此事件`);
        return;
      }
      if (mediaId) discoveredVideoUrlsById.set(mediaId, cleanUrl);
      let match = findMediaBinding(mediaId);
      if (!window.__lzxFlowTransport && !match && state.isRunning && cleanUrl && !wasVideoSeen(cleanUrl) && !isStaticShowcaseAsset(cleanUrl)) {
        const activeTask = state.tasks[state.currentIndex];
        const activeBinding = activeTask?.mediaBindings?.[state.currentLineIndex];
        const isGenerating = activeTask?.status?.[state.currentLineIndex] === "generating" && !activeTask?.downloaded?.[state.currentLineIndex];
        if (isGenerating && activeBinding && (!activeBinding.mediaIds || activeBinding.mediaIds.length === 0)) {
          console.log(`[FlowUI] 🎯 [media-ready] 自动将新就绪视频 (mediaId=${mediaId}) 绑定到当前生成任务 Row=${state.currentIndex}, Line=${state.currentLineIndex}`);
          activeBinding.mediaIds = [mediaId];
          match = { task: activeTask, binding: activeBinding, rowIndex: state.currentIndex, lineIndex: state.currentLineIndex };
        }
      }
      if (!match) {
        console.log(`[FlowUI] [media-ready] 已缓存视频直链 (mediaId=${mediaId})，等待对应任务认领`);
        return;
      }
      console.log(`[FlowUI] 🎯 [media-ready] 匹配到行 Row=${match.rowIndex}, Line=${match.lineIndex}, 添加至 readyIds 并触发下载...`);
      if (!match.binding.readyIds.includes(mediaId)) match.binding.readyIds.push(mediaId);
      match.binding.urls = match.binding.urls || {};
      match.binding.urls[mediaId] = cleanUrl;
      await saveState();
      await triggerAutoDownload(cleanUrl, mediaId, !state.isRunning && window.__lzxFlowTransport ? match.binding : null);
    });
    setupDomVideoWatcher();
  }

  // ----------------------------------------------------
  // 9. 辅助工具方法
  // ----------------------------------------------------
  function splitTextToLines(text) {
    return text ? text.split("\n").map(l => l.trim()).filter(Boolean) : [];
  }

  function calculateDuration(text) {
    const len = text.length;
    if (len <= state.settings.maxLetterFor4s) return 4;
    if (len <= state.settings.maxLetterFor6s) return 6;
    return 8;
  }

  function parsePreset(presetContent) {
    if (!presetContent) return { before: "", after: "" };
    const matches = [...presetContent.matchAll(/\n+/g)];
    if (matches.length === 0) return { before: presetContent, after: "" };
    const longest = matches.reduce((max, curr) => curr[0].length > max[0].length ? curr : max);
    const index = longest.index;
    return {
      before: presetContent.substring(0, index).trim(),
      after: presetContent.substring(index + longest[0].length).trim()
    };
  }

  function getProjectName() {
    if (isVidsPage()) {
      try {
        const titleInput = document.querySelector("input.docs-title-input");
        if (titleInput && titleInput.value && titleInput.value.trim()) return titleInput.value.trim();
        const titleLabel = document.querySelector("#docs-title-input-label-inner");
        if (titleLabel && titleLabel.textContent && titleLabel.textContent.trim()) return titleLabel.textContent.trim();
        const vidsMatch = location.pathname.match(/\/videos\/d\/([a-zA-Z0-9_-]+)/);
        if (vidsMatch) return vidsMatch[1];
      } catch (e) {}
      return "Vids_Project";
    }

    try {
      const inputEl = document.querySelector("flow-editable-text input.editable-text-input");
      if (inputEl && inputEl.value) return inputEl.value.trim();
    } catch (e) {}

    const el = document.querySelector("flow-editable-text");
    if (el && el.textContent.trim()) return el.textContent.trim();

    const match = location.pathname.match(/\/project\/([a-zA-Z0-9_-]+)/);
    if (match) return match[1];

    if (document.title) {
      const t = document.title.replace(/^\[FLOWCLOSE-[^\]]*\]\s*/, "").replace(/^📍+\s*/, "").replace(/^\[FC-[0-9A-Z]{4,8}[^\]]*\]\s*/, "").replace(/ - Google Flow.*/i, "").trim();
      if (t && !t.includes("Google Flow")) return t;
    }

    return "Flow_Project";
  }

  function sanitizePathName(name) {
    return String(name || "").replace(/[\\/:*?"<>|\r\n\t]/g, "_").replace(/\.+$/, "").trim();
  }

  function delay(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  function extendArray(arr, size, fillValue) {
    const res = [...(arr || [])];
    while (res.length < size) res.push(fillValue);
    if (res.length > size) res.length = size;
    return res;
  }

  // 全局托管模式：桥接模块在运行托管批次时设置此回调，把每行状态变化实时上报给桌面软件
  let hostedStatusHook = null;
  let lastGeneratingSaveAt = 0;

  function updateLineStatus(rowIndex, lineIndex, status, message) {
    const task = state.tasks[rowIndex];
    if (task) {
      const prevStatus = task.status?.[lineIndex];
      const prevMsg = task.message?.[lineIndex];
      if (prevStatus === status && prevMsg === message) return;

      task.status[lineIndex] = status;
      task.message[lineIndex] = message;

      const isProgressTick = (prevStatus === "generating" && status === "generating");
      const now = Date.now();
      if (!isProgressTick || (now - lastGeneratingSaveAt >= 12000)) {
        lastGeneratingSaveAt = now;
        saveState();
      }

      // 仅当状态发生实质变化，或控制面板处于打开可见状态时，才更新表格单元格 DOM
      const isPanelVisible = !!(overlayEl && overlayEl.classList.contains("active"));
      if (!isProgressTick || isPanelVisible) {
        const rows = document.querySelectorAll("#flow-tasks-table tbody tr");
        if (rows && rows[rowIndex]) {
          renderTableStatusCell(rows[rowIndex], task, rowIndex);
        }
      }
      if (typeof hostedStatusHook === "function") {
        try { hostedStatusHook(rowIndex, lineIndex, status, message); } catch (_) {}
      }
    }
  }

  // ----------------------------------------------------
  // 9. 异常活动（安全限制）监测与声光报警逻辑
  // ----------------------------------------------------
  let alarmTimer = null;
  let alarmCountdown = 45;
  let activityCheckInterval = null;

  function checkForUnusualActivity() {
    // 使用 textContent 替代 innerText，避免每轮检查触发全页面同步 Layout 重排
    const text = (document.body && document.body.textContent) || "";
    return text.includes("异常活动") || text.includes("異常活動") || text.includes("不尋常的活動") || text.includes("Unusual activity") || text.includes("unusual activity");
  }

  function playAlarmSound() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const playBeep = (freq, startTime, duration) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, startTime);
        gain.gain.setValueAtTime(0.35, startTime);
        gain.gain.exponentialRampToValueAtTime(0.01, startTime + duration);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(startTime);
        osc.stop(startTime + duration);
      };
      playBeep(880, ctx.currentTime, 0.15);
      playBeep(880, ctx.currentTime + 0.25, 0.15);
    } catch (e) {
      console.warn("[FlowUI] 无法播放报警提示音:", e);
    }
  }

  function startActivityCheck() {
    if (activityCheckInterval) clearInterval(activityCheckInterval);
    activityCheckInterval = setInterval(() => {
      if (state.isRunning && !state.isSuspended) {
        if (checkForUnusualActivity()) {
          handleUnusualActivityDetected();
        }
      }
    }, 2500);
  }

  function stopActivityCheck() {
    if (activityCheckInterval) {
      clearInterval(activityCheckInterval);
      activityCheckInterval = null;
    }
  }

  function handleUnusualActivityDetected() {
    console.warn("[FlowUI] 🚨 触发安全限制警告 (检测到异常活动)！已挂起任务。");
    state.isSuspended = true;
    saveState();
    
    if (alarmDialogEl) {
      alarmDialogEl.style.display = "flex";
    }
    
    playAlarmSound();
    
    if (window.__lzxFlowTransport) {
      if (alarmTimer) clearInterval(alarmTimer);
      alarmTimer = null;
      const notice = document.getElementById("flow-alarm-message");
      if (notice) notice.innerText = "网站提示安全限制，队列已暂停；保留当前页面，不自动刷新或重复生成。";
      window.__lzxFlowTransport.sitePaused?.();
      return;
    }
    alarmCountdown = 45;
    const msgEl = document.getElementById("flow-alarm-message");
    if (msgEl) {
      msgEl.style.color = "#f59e0b";
      msgEl.innerText = `系统将在 ${alarmCountdown} 秒后自动刷新网页以尝试恢复，您也可以立即进行手动处理...`;
    }
    
    if (alarmTimer) clearInterval(alarmTimer);
    alarmTimer = setInterval(() => {
      alarmCountdown--;
      if (alarmCountdown <= 0) {
        clearInterval(alarmTimer);
        console.log("[FlowUI] 安全倒计时结束，执行网页刷新恢复...");
        saveState().then(() => {
          location.reload();
        });
      } else {
        if (msgEl) {
          msgEl.innerText = `系统将在 ${alarmCountdown} 秒后自动刷新网页以尝试恢复，您也可以立即进行手动处理...`;
        }
        if (alarmCountdown % 3 === 0) {
          playAlarmSound();
        }
      }
    }, 1000);
  }

  function pauseAlarmCountdown() {
    if (alarmTimer) {
      clearInterval(alarmTimer);
      alarmTimer = null;
    }
    const msgEl = document.getElementById("flow-alarm-message");
    if (msgEl) {
      msgEl.innerText = "倒计时已暂停。请手动消除网页弹窗限制，消除后点击下方“继续运行”。";
      msgEl.style.color = "#10b981";
    }
    showToast("自动刷新倒计时已暂停，等待您的手动处理。");
  }

  function resumeFromAlarm() {
    if (alarmTimer) {
      clearInterval(alarmTimer);
      alarmTimer = null;
    }
    if (alarmDialogEl) {
      alarmDialogEl.style.setProperty("display", "none", "important");
    }
    state.isSuspended = false;
    saveState();
    showToast("安全限制挂起解除，继续批量生成...");
    
    if (state.isRunning && !batchPromise) {
      startBatch();
    }
  }

  // ----------------------------------------------------
  // 10. 初始化与断点重载自启动运行
  // ----------------------------------------------------
  async function init() {
    console.log("[FlowUI] DOMContentLoaded 触发，开始正式初始化 init()...");
    createUI();

    // 如果之前在 document_start 时挂在 documentElement 上，移入正式的 document.body
    if (document.body) {
      if (fabEl && fabEl.parentNode !== document.body) {
        document.body.appendChild(fabEl);
      }
      if (overlayEl && overlayEl.parentNode !== document.body) {
        document.body.appendChild(overlayEl);
      }
    }

    setupEvents();

    // 拍摄页面已有展示视频与视频缩略图基线快照：把页面初始自带的样例或历史视频全部登记为已存在，严防误下载
    function snapshotExistingVideos() {
      snapshotCurrentDomMedia();
      console.log(`[FlowUI] 拍摄初始视频及缩略图快照完成，当前已登记 ${downloadedUrlsSet.size} 条媒体标识`);
    }
    snapshotExistingVideos();
    window.addEventListener("load", () => snapshotExistingVideos());

    // 启动单页应用保活检查：防止 Google Flow 路由二次重绘冲掉挂载在 body 上的 FAB
    setInterval(() => {
      createUI();
    }, 3500);

    // 加载先前保存的状态
    await loadState();
    
    // 如果网页刚才由于刷新中断且处于 Running 状态，则自动断点续跑启动批量任务
    if (state.isRunning) {
      console.log("[FlowUI] 检测到批量运行状态为 TRUE，自动恢复运行断点续传。任务索引:", state.currentIndex, "子行索引:", state.currentLineIndex);
      showToast("正在自动恢复未完成的生成任务，请保持浏览器窗口在前台...");
      
      // 启动安全监测
      startActivityCheck();
      
      // 等待 2.5 秒，保证页面 NextJS 初始化以及 trpc Webpack 完成，然后自启
      setTimeout(() => {
        startBatch();
      }, 2500);
    }
  }

  // 多时机激进挂载：保证在任何阶段都能立即渲染悬浮按钮
  try {
    createUI();
  } catch (e) {
    console.warn("[FlowUI] 首次即时挂载暂缓:", e);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  window.addEventListener("load", () => createUI());

  console.log("%c[FlowUI] 界面控制器加载完毕！", "background: #8b5cf6; color: white; padding: 2px 6px; border-radius: 4px;");

  // ====================================================================
  // 轻量账号桥接 (用于桌面软件的「全局托管模式」通信)
  // - 支持 Google Flow 与 Google Vids (docs.google.com/videos/*)
  // - 只保持一个 WebSocket 连接，不做任何定时 DOM 读取
  // ====================================================================
  (function initAccountBridge() {
    const host = location.hostname;
    if (!(host.endsWith("labs.google") || host.endsWith("flow.google.com") || isVidsPage())) return;
    if (window.__flowAccountBridgeLoaded) return;
    window.__flowAccountBridgeLoaded = true;

    // 针对多开指纹浏览器（比特、AdsPower、MoreLogin 等）本地代理可能劫持 127.0.0.1 的情况，
    // 提供 IPv4 / localhost / IPv6 [::1] 及多端口 (18188, 8000, 8765) 自动轮询与 FlowCue 双向握手校验
    const WS_ENDPOINTS = [
      "ws://127.0.0.1:18188",
      "ws://localhost:18188",
      "ws://[::1]:18188",
      "ws://127.0.0.1:8000",
      "ws://localhost:8000",
      "ws://[::1]:8000",
      "ws://127.0.0.1:8765",
      "ws://localhost:8765",
      "ws://[::1]:8765"
    ];
    let endpointIdx = 0;
    const clientId = "flow_ext_" + Math.random().toString(36).substring(2, 9);
    let socket = null;
    let handshakeVerified = false;
    let handshakeTimer = null;
    let retryDelay = 2000;
    const MAX_RETRY = 8000;
    let cachedPoints = null;
    let cachedEmail = "";
    let lastProbeAt = 0;
    let probing = null;

    const log = (...a) => console.log("[FlowAccount]", ...a);
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const EMAIL_RE = /([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/;

    // ===== 浏览器唯一身份（由插件后台生成并永久保存在本浏览器配置中）=====
    let identity = { instance_id: "", instance_name: "", browser_session: "" };
    async function loadIdentity() {
      for (let i = 0; i < 2 && !identity.instance_id; i++) {
        const r = await Promise.race([sendToExtension("getInstanceInfo", {}), sleep(5000).then(() => null)]);
        if (r && r.instance_id) {
          identity = { instance_id: r.instance_id, instance_name: r.instance_name || "", browser_session: r.browser_session || "" };
          break;
        }
        await sleep(1000);
      }
      if (identity.instance_id) log("本浏览器唯一 ID:", identity.instance_id, identity.instance_name || "");
      else log("⚠️ 未能获取浏览器唯一 ID（插件后台可能未更新，请在扩展管理页刷新插件）");
      applyTitleBadge();
      return identity;
    }
    function identityFields() {
      return { instance_id: identity.instance_id, instance_name: identity.instance_name, browser_session: identity.browser_session };
    }

    // 在窗口标题前加上 [FC-XXXXX 名称]，多开 100+ 个浏览器时可在任务栏 / Alt+Tab 中直接看到是哪一个
    const TITLE_BADGE_RE = /^\[FC-[0-9A-Z]{4,8}[^\]]*\]\s*/;
    const CLOSE_MARK_RE = /^\[FLOWCLOSE-[^\]]*\]\s*/;
    let locateFlashUntil = 0;
    function applyTitleBadge() {
      if (!identity.instance_id) return;
      try {
        const cur = document.title || "";
        const closeMark = (cur.match(CLOSE_MARK_RE) || [""])[0];
        let rest = cur.replace(CLOSE_MARK_RE, "").replace(/^📍+\s*/, "").replace(TITLE_BADGE_RE, "");
        const pin = Date.now() < locateFlashUntil ? "📍📍📍 " : "";
        const badge = `[${identity.instance_id}${identity.instance_name ? " " + identity.instance_name : ""}] `;
        const want = closeMark + pin + badge + rest;
        if (cur !== want) document.title = want;
      } catch (_) {}
    }
    setInterval(applyTitleBadge, 3500);

    // 在页面上显示醒目的定位横幅
    function showLocateBanner(seconds) {
      try {
        const old = document.getElementById("flowcue-locate-banner");
        if (old) old.remove();
        const el = document.createElement("div");
        el.id = "flowcue-locate-banner";
        el.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:22px 10px;text-align:center;" +
          "font:bold 28px/1.4 system-ui,sans-serif;color:#fff;background:linear-gradient(90deg,#dc2626,#f59e0b,#dc2626);" +
          "box-shadow:0 6px 30px rgba(0,0,0,.6);pointer-events:none;";
        el.textContent = `📍 FlowCue 正在定位这个浏览器：${identity.instance_id}${identity.instance_name ? "（" + identity.instance_name + "）" : ""}${cachedEmail ? "  ·  " + cachedEmail : ""}`;
        (document.body || document.documentElement).appendChild(el);
        let on = true;
        const iv = setInterval(() => { on = !on; el.style.opacity = on ? "1" : "0.55"; }, 400);
        setTimeout(() => { clearInterval(iv); el.remove(); }, seconds * 1000);
      } catch (_) {}
    }

    // 精确提取邮箱：
    //   旧做法对整块 textContent 做正则，显示名与邮箱紧挨在一起时会把名字拼进邮箱（多字母），
    //   或卡片被截断时少字母。现改为：优先找「整段文本恰好就是一个邮箱」的元素，其次找「(邮箱)」格式。
    const EXACT_EMAIL_RE = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$/;
    function exactEmailIn(root) {
      if (!root) return "";
      try {
        const nodes = root.querySelectorAll("*");
        for (const el of nodes) {
          if (el.children.length) continue;
          const t = (el.textContent || "").trim();
          if (t.length <= 254 && EXACT_EMAIL_RE.test(t)) return t.toLowerCase();
        }
      } catch (_) {}
      return "";
    }
    function emailFromAttr(raw) {
      if (!raw) return "";
      const paren = raw.match(/[(（<]\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\s*[)）>]/);
      if (paren) return paren[1].toLowerCase();
      // 邮箱前后必须是空白/冒号/行首尾，避免把显示名拼进来
      const iso = raw.match(/(?:^|[\s:：,，])([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(?=$|[\s,，)）])/);
      return iso ? iso[1].toLowerCase() : "";
    }

    function readEmailCheap() {
      if (cachedEmail) return cachedEmail;
      try {
        // Google Vids 页面在 head meta[name="og-profile-acct"] 中直接带有登录邮箱
        const metaAcct = document.querySelector('meta[name="og-profile-acct"]');
        if (metaAcct && metaAcct.content) {
          const m = metaAcct.content.trim().match(EXACT_EMAIL_RE) ? metaAcct.content.trim() : emailFromAttr(metaAcct.content);
          if (m) return (cachedEmail = m.toLowerCase());
        }
        const panel = document.querySelector("flow-account-panel, .flow-account-panel-overlay, .cdk-overlay-pane");
        const exact = exactEmailIn(panel);
        if (exact) return (cachedEmail = exact);
        const els = document.querySelectorAll("[aria-label*='@'], [title*='@'], [data-email*='@']");
        for (const el of els) {
          const de = (el.getAttribute("data-email") || "").trim();
          if (EXACT_EMAIL_RE.test(de)) return (cachedEmail = de.toLowerCase());
          const m = emailFromAttr(el.getAttribute("aria-label") || el.getAttribute("title") || "");
          if (m) return (cachedEmail = m);
        }
        // 最后兜底（不缓存，后续精确读取到时会覆盖）
        if (panel) {
          const loose = (panel.textContent || "").match(EMAIL_RE);
          if (loose) return loose[1].toLowerCase();
        }
      } catch (e) {}
      return "";
    }

    function parseCreditNumber(str) {
      if (!str) return null;
      const clean = String(str).replace(/,/g, "");
      const m = clean.match(/(\d+)/);
      if (!m) return null;
      const n = parseInt(m[1], 10);
      return (!isNaN(n) && n >= 0 && n < 1000000) ? n : null;
    }

    function readPointsFromDom() {
      if (isVidsPage()) return 9999; // Google Vids 无 50 积分限制
      try {
        const span = document.querySelector("span.credits-count, [class*='credits-count']");
        if (span) {
          const n = parseCreditNumber(span.textContent);
          if (n !== null) return n;
        }
        const link = document.querySelector("a.credits-link, a[href*='flow_ai_credits_page'], a[href*='credits']");
        if (link) {
          const n = parseCreditNumber(link.textContent);
          if (n !== null) return n;
        }
        const containers = document.querySelectorAll("flow-account-panel, .flow-account-panel-overlay, .cdk-overlay-pane");
        for (const panel of containers) {
          for (const el of panel.querySelectorAll("span, div, a, p, button")) {
            if (el.children.length > 3) continue;
            const t = (el.textContent || "").trim();
            if (!t) continue;
            if (t.includes("点数") || t.includes("點數") || t.includes("积分") || t.includes("積分") || /credits|points|ai\s*credits/i.test(t) || /\b[\d,]+\s*點\b/.test(t)) {
              let n = parseCreditNumber(t);
              if (n !== null) return n;
              // 兼容英文/窄窗口下文字与数字分属相邻兄弟节点的情况 (例如 <span>AI Credits</span><span>1,000</span>)
              if (el.parentElement) {
                n = parseCreditNumber(el.parentElement.textContent);
                if (n !== null) return n;
              }
            }
          }
        }
      } catch (e) {}
      return null;
    }

    async function doProbe() {
      if (isVidsPage()) {
        readEmailCheap();
        return 9999; // Google Vids 无积分上限限制
      }
      // 菜单已打开时直接读
      let pts = readPointsFromDom();
      if (pts !== null) { readEmailCheap(); return pts; }

      const avatarBtn = document.querySelector('div.header-user-button[role="button"]') ||
                        document.querySelector("div.header-user-button") ||
                        document.querySelector('[aria-label="账号详情"], [aria-label="帳戶詳細資料"], [aria-label*="账号"], [aria-label*="帳戶"], [aria-label*="帳號"], [aria-label*="Account" i], [aria-label*="Google Account" i], [aria-label*="profile" i]') ||
                        document.querySelector('flow-header-user-icon [role="button"], flow-header-user-icon button, flow-header-user-icon img.avatar');
      if (!avatarBtn) { log("未找到头像按钮"); return null; }

      avatarBtn.click();
      // 等待菜单渲染（最多约 3 秒）
      for (let i = 0; i < 10 && pts === null; i++) {
        await sleep(300);
        pts = readPointsFromDom();
      }
      const panel = document.querySelector("flow-account-panel, .flow-account-panel-overlay, .cdk-overlay-pane");
      if (panel) {
        const exact = exactEmailIn(panel);
        if (exact) cachedEmail = exact;
      }
      // 关闭菜单
      await sleep(100);
      const closer = document.querySelector("flow-account-panel button.close-button") ||
                     document.querySelector(".cdk-overlay-backdrop");
      if (closer) closer.click();
      else document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      return pts;
    }

    async function probePoints() {
      // 3 秒内重复请求直接返回缓存，避免频繁点击
      if (Date.now() - lastProbeAt < 3000 && cachedPoints !== null) return cachedPoints;
      if (probing) return probing;
      probing = (async () => {
        try {
          const pts = await doProbe();
          if (pts !== null) cachedPoints = pts;
          lastProbeAt = Date.now();
          log("积分:", cachedPoints, "账号:", cachedEmail || readEmailCheap());
          return cachedPoints;
        } catch (e) {
          log("读取积分失败", e);
          return cachedPoints;
        } finally {
          probing = null;
        }
      })();
      return probing;
    }

    function countMediaTiles() {
      const main = document.querySelector("main") || document.body;
      const imgs = Array.from(main.querySelectorAll("img")).filter(i => {
        const r = i.getBoundingClientRect();
        return r.width > 60 && r.height > 60 && !i.closest("#flow-batch-overlay");
      });
      const vids = Array.from(main.querySelectorAll("video")).filter(v => !v.closest("#flow-batch-overlay"));
      return imgs.length + vids.length;
    }

    function fireKey(type, key, code, keyCode, ctrl) {
      const ev = new KeyboardEvent(type, { key, code, ctrlKey: !!ctrl, bubbles: true, cancelable: true, composed: true, view: window });
      try {
        Object.defineProperty(ev, "keyCode", { get: () => keyCode });
        Object.defineProperty(ev, "which", { get: () => keyCode });
      } catch (e) {}
      document.body.dispatchEvent(ev);
    }

    function findConfirmDeleteButton() {
      const btns = Array.from(document.querySelectorAll(
        "[role='dialog'] button, mat-dialog-container button, .cdk-overlay-pane button, [role='alertdialog'] button"));
      return btns.find(b => {
        const t = (b.textContent || "").trim();
        return b.offsetParent && /^(删除|刪除|永久删除|永久刪除|移至回收站|移到回收站|移至垃圾桶|移到垃圾桶|确定|確定|确认|確認|Delete|Move to trash|Confirm)/i.test(t);
      });
    }

    async function clearFlowMedia() {
      // 关闭插件面板，避免焦点在面板/输入框里（否则 Ctrl+A 会选中文字）
      try { if (typeof closePanel === "function") closePanel(); } catch (e) {}
      try { if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); } catch (e) {}
      window.focus();
      await sleep(300);

      const before = countMediaTiles();
      if (before === 0) return { success: true, before: 0, after: 0, note: "当前页面没有媒体" };

      fireKey("keydown", "a", "KeyA", 65, true);
      fireKey("keyup", "a", "KeyA", 65, true);
      await sleep(800);
      fireKey("keydown", "Delete", "Delete", 46, false);
      fireKey("keyup", "Delete", "Delete", 46, false);

      // 若弹出确认框，自动点击确认
      let confirmed = false;
      for (let i = 0; i < 10; i++) {
        await sleep(300);
        const b = findConfirmDeleteButton();
        if (b) { b.click(); confirmed = true; break; }
      }

      // 等待媒体消失
      let after = countMediaTiles();
      for (let i = 0; i < 15 && after > 0; i++) {
        await sleep(400);
        after = countMediaTiles();
      }
      log(`清空媒体: 删除前 ${before} 个, 删除后 ${after} 个, 确认框=${confirmed}`);
      if (after < before) { try { clearProjectUploadRegistry(); } catch (_) {} }
      return { success: after < before, before, after, confirmed,
               error: after >= before ? "模拟 Ctrl+A / Delete 未生效" : "" };
    }

    function send(obj) {
      if (window.__lzxFlowTransport) { window.__lzxFlowTransport.send(obj); return; }
      try {
        if (socket && socket.readyState === WebSocket.OPEN) {
          if (identity.instance_id && obj && typeof obj === "object" && !obj.instance_id) Object.assign(obj, identityFields());
          socket.send(JSON.stringify(obj));
        }
      } catch (e) {}
    }

    function confirmHandshake(wsUrl) {
      if (handshakeVerified) return;
      handshakeVerified = true;
      if (handshakeTimer) {
        clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
      retryDelay = 2000;
      log(`✅ 已连接桌面软件 (${wsUrl})`);
    }

    async function handleMessage(msg, wsUrl) {
      const action = msg.action || msg.type;
      if (action === "server_hello" || action === "pong" || msg.server === "FlowCue") {
        confirmHandshake(wsUrl);
        if (action === "server_hello" || action === "pong") return;
      }
      if (["ping", "get_points", "clear_tasks", "clear_media", "upload_images", "run_tasks", "stop_tasks", "close_browser",
           "locate_browser", "set_instance_name", "reset_instance_id"].includes(action)) {
        confirmHandshake(wsUrl);
      }

      if (action === "ping") { send({ type: "pong" }); return; }

      // ===== 定位浏览器：窗口切到最前 + 页面横幅 + 标题闪烁 =====
      if (action === "locate_browser") {
        const secs = Math.max(3, Math.min(30, Number((msg.params || {}).seconds) || 10));
        locateFlashUntil = Date.now() + secs * 1000;
        applyTitleBadge();
        showLocateBanner(secs);
        const r = await sendToExtension("focusMyTab", {});
        setTimeout(applyTitleBadge, secs * 1000 + 100);
        send({ action: "locate_browser_response", request_id: msg.request_id, success: true, client_id: clientId,
               data: { focused: !!(r && r.ok), error: (r && r.error) || "" } });
        return;
      }

      if (action === "set_instance_name") {
        const name = String((msg.params || {}).name || "");
        const r = await sendToExtension("setInstanceName", { name });
        if (r && r.instance_id) identity.instance_name = r.instance_name || "";
        applyTitleBadge();
        send({ action: "set_instance_name_response", request_id: msg.request_id, success: !!(r && r.instance_id), client_id: clientId });
        send({ type: "online", client_id: clientId, email: readEmailCheap(), url: location.href });
        return;
      }

      // 桌面软件检测到 ID 重复（浏览器配置被复制）时，要求本浏览器重新生成 ID 并重连
      if (action === "reset_instance_id") {
        const r = await sendToExtension("resetInstanceId", {});
        if (r && r.instance_id) {
          log("已重新生成浏览器唯一 ID:", identity.instance_id, "→", r.instance_id);
          identity = { instance_id: r.instance_id, instance_name: r.instance_name || "", browser_session: r.browser_session || "" };
          applyTitleBadge();
        }
        try { socket && socket.close(); } catch (_) {}
        return;
      }

      if (action === "get_points") {
        cachedEmail = ""; cachedPoints = null; lastProbeAt = 0;
        const pts = await probePoints();
        send({
          action: "get_points_response",
          request_id: msg.request_id,
          success: pts !== null,
          client_id: clientId,
          email: cachedEmail || readEmailCheap(),
          data: {
            remaining_points: pts,
            email: cachedEmail || readEmailCheap(),
            client_id: clientId,
            mode: isVidsPage() ? "vids" : "flow",
            unlimited: isVidsPage()
          }
        });
        return;
      }

      if (action === "clear_tasks") {
        let ok = true, err = "";
        try {
          if (typeof batchPromise !== "undefined" && batchPromise) {
            ok = false; err = "插件队列正在运行，请先停止";
          } else {
            const n = (state.tasks || []).length;
            state.tasks = [];
            state.taskRecord = {};
            state.activeTasksQueue = [];
            state.currentIndex = 0;
            state.currentLineIndex = 0;
            state.retryCount = 0;
            try { renderTable(); } catch (e) {}
            await saveState();
            try { showToast("已由桌面软件清空全部任务列表"); } catch (e) {}
            log("已清空任务列表, 原任务数:", n);
            err = String(n);
          }
        } catch (e) { ok = false; err = String(e && e.message || e); }
        send({ action: "clear_tasks_response", request_id: msg.request_id, success: ok,
               error: ok ? "" : err, data: { cleared: ok ? parseInt(err, 10) || 0 : 0 } });
        return;
      }

      if (action === "clear_media") {
        let result = { success: false };
        try { result = await clearFlowMedia(); } catch (e) { result = { success: false, error: String(e && e.message || e) }; }
        send({ action: "clear_media_response", request_id: msg.request_id, success: !!result.success,
               error: result.error || "", data: result });
        return;
      }

      // ===== 全局托管模式 =====
      if (action === "upload_images") {
        const p = msg.params || {};
        let data;
        if (isVidsPage()) {
          // Google Vids 模式下无需预先批量上传图片，而是在执行任务时逐个上传到「添加动画」输入框
          data = { uploaded: p.paths || [], skipped: [], failed: [], errors: {}, mode: "vids" };
        } else {
          try {
            data = await uploadPathsToFlow(p.paths || [], { timeoutMs: Math.max(10, Number(p.timeout_seconds) || 60) * 1000 });
          } catch (e) {
            data = { uploaded: [], skipped: [], failed: p.paths || [], errors: { _all: String(e && e.message || e) } };
          }
        }
        send({ action: "upload_images_response", request_id: msg.request_id, success: !data.failed.length,
               client_id: clientId, data: { ...data, upload_id: p.upload_id || "" } });
        return;
      }

      if (action === "run_tasks") {
        const p = msg.params || {};
        const batchId = p.batch_id || "";
        const reply = (ok, error, extra = {}) => send({ action: "run_tasks_response", request_id: msg.request_id, success: ok,
                                                       error: error || "", client_id: clientId, data: { batch_id: batchId, ...extra } });
        if (typeof batchPromise !== "undefined" && batchPromise) { reply(false, "插件队列正在运行，请先停止"); return; }
        if (!location.href.includes("/project/") && !isVidsPage()) { reply(false, "浏览器当前不在 Flow 或 Google Vids 项目页面"); return; }
        let count = 0;
        try {
          if (p.clear_existing !== false) {
            state.tasks = []; state.taskRecord = {}; state.activeTasksQueue = [];
            state.currentIndex = 0; state.currentLineIndex = 0; state.retryCount = 0;
          }
          count = importTaskList(Array.isArray(p.tasks) ? p.tasks : []);
          try { renderTable(); } catch (_) {}
          await saveState();
        } catch (e) { reply(false, "导入任务失败：" + String(e && e.message || e)); return; }
        if (!count) { reply(false, "没有可导入的任务"); return; }

        try { showToast(`🤖 托管模式：已接收 ${count} 个任务，开始生成`, 4000); } catch (_) {}

        hostedStatusHook = (r, l, status, message) => {
          if (status !== "success" && status !== "failed") return;
          const t = state.tasks[r] || {};
          send({ type: "hosted_task_status", batch_id: batchId, client_id: clientId, row: r, line: l, status, message: message || "",
                 download_path: t.download_path || "", image_name: t.image?.displayName || "" });
        };
        try { const executing = startBatch(); reply(true, "", { count }); await executing; } catch (e) { log("托管批次异常", e); }
        hostedStatusHook = null;

        const results = state.tasks.map((t, i) => {
          const st = Array.isArray(t.status) ? t.status : [];
          const ok = st.length > 0 && st.every(s => s === "success");
          const msgs = (Array.isArray(t.message) ? t.message : []).filter(Boolean);
          return { row: i, download_path: t.download_path || "", image_name: t.image?.displayName || "",
                   status: ok ? "success" : "failed", message: ok ? "" : (msgs[msgs.length - 1] || "未完成") };
        });
        send({ type: "hosted_batch_finished", batch_id: batchId, client_id: clientId, results,
               stopped: typeof isStopRequested !== "undefined" && !!isStopRequested });
        return;
      }

      if (action === "stop_tasks") {
        try { if (typeof batchPromise !== "undefined" && batchPromise) stopBatch(); } catch (_) {}
        send({ action: "stop_tasks_response", request_id: msg.request_id, success: true, client_id: clientId });
        return;
      }

      if (action === "close_browser") {
        const token = (msg.params && msg.params.token) || "";
        log("收到桌面软件指令：关闭浏览器, token=", token);
        if (token) {
          // 在标题中写入唯一标记，桌面软件据此找到本浏览器窗口并关闭整个浏览器进程
          const marker = `[${token}] `;
          const applyMarker = () => {
            if (!document.title.startsWith(marker)) document.title = marker + document.title.replace(/^\[FLOWCLOSE-[^\]]*\]\s*/, "");
          };
          applyMarker();
          const iv = setInterval(applyMarker, 200);
          setTimeout(() => clearInterval(iv), 15000);
        }
        send({ action: "close_browser_response", request_id: msg.request_id, success: true, client_id: clientId, token });
        
        // 插件优先自己关闭所有窗口（精确且安全）
        setTimeout(() => { sendToExtension("closeBrowser", {}); }, 200);
        return;
      }
    }

    if (window.__lzxFlowTransport) {
      if (!batchPromise) state.isRunning = false;
      window.__lzxFlowTransport.attach(handleMessage, () => ({
        url: location.href, running: !!batchPromise,
        tasks: state.tasks.map(t => ({taskId: t.taskId, download_path: t.download_path,
          status: t.status, mediaBindings: t.mediaBindings, downloadIds: t.downloadIds}))
      }), {prepare: syncPageVideoSettings,
        account: async () => {
          cachedEmail = ""; cachedPoints = null; lastProbeAt = 0;
          const points = await probePoints();
          return {email: cachedEmail || readEmailCheap(), remaining_points: points};
        },
        retry: batchId => {
          if (!/^[a-f0-9]{32}$/.test(batchId || "")) return false;
          const row = state.tasks.findIndex(t => t.download_path === `FlowBridge/${batchId}/result.mp4`);
          return row >= 0 ? retrySavedVideo(state.tasks[row], row, 0, true) : false;
        }});
      return;
    }
    function connect() {
      if (handshakeTimer) {
        clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
      handshakeVerified = false;
      const wsUrl = WS_ENDPOINTS[endpointIdx % WS_ENDPOINTS.length];
      let currentSocket = null;
      try {
        currentSocket = new WebSocket(wsUrl);
        socket = currentSocket;
      } catch (e) {
        endpointIdx = (endpointIdx + 1) % WS_ENDPOINTS.length;
        scheduleReconnect(600);
        return;
      }
      currentSocket.onopen = () => {
        if (socket !== currentSocket) return;
        send({ type: "online", client_id: clientId, email: readEmailCheap(), url: location.href });
        send({ type: "ping", client_id: clientId });
        if (!identity.instance_id) {
          loadIdentity().then(() => {
            if (identity.instance_id && socket === currentSocket && currentSocket.readyState === WebSocket.OPEN) {
              send({ type: "online", client_id: clientId, email: readEmailCheap(), url: location.href });
            }
          });
        }
        // 2.5秒握手验证：若多开浏览器的本地代理劫持了 127.0.0.1（假连接成功但未到达 FlowCue），立即切换至 localhost / [::1] / 备用端口
        handshakeTimer = setTimeout(() => {
          if (socket === currentSocket && !handshakeVerified) {
            log(`⚠️ 端点 ${wsUrl} 握手超时（可能被多开浏览器的代理规则拦截了本地回环地址），自动切换下一通道...`);
            endpointIdx = (endpointIdx + 1) % WS_ENDPOINTS.length;
            try { currentSocket.close(); } catch (_) {}
          }
        }, 2500);
        // 检测插件后台是否为新版本（旧后台没有拖放上传/关闭浏览器等能力）
        sendToExtension("getBgVersion", {}).then(r => {
          const ok = !!(r && r.version);
          send({ type: "bg_status", client_id: clientId, ok, version: (r && r.version) || "",
                 native_drag: !!(r && r.nativeDrag), error: ok ? "" : String((r && r.error) || "插件后台无响应") });
          if (!ok) log("⚠️ 插件后台不是最新版本，请在扩展管理页点击「刷新」重新加载插件");
        });
        // 若首次连接时页面头像 DOM 尚未加载出邮箱，延迟补发一次邮箱识别
        if (!cachedEmail) {
          setTimeout(() => {
            const em = readEmailCheap();
            if (em && socket === currentSocket && currentSocket.readyState === WebSocket.OPEN) {
              send({ type: "online", client_id: clientId, email: em, url: location.href });
            }
          }, 4000);
        }
      };
      currentSocket.onmessage = (ev) => {
        if (socket !== currentSocket) return;
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        handleMessage(msg, wsUrl);
      };
      currentSocket.onclose = () => {
        if (handshakeTimer) {
          clearTimeout(handshakeTimer);
          handshakeTimer = null;
        }
        if (socket === currentSocket) {
          socket = null;
          if (!handshakeVerified) {
            endpointIdx = (endpointIdx + 1) % WS_ENDPOINTS.length;
            scheduleReconnect(600);
          } else {
            scheduleReconnect();
          }
        }
      };
      currentSocket.onerror = () => { /* onclose 会负责重连 */ };
    }

    function scheduleReconnect(customDelay) {
      const waitMs = typeof customDelay === "number" ? customDelay : retryDelay;
      setTimeout(connect, waitMs);
      if (typeof customDelay !== "number") {
        retryDelay = Math.min(retryDelay * 1.5, MAX_RETRY);
      }
    }

    // 页面加载完成后再连接，避免影响页面初始化；连接前先取到本浏览器的唯一 ID
    setTimeout(() => { loadIdentity().finally(connect); }, 1500);
  })();

})();
