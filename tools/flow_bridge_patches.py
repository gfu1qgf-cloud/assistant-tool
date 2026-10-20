"""Exact bridge seams for pristine FlowCue 1.1.4; never edit vendor originals."""

QUIET = '  const console = window.__lzxFlowTransport ? {log() {}, warn: globalThis.console.warn.bind(globalThis.console), error: globalThis.console.error.bind(globalThis.console)} : globalThis.console;'


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
'''


def patch_ui(ui):
    ui = exact(ui, '  "use strict";', '  "use strict";\n' + QUIET)
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
        '      }\n' + binding_line)
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
        '    if (window.__lzxFlowTransport && binding.source !== "generation_response") return false;')
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
    ui = exact(ui, '    // 4. 关闭设置面板\n',
        '    if (window.__lzxFlowTransport) {\n'
        '      const currentPanel = document.querySelector(".cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay");\n'
        '      window.__lzxFlowTransport.verifySettings(settings, currentPanel || overlay);\n'
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
    ui = exact(ui, '      if (panelOpen) {\n        console.warn(`[FlowUI] 当前模型设置面板中无时长选项',
        '      if (panelOpen) {\n        if (window.__lzxFlowTransport) throw durationError("当前模型没有可确认的时长选项，请使用模型默认时长");\n        console.warn(`[FlowUI] 当前模型设置面板中无时长选项')
    ui = exact(ui, '    if (!option) {\n      const availableOpts = options().map',
        '    if (!option) {\n      if (window.__lzxFlowTransport) throw durationError(`当前模型不支持 ${target} 秒，请选择模型默认或支持的时长`);\n      const availableOpts = options().map')
    ui = exact(ui, '        console.warn("[FlowUI] ⚠️ 生成按钮超时未就绪，强制解除 disabled 限制触发点击");',
        '        if (window.__lzxFlowTransport) throw durationError("生成按钮未就绪，请检查网站提示后重试");\n'
        '        console.warn("[FlowUI] ⚠️ 生成按钮超时未就绪，强制解除 disabled 限制触发点击");')
    ui = exact(ui, '    alarmCountdown = 45;',
        '    if (window.__lzxFlowTransport) {\n'
        '      if (alarmTimer) clearInterval(alarmTimer);\n'
        '      alarmTimer = null;\n'
        '      const notice = document.getElementById("flow-alarm-message");\n'
        '      if (notice) notice.innerText = "网站提示安全限制，队列已暂停；保留当前页面，不自动刷新或重复生成。";\n'
        '      window.__lzxFlowTransport.sitePaused?.();\n'
        '      return;\n'
        '    }\n    alarmCountdown = 45;')
    return ui


def patch_proxy(proxy):
    proxy = exact(proxy, '  "use strict";', '  "use strict";\n' + QUIET)
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
    return background + '\n// Host-authenticated recovery reuses the exact upstream bound downloader.\nglobalThis.__lzxRetryBoundDownload = handleDownload;\n'


def patch_upstream(source):
    return (patch_ui((source / 'injects/ui.js').read_text('utf-8')),
            patch_proxy((source / 'injects/flow-proxy.js').read_text('utf-8')),
            patch_background((source / 'background.js').read_text('utf-8')))
