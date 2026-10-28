"""Exact bridge seams for pristine FlowCue 1.1.5; never edit vendor originals."""

QUIET = '  const console = window.__lzxFlowTransport ? {log() {}, warn: globalThis.console.warn.bind(globalThis.console), error: globalThis.console.error.bind(globalThis.console)} : globalThis.console;'

RESULT_RECEIPTS = r'''
  const managedReadyReceipts = new Map();
  function reconcileResultReceipts() {
    if (!window.__lzxFlowTransport) return;
    for (const [mediaId, url] of managedReadyReceipts) {
      const matches = state.tasks.flatMap(task =>
        /^FlowBridge\/[a-f0-9]{32}\/result\.mp4$/.test(task.download_path || "") ?
          (task.mediaBindings || []).map((binding, line) => ({task, binding, line})).filter(({binding}) =>
            binding?.schema === 2 && binding.source === "generation_response" && binding.taskId === task.taskId &&
            binding.submissionId && binding.mediaIds?.includes(mediaId)) : []);
      if (matches.length !== 1) continue;
      const {task, binding, line} = matches[0];
      if (binding.readyIds?.includes(mediaId) || binding.failedIds?.includes(mediaId) ||
          binding.downloading || binding.downloadInitiated || task.downloaded?.[line] || task.downloadIds?.[line]) continue;
      managedReadyReceipts.delete(mediaId);
      window.dispatchEvent(new CustomEvent("flow-task-media-ready", {detail: {mediaId, url, source: "receipt_replay"}}));
    }
  }
'''


def exact(text, before, after, count=1):
    if text.count(before) != count:
        raise ValueError('上游接入点已变化，需要重新审查：' + before[:90])
    return text.replace(before, after)


TRANSPORT = '''    if (window.__lzxFlowTransport) {
      if (!batchPromise) state.isRunning = false;
      window.__lzxFlowTransport.attach(handleMessage, () => ({
        url: location.href, running: !!batchPromise,
        tasks: state.tasks.map(t => ({taskId: t.taskId, download_path: t.download_path,
          status: t.status, mediaBindings: t.mediaBindings, downloadIds: t.downloadIds}))
      }), {prepare: syncPageVideoSettings, initialized: () => bridgeUiReady,
        reconcileResults: reconcileResultReceipts,
        upload: uploadPathsToFlow, images: getLoadedPageImages, click: simulateClick, cleanup: cleanupPageOverlays,
        visual: params => sendToExtension("lzxVisualUploadMatch",params),
        skipDownload: batchId => {
          if (!/^[a-f0-9]{32}$/.test(batchId || "")) return false;
          const path = `FlowBridge/${batchId}/result.mp4`;
          if (state.tasks.length !== 1 || state.tasks[0].download_path !== path) return false;
          // Stop only our local result/download wait. Flow's cloud generation
          // and the original task/media bindings are deliberately retained.
          if (batchPromise) stopBatch();
          return true;
        },
        failed: (taskId, submissionId, reason) => {
          const row = state.tasks.findIndex(t => t.taskId === taskId);
          const line = state.tasks[row]?.mediaBindings?.findIndex(b => b?.submissionId === submissionId);
          if (row < 0 || line == null || line < 0) return false;
          updateLineStatus(row, line, "failed", reason);
          stopBatch();
          return true;
        },
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
'''


