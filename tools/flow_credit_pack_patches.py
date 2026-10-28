"""Small additive submit seam; keep the vendor's single-job mode intact."""
PACKED_SUBMIT = r'''
  async function submitPackedTask(message) {
    const p = message.params || {}, items = p.tasks || [];
    if (batchPromise || items.length !== 1 || p.packed !== true || p.clear_existing !== false ||
        state.tasks.length >= 512 || state.tasks.some(t => t.download_path === items[0].download_path)) {
      throw Object.assign(Error("分包提交条件变化，保留原任务；未重复提交。"), {code:"FLOW_BATCH_UNCONFIRMED"});
    }
    const rowIndex = state.tasks.length, wasRunning = state.isRunning;
    if (importTaskList(items) !== 1) throw Error("无法导入本条分包任务，未提交生成。");
    const task = state.tasks[rowIndex];
    task.lzxPacked = true;
    state.isRunning = true;
    state.currentIndex = rowIndex; state.currentLineIndex = 0;
    await saveState();
    try {
      updateLineStatus(rowIndex, 0, "pending", "同图积分分包：正在提交本条任务...");
      await submitTaskToPage({image:task.image, lineText:task.text,
        presetBefore:"", presetAfter:"", footageType:task.footageType, count:1,
        duration:task.duration, rowIndex, lineIndex:0, download_path:task.download_path,
        resolution:task.resolution, aspectRatio:task.aspectRatio, videoModel:task.videoModel});
      const until = Date.now() + 12000;
      do {
        const binding = task.mediaBindings?.[0];
        if (binding?.schema === 2 && binding.source === "generation_response" &&
            binding.taskId === task.taskId && binding.submissionId && binding.mediaIds?.length === 1) {
          if (!task.downloaded?.[0] && task.status?.[0] !== "failed")
            updateLineStatus(rowIndex, 0, "generating", "已确认本条媒体编号；生成和下载独立进行");
          await saveState();
          return {packed:true, taskId:task.taskId, submissionId:binding.submissionId, mediaIds:binding.mediaIds};
        }
        if (window.__lzxFlowTransport?.sitePaused?.()) break;
        await delay(100);
      } while (Date.now() < until);
      throw Object.assign(Error("本条提交回执未在时限内返回，停止继续提交此包；保留原任务核对，不重复生成。"),
        {code:"FLOW_BATCH_UNCONFIRMED"});
    } finally {
      state.isRunning = wasRunning;
      if (window.currentProcess?.rowIndex === rowIndex) window.currentProcess = null;
      state.activeTasksQueue = [];
      await saveState();
    }
  }

'''


def patch_ui(ui):
    def replace(before, after):
        nonlocal ui
        if ui.count(before) != 1:
            raise ValueError('分包接入点已变化：' + before[:100])
        ui = ui.replace(before, after)
    replace('  async function runBatch(singleRowIndex = null) {', PACKED_SUBMIT + '  async function runBatch(singleRowIndex = null) {')
    replace('}), {prepare: syncPageVideoSettings, initialized:', '}), {submitPacked: submitPackedTask, prepare: syncPageVideoSettings, initialized:')
    replace('          if (state.tasks.length !== 1 || state.tasks[0].download_path !== path) return false;',
        '''          const task = state.tasks.find(t => t.download_path === path);
          if (!task) return false;
          if (task.lzxPacked) {
            for (const binding of task.mediaBindings || []) if (binding) binding.desktopParked = true;
            return true;
          }
          if (state.tasks.length !== 1) return false;''')
    replace('          stopBatch();\n          return true;\n        },\n        account:',
        '          if (!state.tasks[row].lzxPacked) stopBatch();\n          return true;\n        },\n        account:')
    replace('          return row >= 0 ? retrySavedVideo(state.tasks[row], row, 0, true) : false;',
        '''          if (row >= 0) for (const binding of state.tasks[row].mediaBindings || []) if (binding) delete binding.desktopParked;
          return row >= 0 ? retrySavedVideo(state.tasks[row], row, 0, true) : false;''')
    replace('    if (window.__lzxFlowTransport && (binding.source !== "generation_response" || binding.failedIds?.includes(mediaId))) return false;',
        '    if (window.__lzxFlowTransport && (binding.source !== "generation_response" || binding.desktopParked || binding.failedIds?.includes(mediaId))) return false;')
    return ui
