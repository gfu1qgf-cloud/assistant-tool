// Runs before upstream ui.js; all Flow-specific generation remains upstream.
(() => {
  if (window.top !== window || window.__lzxFlowTransport) return;
  let handler, getSnapshot, helpers, lastSnapshot = '', lastIdentity = '', confirmedIdentity = '', lastSnapshotAt = 0, preparedProof = null;
  let timingValues = {}, statusTimer;
  let nativeOperation = false;
  const pageInstance = globalThis.crypto?.randomUUID?.() || String(Date.now()) + ':' + Math.random();
  const timingMs = (key,fallback) => Math.max(1, (Number.isFinite(timingValues[key]) && timingValues[key] > 0 ? timingValues[key] : fallback)*1000);
  function applyTiming(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
    const next = Object.fromEntries(Object.entries(raw).filter(([key,item]) => /^page_/.test(key) &&
      typeof item === 'number' && Number.isFinite(item) && item > 0 && item <= 86400));
    if (JSON.stringify(next) === JSON.stringify(timingValues)) return;
    timingValues = next;
    if (statusTimer) globalThis.clearInterval?.(statusTimer);
    statusTimer = setInterval(() => {accountIssue(); checkFailedCards(); snapshot();}, timingMs('page_poll_seconds',2));
  }
  const emit = data => window.postMessage({sender: 'lzx-flow-page', data}, location.origin);
  let attachedAt = 0, stableSince = 0, stablePrompt, stableSettings, stableUrl = '', lastReadiness = '';
  let lastAccountIssue = '';
  function accountIssue() {
    // Inspect explicit site alerts only. Prompts and arbitrary page text can
    // contain these words without describing the signed-in account.
    const text = [...document.querySelectorAll('[role="alert"], flow-error-page, flow-account-error')]
      .filter(live).map(node => node.textContent || '').join(' ').slice(0, 2000);
    const code = /account\s+(?:(?:has\s+been|is|was)\s+)?(?:disabled|suspended|banned)\b|账号(?:已被|已经|已|被)?(?:停用|封禁|封停)|帳號(?:已遭|已被|已|被)?(?:停用|封鎖)/i.test(text) ? 'FLOW_ACCOUNT_DISABLED' :
      /please\s+(?:sign|log)\s*in|sign\s*in\s+to\s+continue|authentication\s+required|登录(?:已过期|已失效|失效)|需要登录|请先登录|請先登入/i.test(text) ? 'FLOW_ACCOUNT_SIGNED_OUT' :
      /captcha|unusual\s+activity|verify\s+your\s+identity|需要验证|验证码|異常活動|异常活动/i.test(text) ? 'FLOW_ACCOUNT_VERIFICATION' : '';
    if (!code) {lastAccountIssue = ''; return null;}
    const issue = {code, error: text.slice(0, 700)};
    if (lastAccountIssue !== code + issue.error) {
      lastAccountIssue = code + issue.error;
      emit({type: 'account_issue', url: location.href, ...issue});
    }
    return issue;
  }
  const live = node => node && node.isConnected !== false && !node.disabled &&
    node.getAttribute?.('aria-disabled') !== 'true' && node.getAttribute?.('aria-hidden') !== 'true' &&
    (typeof node.getClientRects !== 'function' || node.getClientRects().length > 0);
  function readiness() {
    const url = location.href.split('#')[0];
    const prompt = document.querySelector("flow-rich-text-editor div.ProseMirror[contenteditable='true'], div.ProseMirror[contenteditable='true']");
    const settings = document.querySelector('button.settings-trigger-button') ||
      document.querySelector('div.submit-controls button:not(.generate-icon-button)');
    const present = handler && helpers && (!helpers.initialized || helpers.initialized()) &&
      document.readyState !== 'loading' && live(prompt) && live(settings);
    if (!present || prompt !== stablePrompt || settings !== stableSettings || url !== stableUrl) {
      stableSince = Date.now(); stablePrompt = prompt; stableSettings = settings; stableUrl = url;
    }
    const ready = !!present && Date.now() - attachedAt >= timingMs('page_initial_settle_seconds',2) &&
      Date.now() - stableSince >= timingMs('page_dom_stable_seconds',1.5);
    return {controls_ready: ready, boot_ready: !!handler && !!helpers && (!helpers.initialized || helpers.initialized()), ready_note: ready ? '' :
      present ? '页面控件已出现，正在自动等待加载稳定…' : '正在自动等待提示词框和模型设置控件加载…'};
  }
  async function waitForControls() {
    const url = location.href.split('#')[0], until = Date.now() + timingMs('page_controls_wait_seconds',45);
    while (Date.now() < until) {
      const issue = accountIssue();
      if (issue) throw Object.assign(new Error(issue.error), {code: issue.code});
      if (location.href.split('#')[0] !== url) break;
      if (readiness().controls_ready) return;
      await new Promise(resolve => setTimeout(resolve, timingMs('page_control_poll_seconds',.25)));
    }
    throw Object.assign(new Error('Flow 页面控件尚未加载稳定，程序将自动等待后重试；未提交生成。'), {code: 'FLOW_PAGE_NOT_READY'});
  }
  function pageStatus(state = readiness()) {
    emit({type: 'page_status', url: location.href, page_instance:pageInstance, ...state, note: 'Flow 项目已连接'});
  }
  const locatedResults = new Set();
  function locateResult(tile, mediaId, taskId) {
    const task = getSnapshot?.()?.tasks?.find(t => t.taskId === taskId);
    const bound = task?.mediaBindings?.some(b => b?.schema === 2 && b.mediaIds?.includes(mediaId) && b.readyIds?.includes(mediaId));
    const key = location.href + '#' + mediaId;
    if (!bound || !tile || tile.isConnected === false || typeof tile.scrollIntoView !== 'function' || locatedResults.has(key)) return false;
    tile.scrollIntoView({block: 'nearest', behavior: 'instant'});
    locatedResults.add(key);
    return true;
  }
  function activeEmail() {
    const panel = document.querySelector('flow-account-panel');
    // Match upstream's exact-node rule. Whole-panel text can concatenate an
    // English display name with the email and invent a different address.
    const emails = new Set();
    const add = value => {const email = normalizeEmail(value); if (email) emails.add(email);};
    add(document.querySelector('meta[name="og-profile-acct"]')?.content);
    for (const node of panel?.querySelectorAll?.('*') || []) {
      if (!node.children?.length) add(node.textContent);
    }
    return emails.size === 1 ? [...emails][0] : '';
  }
  function captureAccount() {
    // Capture synchronously while the upstream probe still owns the open menu.
    // A DOM observer is not a reliable hand-off when that menu is removed.
    const email = activeEmail();
    if (email) lastIdentity = email;
    return email;
  }
  async function confirmAccount() {
    // Read the visible identity passively; do not reopen the credits menu for
    // every prepare and submission. The first explicit probe binds this page
    // session; a visible account change still reaches the host's mismatch guard.
    if (!activeEmail() && !lastIdentity && !confirmedIdentity && helpers?.account) {
      const result = await helpers.account();
      confirmedIdentity = normalizeEmail(result?.email);
    }
    const email = activeEmail() || lastIdentity || confirmedIdentity;
    if (!email) throw new Error('无法确认当前 Flow 登录账号，请在该浏览器登录后刷新状态；未提交生成。');
    confirmedIdentity = email;
    return email;
  }
  function normalizeEmail(value) {
    const email = String(value || '').trim().toLowerCase();
    return /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/.test(email) ? email : '';
  }
  function observeAccount() {
    if (!document.documentElement) return;
    new MutationObserver(captureAccount).observe(document.documentElement,
      {subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['content']});
  }
  if (document.documentElement) observeAccount();
  else document.addEventListener('DOMContentLoaded', observeAccount, {once:true});
  function summary() {
    return document.querySelector('button.settings-trigger-button')?.textContent ||
      document.querySelector('div.submit-controls button:not(.generate-icon-button)')?.textContent || '';
  }
  const compact = value => String(value || '').toLowerCase().replace(/\s+/g, '').replace(/[×✕]/g, 'x');
  const signature = settings => JSON.stringify([settings.videoModel, settings.aspectRatio, settings.resolution, settings.duration, settings.count]);
  function settingsPanel() {
    return [...document.querySelectorAll('.cdk-overlay-pane flow-prompt-box-settings, flow-prompt-box-settings, .settings-content-overlay')]
      .find(live) || null;
  }
  const isSelected = node => node.selected === true || node.classList?.contains('mat-button-toggle-checked') ||
    node.getAttribute?.('aria-checked') === 'true' || node.getAttribute?.('aria-pressed') === 'true' ||
    node.querySelector?.('button')?.getAttribute('aria-checked') === 'true' ||
    node.querySelector?.('button')?.getAttribute('aria-pressed') === 'true';
  function durationSeconds(node) {
    const clone = node.cloneNode?.(true);
    clone?.querySelectorAll('mat-icon').forEach(icon => icon.remove());
    const value = (clone?.textContent || node.textContent || node.getAttribute?.('aria-label') || '').trim();
    const match = value.match(/^(\d+)\s*(?:秒|s|sec|seconds?)?$/i);
    return match && Number(match[1]) > 0 ? Number(match[1]) : null;
  }
  function durationGroup(panel) {
    const groups = [...panel.querySelectorAll('flow-toggles, mat-button-toggle-group, [role="radiogroup"], select[aria-label]')];
    return groups.find(group => /时长|時長|秒数|秒數|长度|長度|duration|length/i.test(group.getAttribute('aria-label') || '')) ||
      groups.find(group => {
        const options = [...group.querySelectorAll('mat-button-toggle, [role="radio"], option')];
        return options.length >= 2 && options.some(node => /^\s*\d+\s*(?:秒|s|sec|seconds?)\s*$/i.test(node.textContent || ''));
      });
  }
  async function applyDuration(value) {
    if (value == null) return;
    const target = Number(value), url = location.href, until = Date.now() + timingMs('page_duration_wait_seconds',8);
    const clicked = new WeakSet();
    let opens = 0, nextOpen = 0, previousGroup, optionsStamp = '', optionsAt = 0, selectedOnce = false;
    while (Date.now() < until) {
      if (location.href !== url) throw new Error('设置时长时项目地址发生变化，未提交生成。');
      const panel = settingsPanel();
      if (!panel && opens < 3 && Date.now() >= nextOpen) {
        const trigger = document.querySelector('button.settings-trigger-button') ||
          document.querySelector('div.submit-controls button:not(.generate-icon-button)');
        if (live(trigger)) {trigger.click(); opens++; nextOpen = Date.now() + timingMs('page_duration_reopen_seconds',2);}
      }
      const group = panel && durationGroup(panel);
      if (group) {
        const options = [...group.querySelectorAll('mat-button-toggle, [role="radio"], option')];
        const available = [...new Set(options.map(durationSeconds).filter(v => v != null))].sort((a,b) => a-b);
        const stamp = JSON.stringify(available);
        if (group !== previousGroup || stamp !== optionsStamp) {
          previousGroup = group; optionsStamp = stamp; optionsAt = Date.now(); selectedOnce = false;
        }
        const option = options.find(node => durationSeconds(node) === target);
        if (option && isSelected(option)) {
          if (selectedOnce) return;
          selectedOnce = true;
        } else {
          selectedOnce = false;
          if (option && !clicked.has(option)) {
            const button = option.querySelector?.('button') || option;
            if (live(button)) {
              clicked.add(option);
              if (group.tagName === 'SELECT') {
                group.value = option.value; group.dispatchEvent(new Event('change', {bubbles: true}));
              } else button.click();
            }
          }
        }
        const model = panel.querySelector('.model-select-trigger-content');
        if (!option && available.length && Date.now() - optionsAt >= timingMs('page_duration_options_stable_seconds',2) &&
            /omni.*1[._]?1.*flash/.test(compact(model?.textContent))) {
          throw Object.assign(new Error(`当前 Omni 1.1 Flash 不支持 ${target} 秒；可选时长：${available.join('、')} 秒`),
            {code: 'FLOW_DURATION_UNSUPPORTED', data: {duration: target, available_durations: available}});
        }
      } else {selectedOnce = false; previousGroup = null;}
      await new Promise(resolve => setTimeout(resolve, timingMs('page_control_poll_seconds',.25)));
    }
    throw Object.assign(new Error('时长设置面板尚未稳定，自动等待后重新核对；未提交生成。'),
      {code: 'FLOW_SETTINGS_NOT_READY'});
  }
  async function waitForSettings(settings) {
    const url = location.href, until = Date.now() + timingMs('page_settings_wait_seconds',5);
    let previous = false, failure;
    while (Date.now() <= until) {
      if (location.href !== url) throw new Error('核对参数时项目地址发生变化，未提交生成。');
      try {
        verifySettings(settings, settingsPanel());
        if (previous) return true;
        previous = true;
      } catch (error) {failure = error; previous = false; preparedProof = null;}
      await new Promise(resolve => setTimeout(resolve, timingMs('page_control_poll_seconds',.25)));
    }
    preparedProof = null;
    throw failure || Object.assign(new Error('设置面板尚未稳定，自动等待后重新核对；未提交生成。'), {code: 'FLOW_SETTINGS_NOT_READY'});
  }
  function verifySettings(settings, panel = null) {
    // Read selected controls only. A collapsed summary need not repeat fields
    // that the open settings panel presents separately. Unselected options,
    // requested store values and arbitrary page text are never evidence.
    const chosen = {};
    const put = (key, value) => { if (value) (chosen[key] ||= new Set()).add(compact(value)); };
    if (panel && !live(panel)) throw Object.assign(new Error('设置面板正在重建，自动等待后重新核对；未提交生成。'), {code: 'FLOW_SETTINGS_NOT_READY'});
    if (panel) {
      const model = panel.querySelector('.model-select-trigger-content') ||
        panel.querySelector('button[aria-label="选择模型系列"], button[aria-label="選擇模型系列"], button[aria-label*="model" i]');
      if (model) put('model', model.textContent);
      for (const group of panel.querySelectorAll('flow-toggles, mat-button-toggle-group, [role="radiogroup"]')) {
        const label = compact(group.getAttribute('aria-label'));
        const category = /宽高比|顯示比例|長寬比|aspect/.test(label) ? 'aspect' :
          /分辨率|解析度|resolution/.test(label) ? 'resolution' : /输出数量|生成數量|輸出數量|outputs|count/.test(label) ? 'count' :
          /时长|時長|秒数|秒數|长度|長度|duration|length/.test(label) ? 'duration' : '';
        const seen = new Set();
        for (const node of group.querySelectorAll('mat-button-toggle, [role="radio"], button[aria-checked]')) {
          const button = node.querySelector('button');
          if (!isSelected(node)) continue;
          const value = compact(node.textContent);
          if (seen.has(value)) continue;
          seen.add(value);
          const key = category || (/crop_\d|\d+:\d+/.test(value) ? 'aspect' :
            /\d+p/.test(value) ? 'resolution' : /^x\d+$/.test(value) ? 'count' : '');
          if (key) put(key, key === 'duration' ? String(durationSeconds(node) || '') : value);
        }
      }
      const group = settings.duration != null && durationGroup(panel);
      if (group) for (const node of group.querySelectorAll('mat-button-toggle, [role="radio"], option')) {
        if (isSelected(node)) put('duration', String(durationSeconds(node) || ''));
      }
    }
    const displayed = summary(), text = compact(displayed);
    const ratio = settings.aspectRatio.includes('9_16') ? /9:16|crop_9_16/ : /16:9|crop_16_9/;
    const resolution = settings.resolution.includes('360') ? /360p/ : /720p/;
    const fields = [['model', 'Omni 1.1 Flash 模型', /omni.*1[._]?1.*flash/],
      ['aspect', '画面比例', ratio], ['resolution', '分辨率', resolution], ['count', '单次输出 x1', /[x×✕]\s*1(?!\d)/i]];
    if (settings.duration != null) fields.push(['duration', `${settings.duration} 秒时长`,
      chosen.duration ? new RegExp('^' + Number(settings.duration) + '$') :
        new RegExp('(?<![\\d.])' + Number(settings.duration) + '\\s*(?:秒|seconds?|sec|s)(?![a-z\\d])', 'i')]);
    const missing = fields.filter(([key, _label, matcher]) => chosen[key] ?
      chosen[key].size !== 1 || !matcher.test([...chosen[key]][0]) :
      !matcher.test(['count', 'duration'].includes(key) ? displayed : text));
    if (missing.length) {
      const wrong = missing.some(([key]) => chosen[key]);
      throw Object.assign(new Error('无法确认页面已选中的' + missing.map(([, label]) => label).join('、') +
        (wrong ? '；实际选中参数不一致，请检查 Flow 设置后恢复该浏览器。未提交生成。' :
          '；设置控件尚未完整显示，自动等待后重新核对。未提交生成。')),
        {code: wrong ? 'FLOW_SETTINGS_MISMATCH' : 'FLOW_SETTINGS_NOT_READY'});
    }
    if (panel) preparedProof = {signature: signature(settings), url: location.href, at: Date.now()};
    return true;
  }
  function readCost() {
    const nodes = document.querySelectorAll('button.generate-icon-button, .submit-controls, [data-credit-cost], .generation-cost');
    const values = new Set();
    for (const node of nodes) {
      const explicit = node.getAttribute('data-credit-cost');
      if (explicit !== null && /^\d+$/.test(explicit)) values.add(Number(explicit));
      const text = [node.textContent, node.getAttribute('aria-label'), node.getAttribute('title')].join(' ');
      // Reject ambiguous formatted numbers rather than reading "1,000" as 0.
      for (const match of text.matchAll(/(?<![\d.,])(\d+)(?![\d.,])\s*(?:credits?|积分|積分|点数|點數)(?![a-z])/gi)) values.add(Number(match[1]));
    }
    return values.size === 1 ? [...values][0] : null;
  }
  const pendingResponses = new Map(); // RAM only; never write response bodies to Chrome storage.
  function generationFailed(mediaId, source, reason) {
    if (!['server_status','refund_card'].includes(source)) return;
    const matches = [];
    for (const task of getSnapshot?.()?.tasks || []) for (const binding of task.mediaBindings || []) {
      if (binding?.schema === 2 && binding.source === 'generation_response' && binding.taskId === task.taskId &&
          binding.submissionId && binding.mediaIds?.length === 1 && binding.mediaIds.includes(mediaId) && !binding.readyIds?.includes(mediaId) &&
          !binding.failedIds?.includes(mediaId) && /^FlowBridge\/[a-f0-9]{32}\/result\.mp4$/.test(task.download_path || '')) {
        matches.push({task,binding});
      }
    }
    if (matches.length !== 1) return;
    const {task,binding} = matches[0];
    binding.failedIds = [...(binding.failedIds || []),mediaId];
    const text = String(reason || 'Flow 已确认生成失败，等待自动重试').slice(0,500);
    helpers?.failed?.(task.taskId,binding.submissionId,text);
    snapshot(true);
    emit({type:'generation_failed',taskId:task.taskId,submissionId:binding.submissionId,
      mediaId,download_path:task.download_path,failure_source:source,error:text});
  }
  function checkFailedCards() {
    for (const tile of document.querySelectorAll('flow-video-tile, flow-grid-tile-container, [data-media-id]')) {
      const text = tile.textContent || '';
      if (!/failed to generate|生成失败|生成失敗/i.test(text) || !/refund|退还|退還|退回|退款/i.test(text)) continue;
      const ids = new Set(['data-media-id','data-tile-id','data-generation-id','data-asset-id','data-id','id'].map(key=>tile.getAttribute?.(key)).filter(Boolean));
      for (const id of ids) generationFailed(id,'refund_card','Flow 失败卡片明确提示生成失败并退款');
    }
  }
  window.addEventListener('flow-task-media-failed',event=>generationFailed(event.detail?.mediaId,event.detail?.source,event.detail?.reason));
  function snapshot(force = false) {
    if (!getSnapshot) return;
    if (force) helpers?.reconcileResults?.();
    const state = readiness();
    const stamp = JSON.stringify(state);
    if (stamp !== lastReadiness) {lastReadiness = stamp; pageStatus(state);}
    const data = {...getSnapshot(), ...state, native_busy:nativeOperation, page_instance:pageInstance,
      page_visibility:document.visibilityState || 'unknown', page_focused:document.hasFocus?.() === true};
    const encoded = JSON.stringify(data);
    if (force || encoded !== lastSnapshot || Date.now() - lastSnapshotAt > timingMs('page_snapshot_seconds',15)) {
      lastSnapshot = encoded;
      lastSnapshotAt = Date.now();
      emit({type: 'snapshot', data});
    }
    if (force) {
      // A WebSocket reconnect may have lost the one-shot receipt. Replaying a
      // bounded diagnostic is free; successful media ownership stays in state.
      for (const [submission, receipt] of pendingResponses) {
        const saved = findSubmission(submission);
        const binding = saved && data.tasks?.[saved.rowIndex]?.mediaBindings?.[saved.lineIndex];
        if (!saved || binding?.source === 'generation_response' && binding.mediaIds?.length) pendingResponses.delete(submission);
        else emit(receipt);
      }
    }
  }
  function findSubmission(submissionId) {
    if (typeof submissionId !== 'string' || !submissionId) return null;
    const matches = [];
    for (const [rowIndex, task] of (getSnapshot?.()?.tasks || []).entries()) {
      if (!/^FlowBridge\/[a-f0-9]{32}\/result\.mp4$/.test(task.download_path || '')) continue;
      for (const [lineIndex, binding] of (task.mediaBindings || []).entries()) {
        if (binding?.schema === 2 && binding.taskId === task.taskId && binding.submissionId === submissionId) {
          matches.push({rowIndex, lineIndex, taskId: task.taskId, submissionId});
        }
      }
    }
    return matches.length === 1 ? matches[0] : null;
  }
  function matchesSubmission(process) {
    const saved = findSubmission(process?.submissionId);
    return !!saved && saved.rowIndex === process.rowIndex && saved.lineIndex === process.lineIndex;
  }
  function hasPendingResults() {
    return (getSnapshot?.()?.tasks || []).some(task => (task.mediaBindings || []).some((binding, line) =>
      binding?.schema === 2 && binding.source === 'generation_response' && binding.mediaIds?.length &&
      task.status?.[line] !== 'success'));
  }
  window.__lzxFlowTransport = {
    verifySettings, settingsPanel, waitForSettings, applyDuration, activeEmail, captureAccount, locateResult, readiness, waitForControls,
    findSubmission, matchesSubmission, hasPendingResults,
    batchIdle() {snapshot(true);},
    sitePaused() {emit({type: 'site_pause', reason: 'Flow 网站提示安全限制，已暂停后续提交；保留当前页面，不自动刷新。'});},
    recordGenerationResponse(process, response) {
      const task = getSnapshot?.()?.tasks?.[process.rowIndex];
      if (!task || task.mediaBindings?.[process.lineIndex]?.submissionId !== process.submissionId) return;
      response ??= null;
      const encoded = JSON.stringify(response);
      if (encoded.length > 196608) response = {truncated: true, prefix: encoded.slice(0, 196608)};
      const receipt = {type: 'generation_response', submissionId: process.submissionId, taskId: task.taskId,
        download_path: task.download_path, response};
      if (!pendingResponses.has(process.submissionId) || pendingResponses.get(process.submissionId).response == null) {
        pendingResponses.set(process.submissionId, receipt);
      }
      while (pendingResponses.size > 4) pendingResponses.delete(pendingResponses.keys().next().value);
      emit(receipt);
    },
    bindingMissing(process) {
      const task = getSnapshot?.()?.tasks?.[process.rowIndex];
      if (!task || task.mediaBindings?.[process.lineIndex]?.submissionId !== process.submissionId) return;
      snapshot(true);
      emit({type: 'binding_missing', submissionId: process.submissionId, taskId: task.taskId, download_path: task.download_path});
    },
    send(message) {
      if (message.action === 'get_points_response') {
        // Upstream resets its account cache for this explicit probe and returns
        // the observation captured before closing its menu. Do not replace it
        // with a second, narrower DOM query after that menu has disappeared.
        const email = activeEmail() || normalizeEmail(message.data?.email || message.email);
        confirmedIdentity = email;
        message = {...message, email, data: {...message.data, email}};
        if (!email) message = {...message, success: false, code: 'FLOW_ACCOUNT_NOT_READY',
          error: '账号菜单尚未返回可确认的邮箱，正在自动重试首次读取；不能据此判断未登录。'};
      }
      snapshot(true);
      emit(message);
    },
    attach(receive, state, api) {
      handler = receive; getSnapshot = state; helpers = api;
      window.__lzxReferenceIdentity?.init(api, timingMs);
      attachedAt = Date.now(); stableSince = attachedAt;
      pageStatus();
      snapshot(true);
    }
  };
  window.addEventListener('message', async event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.sender !== 'lzx-flow-extension') return;
    const message = event.data.message;
    if (message?.action === 'configure_timing') {applyTiming(message.values); return;}
    if (message?.timing) applyTiming(message.timing);
    if (!handler || !message) return;
    if (message.action === 'ready') {pageStatus(); return;}
    if (!['get_points', 'prepare', 'upload_images', 'run_tasks', 'snapshot', 'retry_download'].includes(message.action)) return;
    let ownsOperation = false;
    try {
      if (message.action === 'snapshot') { snapshot(true); return; }
      if (nativeOperation) throw Object.assign(Error('此页面仍在处理原操作，等待完成后再恢复；未提交本条生成。'), {code:'FLOW_BATCH_BUSY'});
      nativeOperation = ownsOperation = true;
      snapshot(true);
      if (['prepare', 'upload_images', 'run_tasks', 'get_points'].includes(message.action)) {
        const issue = accountIssue();
        if (issue) throw Object.assign(new Error(issue.error), {code: issue.code});
      }
      if (message.params?.wait_for_ready && ['prepare', 'upload_images', 'run_tasks'].includes(message.action)) {
        await waitForControls();
      }
      if (message.action === 'get_points') { lastIdentity = ''; confirmedIdentity = ''; }
      if (message.action === 'prepare') {
        if (getSnapshot?.()?.running) {
          snapshot(true);
          throw Object.assign(new Error('上一批网页队列仍在收尾，空闲后自动继续；未提交本条生成。'), {code: 'FLOW_BATCH_BUSY'});
        }
        preparedProof = null;
        await helpers.prepare(message.params);
        if (!(preparedProof && preparedProof.signature === signature(message.params) && preparedProof.url === location.href &&
            Date.now() - preparedProof.at < timingMs('page_proof_valid_seconds',5))) verifySettings(message.params);
        const email = await confirmAccount();
        emit({action: 'prepare_response', request_id: message.request_id, success: true,
          data: {verified: true, model: 'omni_1_1_flash', cost: readCost(), summary: summary(), email, page_instance:pageInstance}});
      } else if (message.action === 'upload_images') {
        if (!window.__lzxReferenceIdentity) throw Error('参考图校验脚本尚未就绪，未提交生成。');
        const email = await confirmAccount();
        const guardAccount = async () => {
          if (await confirmAccount() !== email || message.params?.expected_account && email !== message.params.expected_account.toLowerCase()) {
            window.__lzxReferenceIdentity.invalidate();
            throw Object.assign(Error('准备参考图期间登录账号变化，未提交生成。'), {code:'FLOW_ACCOUNT_CHANGED'});
          }
        };
        await guardAccount();
        const data = await window.__lzxReferenceIdentity.upload(message.params || {},
          {account:email, page_instance:pageInstance, guard:guardAccount});
        emit({action:'upload_images_response', request_id:message.request_id, success:true, data});
      } else if (message.action === 'retry_download') {
        const ok = await helpers.retry(message.params?.batch_id);
        emit({action: 'retry_download_response', request_id: message.request_id, success: !!ok,
          error: ok ? '' : '当前项目中没有可重试下载的绑定结果；未重新生成。'});
      } else {
        if (message.action === 'run_tasks') {
          const task = message.params?.tasks?.[0];
          if (message.params?.tasks?.length !== 1 || task?.reference?.upload_id !== message.params?.batch_id || !window.__lzxReferenceIdentity)
            throw Object.assign(Error('本条任务缺少准确参考图凭据，未提交生成。'), {code:'FLOW_REFERENCE_NOT_CONFIRMED'});
          window.__lzxReferenceIdentity.resolve(task);
        }
        if (message.action === 'run_tasks' && message.params?.expected_account &&
            await confirmAccount() !== message.params.expected_account.toLowerCase()) {
          throw new Error('提交前登录账号发生变化或无法确认，未发送新的生成请求；请刷新账号核对。');
        }
        await handler(message);
      }
    } catch (error) {
      emit({action: message.action + '_response', request_id: message.request_id, success: false,
        error: String(error.message || error), code: error.code || '',
        ...(error.code === 'FLOW_DURATION_UNSUPPORTED' ? {data: error.data} : {})});
    } finally {
      if (ownsOperation) nativeOperation = false;
      snapshot(true);
    }
  });
  statusTimer = setInterval(() => {accountIssue(); checkFailedCards(); snapshot();}, timingMs('page_poll_seconds',2));
})();