def patch_ui(ui):
    # Upstream 1.1.6 clears the complete prompt/reference slot before each
    # standalone task. Managed same-image packs own exact-ID attachment and
    # verification; clearing it here would destroy the retained frame proof.
    ui = exact(ui, '  async function clearFlowPromptBox(reason = "切换下一任务前清理") {',
        '  async function clearFlowPromptBox(reason = "切换下一任务前清理") {\n'
        '    if (window.__lzxFlowTransport) return;')
    ui = exact(ui, '  "use strict";', '  "use strict";\n' + QUIET)
    ui = exact(ui, '      const saved = parseInt(localStorage.getItem("flowcue_ws_ep") || "0", 10);',
        '      const saved = window.__lzxFlowTransport ? 0 : parseInt(localStorage.getItem("flowcue_ws_ep") || "0", 10);')
    ui = exact(ui, '  if (window.top !== window) return;',
        '  if (window.top !== window || window.__lzxFlowUI || window.__flowAccountBridgeLoaded) return;\n'
        '  window.__lzxFlowUI = true;')
    ui = exact(ui, '  let state = {', '  let bridgeUiReady = false;\n' + RESULT_RECEIPTS + '\n  let state = {')
    ui = exact(ui, '    if (state.isRunning && !batchPromise) {',
        '    if (!window.__lzxFlowTransport && state.isRunning && !batchPromise) {')
    ui = exact(ui, '    await loadState();\n    \n    // 如果网页刚才由于刷新中断',
        '    await loadState();\n'
        '    if (window.__lzxFlowTransport && !batchPromise) state.isRunning = false;\n'
        '    bridgeUiReady = true;\n    \n    // 如果网页刚才由于刷新中断')
    ui = exact(ui, '    if (state.isRunning) {\n      console.log("[FlowUI] 检测到批量运行状态为 TRUE',
        '    if (!window.__lzxFlowTransport && state.isRunning) {\n      console.log("[FlowUI] 检测到批量运行状态为 TRUE')
    ui = exact(ui,
        '      onBatchFinished();\n      document.querySelectorAll("#flow-tasks-table input, #flow-tasks-table textarea, #flow-tasks-table select, #flow-tasks-table button").forEach(el => el.disabled = false);\n      batchPromise = null;',
        '      try {\n        onBatchFinished();\n'
        '        document.querySelectorAll("#flow-tasks-table input, #flow-tasks-table textarea, #flow-tasks-table select, #flow-tasks-table button").forEach(el => el.disabled = false);\n'
        '      } finally {\n        batchPromise = null;\n        window.__lzxFlowTransport?.batchIdle?.();\n      }')
    ui = exact(ui,
        '        // 任务下载完成后的批量间隔延时',
        '        // The desktop owns the next batch. Its final job has no next-item interval.\n'
        '        if (window.__lzxFlowTransport && isTaskCompleted && rIdx === endRow - 1 && lIdx === lines.length - 1) return;\n\n'
        '        // 任务下载完成后的批量间隔延时')
    ui = exact(ui, '        reply(true, "", { count });', '')
    ui = exact(ui, '        try { await startBatch(); } catch (e) { log("托管批次异常", e); }',
        '        try { const executing = startBatch(); reply(true, "", { count }); await executing; } catch (e) { log("托管批次异常", e); }')
    for condition in (
        'if (!match && state.isRunning) {',
        'if (!bound && state.isRunning && renderedId && inTile && renderedUrl) {',
        'if (!match && state.isRunning && cleanUrl && !wasVideoSeen(cleanUrl) && !isStaticShowcaseAsset(cleanUrl)) {',
    ):
        ui = exact(ui, condition, condition.replace('if (', 'if (!window.__lzxFlowTransport && ', 1))
    ui = exact(ui,
        '                } else if (vidId && (!binding.readyIds || binding.readyIds.length === 0) && !wasVideoSeen(src, v) && isFlowProjectVideo && inVideoTile) {',
        '                } else if (!window.__lzxFlowTransport && vidId && (!binding.readyIds || binding.readyIds.length === 0) && !wasVideoSeen(src, v) && isFlowProjectVideo && inVideoTile) {')
    ui = exact(ui, '          const boundMediaIds = binding?.mediaIds || [];',
        '          const boundMediaIds = window.__lzxFlowTransport && binding?.source !== "generation_response" ? [] : (binding?.mediaIds || []);')
    ui = exact(ui, '            if (candidateUrl && isValidFlowVideoUrl(candidateUrl)) {',
        '            if (candidateUrl && isValidFlowVideoUrl(candidateUrl) && (!window.__lzxFlowTransport || binding.readyIds?.includes(mId))) {')
    ui = exact(ui, '                if (cachedVideoUrl && isValidFlowVideoUrl(cachedVideoUrl)) {',
        '                if (cachedVideoUrl && isValidFlowVideoUrl(cachedVideoUrl) && (!window.__lzxFlowTransport || binding.readyIds?.includes(thumbId))) {')
    ui = exact(ui, '                if (vidId && boundMediaIds.length > 0 && boundMediaIds.includes(vidId)) {',
        '                if (vidId && boundMediaIds.length > 0 && boundMediaIds.includes(vidId) &&\n'
        '                    (!window.__lzxFlowTransport || !v.error && (v.readyState >= 1 || v.currentTime > 0))) {')
    ui = exact(ui, '      if (bound && (videoEl.readyState >= 1 || videoEl.currentTime > 0 || isFlowProjectVideoUrl) && !videoEl.error && inTile) {',
        '      if (bound && (videoEl.readyState >= 1 || videoEl.currentTime > 0 || !window.__lzxFlowTransport && isFlowProjectVideoUrl) && !videoEl.error && inTile) {')
    ui = exact(ui, '      if (!match) {\n        console.log(`[FlowUI] [media-ready] 已缓存视频直链',
        '      if (window.__lzxFlowTransport && event.detail?.source === "url_discovery") return;\n'
        '      if (!match) {\n        console.log(`[FlowUI] [media-ready] 已缓存视频直链')
    ui = exact(ui, '                                    (boundMediaIds.length === 0 && !wasVideoSeen(`https://flow-content.google/video/${thumbId}`));',
        '                                    (!window.__lzxFlowTransport && boundMediaIds.length === 0 && !wasVideoSeen(`https://flow-content.google/video/${thumbId}`));')
    ui = exact(ui, '      const { submissionId, mediaIds, source } = event.detail || {};',
        '      const { submissionId, mediaIds, source } = event.detail || {};\n'
        '      if (window.__lzxFlowTransport && source !== "generation_response") return;')
    ui = exact(ui, '      const process = window.currentProcess;\n      console.log("[FlowUI] 📥 收到 flow-task-media-bound 事件:",',
        '      const process = window.currentProcess?.submissionId === submissionId ? window.currentProcess :\n'
        '        window.__lzxFlowTransport?.findSubmission?.(submissionId);\n'
        '      console.log("[FlowUI] 📥 收到 flow-task-media-bound 事件:",')
    ui = exact(ui, '      if (!state.isRunning) {\n        console.warn("[FlowUI] [media-bound] 忽略: state.isRunning 为 false");',
        '      if (!state.isRunning && !window.__lzxFlowTransport) {\n        console.warn("[FlowUI] [media-bound] 忽略: state.isRunning 为 false");')
    binding_line = '      binding.mediaIds = [...new Set(mediaIds.filter(id => typeof id === "string" && /^[a-zA-Z0-9_-]+$/.test(id)))];'
    ui = exact(ui, binding_line,
        '      if (window.__lzxFlowTransport) {\n'
        '        if (binding.source === "generation_response" && binding.mediaIds?.length &&\n'
        '            (binding.mediaIds.length !== mediaIds.length || binding.mediaIds.some(id => !mediaIds.includes(id)))) return;\n'
        '        binding.source = "generation_response";\n'
        '        binding.readyIds = (binding.readyIds || []).filter(id => mediaIds.includes(id));\n'
        '        binding.urls = Object.fromEntries(Object.entries(binding.urls || {}).filter(([id]) => mediaIds.includes(id)));\n'
        '      }\n' + binding_line + '\n      reconcileResultReceipts();')
    ui = exact(ui, '      if (mediaId) discoveredVideoUrlsById.set(mediaId, cleanUrl);',
        '      if (window.__lzxFlowTransport && mediaId && parsedId === mediaId &&\n'
        '          !["url_discovery", "receipt_replay"].includes(event.detail?.source)) {\n'
        '        managedReadyReceipts.set(mediaId, cleanUrl);\n'
        '        while (managedReadyReceipts.size > 64) managedReadyReceipts.delete(managedReadyReceipts.keys().next().value);\n'
        '      }\n'
        '      if (mediaId) discoveredVideoUrlsById.set(mediaId, cleanUrl);')
    missing = '''      if (!state.isRunning || !process || process.submissionId !== event.detail?.submissionId) {
        return;
      }
    });
    window.addEventListener("flow-task-media-ready",'''
    ui = exact(ui, missing, missing.replace('      }\n    });',
        '      }\n      window.__lzxFlowTransport?.bindingMissing?.(process);\n    });', 1))
    ui = exact(ui, '    function send(obj) {\n      try {',
        '    function send(obj) {\n      if (window.__lzxFlowTransport) { window.__lzxFlowTransport.send(obj); return; }\n      try {')
    ui = exact(ui, '      if (action === "get_points") {\n        const pts = await probePoints();',
        '      if (action === "get_points") {\n        cachedEmail = ""; cachedPoints = null; lastProbeAt = 0;\n        const pts = await probePoints();')
    ui = exact(ui, '      if (pts !== null) { readEmailCheap(); return pts; }',
        '      if (pts !== null) { readEmailCheap();\n'
        '        window.__lzxFlowTransport?.creditRefresh?.(document.querySelector("flow-account-panel, .flow-account-panel-overlay, .cdk-overlay-pane"), cachedEmail || readEmailCheap());\n'
        '        return pts; }')
    ui = exact(ui, '      // 关闭菜单\n      await sleep(100);',
        '      window.__lzxFlowTransport?.creditRefresh?.(panel, cachedEmail || readEmailCheap());\n'
        '      // 关闭菜单\n      await sleep(100);')
    ui = exact(ui, '        } finally {\n          probing = null;\n        }',
        '        } finally {\n'
        '          try { await window.__lzxFlowTransport?.closeAccountPanel?.(); }\n'
        '          finally { probing = null; }\n'
        '        }')
    ui = exact(ui, '            if (!t) continue;\n            if (t.includes("点数")',
        '            if (!t) continue;\n'
        '            if (window.__lzxFlowTransport?.isCreditRefreshText?.(t)) continue;\n'
        '            if (t.includes("点数")')
    ui = exact(ui, '                n = parseCreditNumber(el.parentElement.textContent);',
        '                if (window.__lzxFlowTransport?.isCreditRefreshText?.(el.parentElement.textContent)) continue;\n'
        '                n = parseCreditNumber(el.parentElement.textContent);')
    ui = exact(ui, '    function connect() {\n', TRANSPORT + '    function connect() {\n')
    ui = exact(ui, '  async function retrySavedVideo(task, rowIndex, lineIndex) {',
        '  async function retrySavedVideo(task, rowIndex, lineIndex, downloadOnly = false) {')
    ui = exact(ui, '    if (binding?.schema !== 2 || !binding.mediaIds?.length) {',
        '    if (binding?.schema !== 2 || !binding.mediaIds?.length) {\n'
        '      if (window.__lzxFlowTransport && binding?.schema === 2) {\n'
        '        updateLineStatus(rowIndex, lineIndex, "generating", "保留原提交等待媒体编号，不清除绑定、不重新生成。");\n'
        '        return !downloadOnly;\n'
        '      }')
    ui = exact(ui, '      updateLineStatus(rowIndex, lineIndex, "generating", "已取得媒体 ID，等待获取完整签名视频直链...");\n      return true;',
        '      updateLineStatus(rowIndex, lineIndex, "generating", "已取得媒体 ID，等待获取完整签名视频直链...");\n      return !downloadOnly;')
    ui = exact(ui, '    return triggerAutoDownload(url, mediaId);',
        '    return triggerAutoDownload(url, mediaId, downloadOnly ? binding : null);')
    ui = exact(ui, '  async function triggerAutoDownload(videoUrl, mediaName) {\n    if (!state.isRunning) {',
        '  async function triggerAutoDownload(videoUrl, mediaName, retryBinding = null) {\n    if (!state.isRunning && !retryBinding) {')
    ui = exact(ui, '    let match = findMediaBinding(mediaId);\n    if (!window.__lzxFlowTransport && !match && state.isRunning) {',
        '    let match = findMediaBinding(mediaId);\n'
        '    if (retryBinding && (match?.binding !== retryBinding || retryBinding.schema !== 2)) return false;\n'
        '    if (!window.__lzxFlowTransport && !match && state.isRunning) {')
    ui = exact(ui, '    const { task, binding, rowIndex, lineIndex } = match;',
        '    const { task, binding, rowIndex, lineIndex } = match;\n'
        '    if (window.__lzxFlowTransport && (binding.source !== "generation_response" || binding.failedIds?.includes(mediaId))) return false;')
    ui = exact(ui, '      await triggerAutoDownload(cleanUrl, mediaId);',
        '      await triggerAutoDownload(cleanUrl, mediaId, !state.isRunning && window.__lzxFlowTransport ? match.binding : null);')
    ui = exact(ui, '      if (!videoEl || !state.isRunning) return;',
        '      if (!videoEl || (!state.isRunning && !window.__lzxFlowTransport?.hasPendingResults?.())) return;')
    ui = exact(ui, '        triggerAutoDownload(renderedUrl, renderedId);',
        '        triggerAutoDownload(renderedUrl, renderedId, !state.isRunning && window.__lzxFlowTransport ? bound.binding : null);')
    ui = exact(ui, '        if (!bound.binding.readyIds.includes(renderedId)) bound.binding.readyIds.push(renderedId);\n        bound.binding.urls = bound.binding.urls || {};',
        '        if (!bound.binding.readyIds.includes(renderedId)) bound.binding.readyIds.push(renderedId);\n'
        '        if (window.__lzxFlowTransport && bound.task === state.tasks[state.currentIndex]) {\n'
        '          window.__lzxFlowTransport.locateResult?.(videoEl.closest("flow-video-tile") || videoEl.closest("flow-grid-tile-container"), renderedId, bound.task.taskId);\n'
        '        }\n        bound.binding.urls = bound.binding.urls || {};')
    ui = exact(ui, '        if (event.target?.tagName === "VIDEO" && state.isRunning) {',
        '        if (event.target?.tagName === "VIDEO" && (state.isRunning || window.__lzxFlowTransport?.hasPendingResults?.())) {')
    start, end = ui.index('  function setupDomVideoWatcher()'), ui.index('  function setupEvents()')
    watcher = exact(ui[start:end], '      if (!state.isRunning) return;',
        '      if (!state.isRunning && !window.__lzxFlowTransport?.hasPendingResults?.()) return;')
    watcher = exact(watcher, '      if (!state.isRunning || batchPromise) return;',
        '      if ((!state.isRunning && !window.__lzxFlowTransport?.hasPendingResults?.()) || batchPromise) return;')
    ui = ui[:start] + watcher + ui[end:]
    ui = exact(ui, '    function setToggle(ariaLabels, matcher) {\n',
        '    function setToggle(ariaLabels, matcher) {\n'
        '      if (window.__lzxFlowTransport) {\n'
        '        overlay = window.__lzxFlowTransport.settingsPanel();\n'
        '        if (!overlay) throw Object.assign(new Error("设置面板正在重建，自动等待后重新核对；未提交生成。"), {code: "FLOW_SETTINGS_NOT_READY"});\n'
        '      }\n')
    ui = exact(ui, '    // 4. 关闭设置面板\n',
        '    if (window.__lzxFlowTransport) {\n'
        '      await window.__lzxFlowTransport.waitForSettings(settings);\n'
        '    }\n\n    // 4. 关闭设置面板\n')
    old_loop = '!(task.mediaBindings?.[lIdx]?.schema === 2 && task.mediaBindings?.[lIdx]?.mediaIds?.length > 0) && subTry < 2'
    ui = exact(ui, old_loop,
        '(window.__lzxFlowTransport ? task.mediaBindings?.[lIdx]?.schema !== 2 : !(task.mediaBindings?.[lIdx]?.schema === 2 && task.mediaBindings?.[lIdx]?.mediaIds?.length > 0)) && subTry < (window.__lzxFlowTransport ? 1 : 2)')
    ui = exact(ui, '            if (subTry === 0) {', '            if (subTry === 0 && !window.__lzxFlowTransport) {')
    ui = exact(ui, '    if (!params.generateRequestSent && boundIds.length === 0 && !isFlowCurrentlyGenerating()) {',
        '    if (!window.__lzxFlowTransport && !params.generateRequestSent && boundIds.length === 0 && !isFlowCurrentlyGenerating()) {')
    ui = exact(ui, '  function resolveDownloadPath(downloadPath, processInfo = {}) {',
        '  function resolveDownloadPath(downloadPath, processInfo = {}) {\n'
        '    if (window.__lzxFlowTransport && /^FlowBridge\\/[a-f0-9]{32}\\/result\\.mp4$/.test(downloadPath)) return downloadPath;')
    ui = exact(ui, '      item.duration = normalizeDuration(item.duration ?? item.duration_seconds ?? item.durationSeconds, 6);',
        '      item.duration = normalizeDuration(item.duration ?? item.duration_seconds ?? item.durationSeconds, window.__lzxFlowTransport ? null : 6);')
    ui = exact(ui, 'const duration = normalizeDuration(task.duration, calculateDuration(lineText));',
        'const duration = normalizeDuration(task.duration, window.__lzxFlowTransport ? null : calculateDuration(lineText));', 2)
    ui = exact(ui, '  async function applyDurationSetting(value) {',
        '  async function applyDurationSetting(value) {\n'
        '    if (window.__lzxFlowTransport) return window.__lzxFlowTransport.applyDuration(value);')
    ui = exact(ui, '      if (panelOpen) {\n        console.warn(`[FlowUI] 当前模型设置面板中无时长选项',
        '      if (panelOpen) {\n        if (window.__lzxFlowTransport) throw durationError("当前模型没有可确认的时长选项，请使用模型默认时长");\n        console.warn(`[FlowUI] 当前模型设置面板中无时长选项')
    ui = exact(ui, '    if (!option) {\n      const availableOpts = options().map',
        '    if (!option) {\n      if (window.__lzxFlowTransport) throw durationError(`当前模型不支持 ${target} 秒，请选择模型默认或支持的时长`);\n      const availableOpts = options().map')
    ui = exact(ui, '        console.warn("[FlowUI] ⚠️ 生成按钮超时未就绪，强制解除 disabled 限制触发点击");',
        '        if (window.__lzxFlowTransport) throw durationError("生成按钮未就绪，请检查网站提示后重试");\n'
        '        console.warn("[FlowUI] ⚠️ 生成按钮超时未就绪，强制解除 disabled 限制触发点击");')
    ui = exact(ui, '  function checkForUnusualActivity() {',
        '  function checkForUnusualActivity() {\n'
        '    if (window.__lzxFlowTransport) return window.__lzxFlowTransport.unusualActivity?.() === true;')
    ui = exact(ui, '    alarmCountdown = 45;',
        '    if (window.__lzxFlowTransport) {\n'
        '      if (alarmTimer) clearInterval(alarmTimer);\n'
        '      alarmTimer = null;\n'
        '      const notice = document.getElementById("flow-alarm-message");\n'
        '      if (notice) notice.innerText = "网站提示安全限制，队列已暂停；保留当前页面，不自动刷新或重复生成。";\n'
        '      window.__lzxFlowTransport.sitePaused?.();\n'
        '      return;\n'
        '    }\n    alarmCountdown = 45;')
    ui = exact(ui, '    const isPresent = (p, imgs) => imgs.some(img => isStrictImageMatch(nameOf(p), img.displayName));',
        '    const isPresent = (p, imgs) => imgs.some(img => window.__lzxFlowTransport ?\n'
        '      !!(img.primaryMediaKey || img.id) && !String(img.primaryMediaKey || img.id).startsWith("img_") &&\n'
        '      [nameOf(p), nameOf(p).replace(/\\.[^.]+$/, "")].map(n => n.toLowerCase()).includes(String(img.displayName || "").toLowerCase()) : isStrictImageMatch(nameOf(p), img.displayName));')
    ui = exact(ui, '    console.log(`[FlowUI] 读取到页面素材图片共 ${images.length} 张:`, images);\n    return images;',
        '    console.log(`[FlowUI] 读取到页面素材图片共 ${images.length} 张:`, images);\n'
        '    return window.__lzxFlowTransport && window.__lzxReferenceIdentity?.images ? window.__lzxReferenceIdentity.images(images) : images;')
    ui = exact(ui, '  async function uploadPathsToFlow(paths, { timeoutMs = 60000 } = {}) {',
        '  async function uploadPathsToFlow(paths, { timeoutMs = 60000, maxWaitMs = timeoutMs, visualFallback = null, operationGuard = null, forceUpload = false } = {}) {')
    ui = exact(ui, '    for (let i = 0; uploadInFlight && i < 60; i++) await delay(500);',
        '    for (let i = 0; uploadInFlight && i < 60; i++) {operationGuard?.(); await delay(500);}\n    operationGuard?.();')
    ui = exact(ui, '    const nameOf = p => getFilenameFromPath(p);\n    const isPresent =',
        '    const nameOf = p => getFilenameFromPath(p);\n'
        '    const oldIds = new Set(forceUpload ? getLoadedPageImages().map(img=>img.primaryMediaKey || img.id) : []);\n'
        '    const isPresent =')
    ui = exact(ui, '!!(img.primaryMediaKey || img.id) && !String(img.primaryMediaKey || img.id).startsWith("img_") &&',
        '!!(img.primaryMediaKey || img.id) && !oldIds.has(img.primaryMediaKey || img.id) && !String(img.primaryMediaKey || img.id).startsWith("img_") &&')
    ui = exact(ui, '      for (let i = 0; i < todo.length; i += UPLOAD_BATCH_SIZE) {',
        '      for (let i = 0; i < todo.length; i += UPLOAD_BATCH_SIZE) {\n        operationGuard?.();')
    ui = exact(ui, '        await delay(2000);\n        imgs = getLoadedPageImages();',
        '        operationGuard?.();\n        await delay(2000);\n        operationGuard?.();\n        imgs = getLoadedPageImages();')
    ui = exact(ui, '      const beforeKeys = snapshotImageKeys(imgs);\n      const dispatched = [];',
        '      const beforeKeys = snapshotImageKeys(imgs);\n'
        '      let priorUploadError = window.__lzxFlowTransport?.uploadActivity?.(todo)?.error || "";\n'
        '      const dispatched = [];')
    ui = exact(ui, '      const start = Date.now();\n      let waiting = dispatched.slice();\n      let countConfirmed = false;\n      while (waiting.length && Date.now() - start < timeoutMs) {',
        '      const start = Date.now(), managed = !!window.__lzxFlowTransport;\n'
        '      const totalWait = managed ? Math.max(timeoutMs, maxWaitMs) : timeoutMs;\n'
        '      let progressAt = start, progressStamp = "", uploadError = "", visualCount = 0, visualAt = start;\n'
        '      let waiting = dispatched.slice();\n'
        '      let countConfirmed = false;\n'
        '      while (waiting.length && Date.now() - start < totalWait) {')
    ui = exact(ui, '        waiting = waiting.filter(p => !isPresent(p, imgs));\n        if (waiting.length && newTilesConfirmed(beforeKeys, beforeCount, imgs, dispatched.length)) {',
        '        waiting = waiting.filter(p => !isPresent(p, imgs));\n'
        '        if (!waiting.length) break;\n'
        '        if (managed && clean.length === 1 && visualFallback && visualCount < 3 && Date.now() - visualAt >= 15000) {\n'
        '          visualAt = Date.now();\n'
        '          try {\n'
        '            const image = await visualFallback();\n'
        '            if (!image?.pending) visualCount++;\n'
        '            if (image?.primaryMediaKey) {result.visual_matches = [{path:waiting[0],image}]; waiting = []; break;}\n'
        '          } catch (_) {visualCount++;}\n'
        '        }\n'
        '        if (managed) {\n'
        '          const activity = window.__lzxFlowTransport.uploadActivity?.(waiting) || {};\n'
        '          if (activity.error && activity.error !== priorUploadError) {uploadError = activity.error; break;}\n'
        '          if (!activity.error) priorUploadError = "";\n'
        '          const partial = imgs.filter(img => waiting.some(p =>\n'
        '            [nameOf(p), nameOf(p).replace(/\\.[^.]+$/, "")].map(n => n.toLowerCase()).includes(String(img.displayName || "").toLowerCase())))\n'
        '            .map(img => [img.displayName,img.primaryMediaKey || img.id].join(":"));\n'
        '          const stamp = JSON.stringify([waiting,activity.stamp || "",partial]);\n'
        '          if (stamp !== progressStamp) {progressStamp = stamp; progressAt = Date.now();}\n'
        '          const pendingOwnImage = partial.some(item => /:img_/.test(item));\n'
        '          if (!activity.active && !pendingOwnImage && Date.now() - progressAt >= timeoutMs) break;\n'
        '        }\n'
        '        if (waiting.length && newTilesConfirmed(beforeKeys, beforeCount, imgs, dispatched.length)) {')
    ui = exact(ui, 'result.errors[p] = "Flow 页面中没有出现这张图片（超时）";',
        'result.errors[p] = uploadError || (managed ? (Date.now() - start >= totalWait ? "图片上传达到最长等待时间，尚未确认本次图片的素材编号" : "图片上传持续无进展，尚未确认本次图片的素材编号") : "Flow 页面中没有出现这张图片（超时）");')
    ui = exact(ui, '        if (isRegisteredInFlow(p, imgs.length, regEntries)) {',
        '        if (!window.__lzxFlowTransport && isRegisteredInFlow(p, imgs.length, regEntries)) {')
    ui = exact(ui, '        if (waiting.length && newTilesConfirmed(beforeKeys, beforeCount, imgs, dispatched.length)) {',
        '        if (!window.__lzxFlowTransport && waiting.length && newTilesConfirmed(beforeKeys, beforeCount, imgs, dispatched.length)) {')
    ui = exact(ui, '      const finalDisplayName = imageName || localFileName || (matchedImg ? matchedImg.displayName : "未命名素材");',
        '      if (window.__lzxFlowTransport) matchedImg = window.__lzxReferenceIdentity.resolve(item);\n'
        '      const finalDisplayName = imageName || localFileName || (matchedImg ? matchedImg.displayName : "未命名素材");')
    ui = exact(ui, '  async function attachImageToPage(image, footageType) {',
        '  async function attachImageToPage(image, footageType) {\n'
        '    if (window.__lzxFlowTransport) return window.__lzxReferenceIdentity.attach(image, footageType);')
    ui = exact(ui, '  function verifyImageAttached(image, footageType) {',
        '  function verifyImageAttached(image, footageType) {\n'
        '    if (window.__lzxFlowTransport) return window.__lzxReferenceIdentity.verify(image, footageType);')
    ui = exact(ui, '        console.warn("[FlowUI] 注入图片原料异常 (继续执行):", imgErr);',
        '        if (window.__lzxFlowTransport) throw imgErr;\n'
        '        console.warn("[FlowUI] 注入图片原料异常 (继续执行):", imgErr);')
    ui = exact(ui, '        console.warn(`[FlowUI] ⚠️ 挂载检测未直接捕获到芯片，继续执行发送...`);',
        '        if (window.__lzxFlowTransport) throw new Error("本条参考图首帧未通过校验，未提交生成。");\n'
        '        console.warn(`[FlowUI] ⚠️ 挂载检测未直接捕获到芯片，继续执行发送...`);')
    ui = exact(ui, '    button.click();\n    await delay(1800);',
        '    if (window.__lzxFlowTransport) window.__lzxReferenceIdentity.assert(image, targetFootage);\n'
        '    window.__lzxFlowTransport?.assertUi?.();\n'
        '    button.click();\n'
        '    if (window.__lzxFlowTransport?.uiMutexActive?.()) await window.__lzxFlowTransport.afterSubmit(params);\n'
        '    else await delay(1800);')
    ui = exact(ui, '  async function submitTaskToPage(params) {',
        '  async function submitTaskToPage(params) {\n'
        '    if (window.__lzxFlowTransport?.uiOperation) return window.__lzxFlowTransport.uiOperation("submit", () => submitTaskToPageUi(params), String(params.download_path || "").match(/^FlowBridge\\/([a-f0-9]{32})\\/result\\.mp4$/)?.[1]);\n'
        '    return submitTaskToPageUi(params);\n'
        '  }\n'
        '  async function submitTaskToPageUi(params) {')
    ui = exact(ui, '  function simulateClick(el) {\n    if (!el) return;',
        '  function simulateClick(el) {\n    if (!el) return;\n    window.__lzxFlowTransport?.assertUi?.();')
    ui = exact(ui, '    // 5. 注入文本到 ProseMirror 编辑器 (支持多行段落，先确保残留文本被清空，再注入新文本)',
        '    window.__lzxFlowTransport?.assertUi?.();\n    // 5. 注入文本到 ProseMirror 编辑器 (支持多行段落，先确保残留文本被清空，再注入新文本)')
    ui = exact(ui, '    window.promptBoxStore?.getState?.().actions?.setPrompt?.(finalPrompt);\n    await delay(300);',
        '    window.promptBoxStore?.getState?.().actions?.setPrompt?.(finalPrompt);\n'
        '    if (window.__lzxFlowTransport?.uiMutexActive?.()) await window.__lzxFlowTransport.waitPrompt(editor, finalPrompt);\n'
        '    else await delay(300);')
    ui = exact(ui, 'if (!task.image?.src) {', 'if (!window.__lzxFlowTransport && !task.image?.src) {', 2)
    return ui


