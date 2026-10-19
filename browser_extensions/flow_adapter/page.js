// Runs before upstream ui.js; all Flow-specific generation remains upstream.
(() => {
  if (window.top !== window || window.__lzxFlowTransport) return;
  let handler, getSnapshot, helpers, lastSnapshot = '', lastIdentity = '', confirmedIdentity = '', lastSnapshotAt = 0, preparedProof = null;
  const emit = data => window.postMessage({sender: 'lzx-flow-page', data}, location.origin);
  let attachedAt = 0, stableSince = 0, stablePrompt, stableSettings, stableUrl = '', lastReadiness = '';
  const live = node => node && node.isConnected !== false && !node.disabled &&
    node.getAttribute?.('aria-disabled') !== 'true' && node.getAttribute?.('aria-hidden') !== 'true' &&
    (typeof node.getClientRects !== 'function' || node.getClientRects().length > 0);
  function readiness() {
    const url = location.href.split('#')[0];
    const prompt = document.querySelector("flow-rich-text-editor div.ProseMirror[contenteditable='true'], div.ProseMirror[contenteditable='true']");
    const settings = document.querySelector('button.settings-trigger-button') ||
      document.querySelector('div.submit-controls button:not(.generate-icon-button)');
    const present = handler && helpers && document.readyState !== 'loading' && live(prompt) && live(settings);
    if (!present || prompt !== stablePrompt || settings !== stableSettings || url !== stableUrl) {
      stableSince = Date.now(); stablePrompt = prompt; stableSettings = settings; stableUrl = url;
    }
    const ready = !!present && Date.now() - attachedAt >= 2000 && Date.now() - stableSince >= 1500;
    return {controls_ready: ready, ready_note: ready ? '' :
      present ? '页面控件已出现，正在自动等待加载稳定…' : '正在自动等待提示词框和模型设置控件加载…'};
  }
  async function waitForControls() {
    const url = location.href.split('#')[0], until = Date.now() + 45000;
    while (Date.now() < until) {
      if (location.href.split('#')[0] !== url) break;
      if (readiness().controls_ready) return;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw Object.assign(new Error('Flow 页面控件尚未加载稳定，程序将自动等待后重试；未提交生成。'), {code: 'FLOW_PAGE_NOT_READY'});
  }
  function pageStatus(state = readiness()) {
    emit({type: 'page_status', url: location.href, ...state, note: 'Flow 项目已连接'});
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
  new MutationObserver(captureAccount)
    .observe(document.documentElement, {subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ['content']});
  function summary() {
    return document.querySelector('button.settings-trigger-button')?.textContent ||
      document.querySelector('div.submit-controls button:not(.generate-icon-button)')?.textContent || '';
  }
  const compact = value => String(value || '').toLowerCase().replace(/\s+/g, '').replace(/[×✕]/g, 'x');
  const signature = settings => JSON.stringify([settings.videoModel, settings.aspectRatio, settings.resolution, settings.duration, settings.count]);
  function verifySettings(settings, panel = null) {
    // Read selected controls only. A collapsed summary need not repeat fields
    // that the open settings panel presents separately. Unselected options,
    // requested store values and arbitrary page text are never evidence.
    const chosen = {};
    const put = (key, value) => { if (value) (chosen[key] ||= new Set()).add(compact(value)); };
    if (panel) {
      const model = panel.querySelector('.model-select-trigger-content') ||
        panel.querySelector('button[aria-label="选择模型系列"], button[aria-label="選擇模型系列"], button[aria-label*="model" i]');
      if (model) put('model', model.textContent);
      for (const group of panel.querySelectorAll('flow-toggles, mat-button-toggle-group, [role="radiogroup"]')) {
        const label = compact(group.getAttribute('aria-label'));
        const category = /宽高比|顯示比例|長寬比|aspect/.test(label) ? 'aspect' :
          /分辨率|解析度|resolution/.test(label) ? 'resolution' : /输出数量|生成數量|輸出數量|outputs|count/.test(label) ? 'count' : '';
        const seen = new Set();
        for (const node of group.querySelectorAll('mat-button-toggle, [role="radio"], button[aria-checked]')) {
          const button = node.querySelector('button');
          if (!(node.classList.contains('mat-button-toggle-checked') || node.getAttribute('aria-checked') === 'true' ||
              button?.getAttribute('aria-checked') === 'true')) continue;
          const value = compact(node.textContent);
          if (seen.has(value)) continue;
          seen.add(value);
          const key = category || (/crop_\d|\d+:\d+/.test(value) ? 'aspect' :
            /\d+p/.test(value) ? 'resolution' : /^x\d+$/.test(value) ? 'count' : '');
          if (key) put(key, value);
        }
      }
    }
    const text = compact(summary());
    const ratio = settings.aspectRatio.includes('9_16') ? /9:16|crop_9_16/ : /16:9|crop_16_9/;
    const resolution = settings.resolution.includes('360') ? /360p/ : /720p/;
    const fields = [['model', 'Omni 1.1 Flash 模型', /omni.*1[._]?1.*flash/],
      ['aspect', '画面比例', ratio], ['resolution', '分辨率', resolution], ['count', '单次输出 x1', /x1(?!\d)/]];
    const missing = fields.filter(([key, _label, matcher]) => chosen[key] ?
      chosen[key].size !== 1 || !matcher.test([...chosen[key]][0]) : !matcher.test(text));
    if (missing.length) {
      throw new Error('无法确认页面已选中的' + missing.map(([, label]) => label).join('、') +
        '；请检查 Flow 设置后恢复该浏览器。未提交生成。');
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
  function snapshot(force = false) {
    if (!getSnapshot) return;
    const state = readiness();
    const stamp = JSON.stringify(state);
    if (stamp !== lastReadiness) {lastReadiness = stamp; pageStatus(state);}
    const data = {...getSnapshot(), ...state};
    const encoded = JSON.stringify(data);
    if (force || encoded !== lastSnapshot || Date.now() - lastSnapshotAt > 15000) {
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
    verifySettings, activeEmail, captureAccount, locateResult, readiness, waitForControls,
    findSubmission, matchesSubmission, hasPendingResults,
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
      attachedAt = Date.now(); stableSince = attachedAt;
      pageStatus();
      snapshot(true);
    }
  };
  window.addEventListener('message', async event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.sender !== 'lzx-flow-extension') return;
    const message = event.data.message;
    if (!handler || !message) return;
    if (message.action === 'ready') {pageStatus(); return;}
    if (!['get_points', 'prepare', 'upload_images', 'run_tasks', 'snapshot', 'retry_download'].includes(message.action)) return;
    try {
      if (message.action === 'snapshot') { snapshot(true); return; }
      if (message.params?.wait_for_ready && ['prepare', 'upload_images', 'run_tasks'].includes(message.action)) {
        await waitForControls();
      }
      if (message.action === 'get_points') { lastIdentity = ''; confirmedIdentity = ''; }
      if (message.action === 'prepare') {
        preparedProof = null;
        await helpers.prepare(message.params);
        if (!(preparedProof && preparedProof.signature === signature(message.params) && preparedProof.url === location.href &&
            Date.now() - preparedProof.at < 5000)) verifySettings(message.params);
        const email = await confirmAccount();
        emit({action: 'prepare_response', request_id: message.request_id, success: true,
          data: {verified: true, model: 'omni_1_1_flash', cost: readCost(), summary: summary(), email}});
      } else if (message.action === 'retry_download') {
        const ok = await helpers.retry(message.params?.batch_id);
        emit({action: 'retry_download_response', request_id: message.request_id, success: !!ok,
          error: ok ? '' : '当前项目中没有可重试下载的绑定结果；未重新生成。'});
      } else {
        if (message.action === 'run_tasks' && message.params?.expected_account &&
            await confirmAccount() !== message.params.expected_account.toLowerCase()) {
          throw new Error('提交前登录账号发生变化或无法确认，未发送新的生成请求；请刷新账号核对。');
        }
        await handler(message);
      }
    } catch (error) {
      emit({action: message.action + '_response', request_id: message.request_id, success: false,
        error: String(error.message || error), code: error.code || ''});
    }
  });
  setInterval(() => snapshot(), 2000);
})();