def patch_proxy(proxy):
    proxy = exact(proxy, '  "use strict";', '  "use strict";\n' + QUIET)
    proxy = exact(proxy, '  if (window.top !== window) return;',
        '  if (window.top !== window || window.__lzxFlowProxy || window.__flowAccountBridgeLoaded) return;\n'
        '  window.__lzxFlowProxy = true;')
    proxy = exact(proxy, '  const origHasOwnProperty = Object.prototype.hasOwnProperty;',
        '  if (!window.__lzxFlowTransport) {\n  const origHasOwnProperty = Object.prototype.hasOwnProperty;')
    proxy = exact(proxy, '    return origHasOwnProperty.call(this, prop);\n  };',
        '    return origHasOwnProperty.call(this, prop);\n  };\n  }')
    proxy = exact(proxy, '  function setupWebpackHook() {',
        '  function setupWebpackHook() {\n    if (window.__lzxFlowTransport) return;')
    proxy = exact(proxy, '    const activeProcess = process || window.currentProcess;',
        '    const activeProcess = window.__lzxFlowTransport ? process : (process || window.currentProcess);')
    ownership = '(window.__lzxFlowTransport?.matchesSubmission?.(activeProcess) ?? (window.currentProcess === activeProcess))'
    proxy = exact(proxy, 'activeProcess?.submissionId && window.currentProcess === activeProcess',
        'activeProcess?.submissionId && ' + ownership, 3)
    proxy = exact(proxy, '    const frames = readRpcFrames(data);',
        '    if (isGenerateRequest && activeProcess?.submissionId && ' + ownership + ') window.__lzxFlowTransport?.recordGenerationResponse?.(activeProcess, data);\n'
        '    const frames = readRpcFrames(data);')
    proxy = exact(proxy, '    if (submission && activeProcess?.submissionId && ' + ownership + ') {',
        '    if (submission && isGenerateRequest && activeProcess?.submissionId && ' + ownership + ') {')
    proxy = exact(proxy, '    if (!boundInThisResponse && activeProcess?.submissionId && ' + ownership + ' && !activeProcess.mediaBound) {',
        '    if (!window.__lzxFlowTransport && !boundInThisResponse && activeProcess?.submissionId && ' + ownership + ' && !activeProcess.mediaBound) {')
    proxy = exact(proxy, '      if (!ids.length && Array.isArray(media) && Array.isArray(groups)) {',
        '      if (!window.__lzxFlowTransport && !ids.length && Array.isArray(media) && Array.isArray(groups)) {')
    proxy = exact(proxy, '      if (!ids.length && Array.isArray(media) && media.length > 0) {',
        '      if (!window.__lzxFlowTransport && !ids.length && Array.isArray(media) && media.length > 0) {')
    proxy = exact(proxy, 'source: "submission"', 'source: "generation_response"', 2)
    proxy = exact(proxy, 'window.dispatchEvent(new CustomEvent("flow-task-media-ready", { detail: { mediaId: mId, url } }));',
        'window.dispatchEvent(new CustomEvent("flow-task-media-ready", { detail: { mediaId: mId, url, source:"url_discovery" } }));')
    proxy = exact(proxy, '      if (!mediaId || status !== "MEDIA_GENERATION_STATUS_SUCCESSFUL") continue;',
        '      if (window.__lzxFlowTransport && mediaId && status === "MEDIA_GENERATION_STATUS_FAILED") {\n'
        '        window.dispatchEvent(new CustomEvent("flow-task-media-failed", {detail:{mediaId, source:"server_status",\n'
        '          reason:String(media.mediaMetadata?.mediaStatus?.error?.message || "Flow 已明确返回视频生成失败").slice(0,500)}}));\n'
        '      }\n'
        '      if (!mediaId || status !== "MEDIA_GENERATION_STATUS_SUCCESSFUL") continue;')
    # A single batchexecute response can contain old project history BEFORE the
    # creation frame. Similar array shapes never prove a frame created media.
    proxy = exact(proxy,
        '    const submission = frames.find(frame => frame.rpc === "eb1hJf" || (Array.isArray(frame.payload?.[2]) && Array.isArray(frame.payload?.[3])));',
        '    const submission = frames.find(frame => window.__lzxFlowTransport ? ["eb1hJf", "L2jnw"].includes(frame.rpc) :\n'
        '      frame.rpc === "eb1hJf" || (Array.isArray(frame.payload?.[2]) && Array.isArray(frame.payload?.[3])));')
    proxy = exact(proxy, '      console.log(`[FlowProxy] [${submission.rpc}] 提取到的 mediaIds:`, ids);',
        '      if (window.__lzxFlowTransport && new Set(ids).size !== 1) ids = [];\n'
        '      console.log(`[FlowProxy] [${submission.rpc}] 提取到的 mediaIds:`, ids);')
    proxy = exact(proxy, '      const ids = lists.flat().map(media => mediaIdOf(media?.name)).filter(Boolean);',
        '      const createdLists = window.__lzxFlowTransport && frames.length ?\n'
        '        frames.filter(frame => ["eb1hJf", "L2jnw"].includes(frame.rpc)).flatMap(frame => responseMediaLists(frame.payload)) : lists;\n'
        '      const candidates = [...new Set(createdLists.flat().map(media => mediaIdOf(media?.name)).filter(Boolean))];\n'
        '      const ids = window.__lzxFlowTransport && candidates.length !== 1 ? [] : candidates;')
    proxy = exact(proxy, '  function classifyRequest(url, body) {', '''  function classifyRequest(url, body) {
    if (window.__lzxFlowTransport) {
      let parsed;
      try { parsed = new URL(url, "https://flow.google.com"); } catch (_) { return {generate:false,status:false}; }
      const rpcIds = new Set((parsed.searchParams.get("rpcids") || "").split(",").filter(Boolean));
      if (!rpcIds.size) {
        try {
          const collect = value => {
            if (!Array.isArray(value)) return;
            if (typeof value[0] === "string") { rpcIds.add(value[0]); return; }
            value.forEach(collect);
          };
          collect(JSON.parse(new URLSearchParams(body || "").get("f.req") || "null"));
        } catch (_) {}
      }
      return {
        generate: /(?:video:batchAsyncGenerateVideo[A-Za-z]*|StreamGenerateContent)(?:$|\\/)/.test(parsed.pathname) ||
          [...rpcIds].some(id => ["eb1hJf", "L2jnw"].includes(id)),
        status: /batchCheckAsyncVideoGenerationStatus|BatchGetMedia|ListMedia|batchexecute/.test(parsed.pathname) ||
          [...rpcIds].some(id => ["Iyc41d", "bOKtO", "jwpduf", "as29s", "nzlxg"].includes(id))
      };
    }''')
    message = '        console.log(`[FlowProxy] ℹ️ 生成请求响应未直接返回 mediaLists ID (submissionId=${activeProcess.submissionId})，继续等待后续轮询帧或 DOM 视频渲染认领`);'
    proxy = exact(proxy, message, message + '\n        if (window.__lzxFlowTransport) window.dispatchEvent(new CustomEvent("flow-task-binding-missing", {detail: {submissionId: activeProcess.submissionId}}));')
    proxy = exact(proxy,
        '      if (contentType.includes("application/octet-stream") || contentType.includes("blob") || contentType.includes("video/") || contentType.includes("image/") || contentType.includes("audio/")) {',
        '      if (contentType.includes("video/") || contentType.includes("image/") || contentType.includes("audio/")) {')
    # Equal-length frames are not necessarily duplicate responses.
    proxy = exact(proxy, '          const key = `${value[1]}:${typeof value[2] === "string" ? value[2].length : ""}`;',
        '          const key = JSON.stringify([value[1], value[2]]);')
    return proxy


def patch_background(background):
    focus = '      try { await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true }); } catch (_) {}'
    background = exact(background, focus,
        '      try {\n'
        '        await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true });\n'
        '        attachedHere = true; // Successful command proves this extension owns the session.\n'
        '      } catch (_) {}', 3)
    background = exact(background, '    const isManual = !!(task && task.manual);',
        '    const isManual = !!(task && task.manual);\n'
        '    if (isManual && /^FlowBridge\\//.test(filename || "")) throw new Error("托管任务只允许下载已验证的原媒体，不允许跳过归属校验。");')
    background = exact(background, '    const parsed = new URL(url);',
        '    const parsed = new URL(url);\n'
        '    if (parsed.protocol !== "https:" || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) {\n'
        '      throw new Error("下载地址必须是无账号信息的标准 HTTPS 地址");\n'
        '    }\n'
        '    const flowAsb = parsed.hostname === "flow.google.com" ? parsed.pathname.match(/^\\/asb\\/([a-zA-Z0-9_-]+)(?:=[a-zA-Z0-9_,.-]+)?$/) : null;')
    background = exact(background, '      parsed.hostname === "flow.google.com" ||\n      parsed.hostname.endsWith(".flow.google.com") ||',
        '      !!flowAsb ||')
    background = exact(background,
        '      (parsed.hostname === "flow.google.com" || parsed.hostname.endsWith(".flow.google.com")) && parsed.pathname.startsWith("/asb/") ?\n        parsed.pathname.match(/^\\/asb\\/([a-zA-Z0-9_-]+)/)?.[1] :',
        '      flowAsb ? flowAsb[1] :')
    anchor = '              await chrome.storage.session.set({ [`download_${downloadId}`]: { ...task, filename: targetFilename, sanitizedFilename, url } });'
    background = exact(background, anchor, anchor + '\n              await globalThis.__lzxDownloadBound?.(downloadId, { ...task, filename: targetFilename, url });')
    background = exact(background, '      if (!res.success && sanitizedFilename.includes("/")) {',
        '      if (!res.success && !/^FlowBridge\\//.test(sanitizedFilename) && sanitizedFilename.includes("/")) {')
    background = exact(background, '        return await existing.promise;',
        '        const result = await existing.promise;\n'
        '        const item = result?.downloadId != null ? (await chrome.downloads.search({id: result.downloadId}))[0] : null;\n'
        '        if (item && item.exists !== false && ["in_progress", "complete"].includes(item.state)) return result;\n'
        '        if (dedupKey) recentAutoDownloads.delete(dedupKey);\n'
        '        if (mediaFileKey) recentAutoDownloads.delete(mediaFileKey);')
    return background + '''
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
'''


def patch_upstream(source):
    return (patch_ui((source / 'injects/ui.js').read_text('utf-8')),
            patch_proxy((source / 'injects/flow-proxy.js').read_text('utf-8')),
            patch_background((source / 'background.js').read_text('utf-8')))


def patch_isolated(text):
    return exact(text, 'if (window.top !== window) return;',
        'if (window.top !== window || window.__lzxFlowNativeBridge) return;\nwindow.__lzxFlowNativeBridge = true;')
