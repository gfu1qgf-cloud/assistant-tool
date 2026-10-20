// Each attempt keeps its own proof; verified assets may be reused in this page.
(() => {
  if (window.top !== window || window.__lzxReferenceIdentity) return;
  let helpers, ms = (_key, fallback) => fallback * 1000;
  const bindings = new Map(), assets = new Map();
  let assetScope = '';
  const error = text => Object.assign(Error(text), {code:'FLOW_REFERENCE_NOT_CONFIRMED'});
  const sleep = duration => new Promise(resolve => setTimeout(resolve, duration));
  const name = value => String(value || '').split(/[\\/]/).at(-1).toLowerCase();
  const project = () => location.href.split('#')[0];
  function invalidate() {bindings.clear(); assets.clear(); assetScope = '';}
  function valid(reference) {
    return reference && /^[a-f0-9]{32}$/.test(reference.upload_id || '') && /^[a-f0-9]{64}$/.test(reference.sha256 || '') &&
      /^LZXREF[a-f0-9]{32}\.(png|jpe?g|webp|bmp)$/.test(reference.filename || '') &&
      reference.filename.startsWith('LZXREF' + reference.upload_id + '.') && name(reference.path) === name(reference.filename);
  }
  function same(a, b) {return a && b && ['upload_id','path','filename','sha256','media_id'].every(key => a[key] === b[key]);}
  function mediaIds(node) {
    const ids = new Set();
    for (const item of [node, ...Array.from(node?.querySelectorAll?.('[data-media-id],[data-tile-id],img') || [])]) {
      for (const key of ['data-media-id','data-tile-id']) {
        const value = item?.getAttribute?.(key); if (value) ids.add(value);
      }
      const src = item?.currentSrc || item?.src || item?.getAttribute?.('src');
      if (src) try {
        const url = new URL(src, location.href);
        for (const key of ['name','key']) if (url.searchParams.get(key)) ids.add(url.searchParams.get(key));
        const path = url.pathname.match(/\/(?:image|asb)\/([A-Za-z0-9_-]+)/) || url.pathname.match(/\/([a-f0-9-]{36})(?:[=/]|$)/i);
        if (path) ids.add(path[1]);
      } catch {}
    }
    return ids;
  }
  const hasMedia = node => !!node?.querySelector?.('img,[data-media-id],flow-ingredient-chip,.ingredient-chip,mat-chip');
  function slots() {
    const bar = document.querySelector('flow-ingredient-bar.prompt-ingredient-bar,.prompt-ingredient-bar,flow-ingredient-bar');
    const all = Array.from(bar?.querySelectorAll('.frame-trigger') || []);
    const end = node => /(结束|結束|\bend\b)/i.test((node.textContent || '') + ' ' + (node.getAttribute('aria-label') || ''));
    return {bar, start:all.find(node => !end(node)), ends:all.filter(end)};
  }
  function trusted(image) {
    const reference = image?.reference, saved = bindings.get(reference?.upload_id);
    return valid(reference) && saved && saved.project === project() &&
      same(reference, saved.reference) && image.primaryMediaKey === reference.media_id;
  }
  function verify(image, footage='VIDEO_FRAMES') {
    if (footage !== 'VIDEO_FRAMES' || !trusted(image)) return false;
    const {start, ends} = slots();
    if (!start || ends.some(hasMedia)) return false;
    const selected = mediaIds(start);
    if (selected.size !== 1 || !selected.has(image.reference.media_id)) return false;
    const imgs = Array.from(start.querySelectorAll('img'));
    return imgs.length <= 1 && imgs.every(img => mediaIds(img).has(image.reference.media_id));
  }
  async function clearEnds() {
    for (let i=0; i<6; i++) {
      const end = slots().ends.find(hasMedia);
      if (!end) return;
      const button = Array.from(end.querySelectorAll('button')).find(node =>
        /remove|clear|移除|删除|刪除|清除/i.test((node.getAttribute('aria-label') || '') + ' ' + (node.textContent || '') + ' ' + node.className));
      if (!button) throw error('结束帧仍有旧素材且无法安全移除；未提交生成。');
      helpers.click(button); await sleep(ms('page_control_poll_seconds', .25));
    }
    if (slots().ends.some(hasMedia)) throw error('结束帧旧素材尚未移除；未提交生成。');
  }
  async function attach(image, footage='VIDEO_FRAMES') {
    if (!trusted(image) || footage !== 'VIDEO_FRAMES') throw error('本条参考图没有本次上传的素材凭据，未提交生成。');
    await clearEnds();
    if (verify(image, footage)) return;
    let start = slots().start;
    if (hasMedia(start)) {
      const remove = Array.from(start.querySelectorAll('button')).find(node =>
        /remove|clear|移除|删除|刪除|清除/i.test((node.getAttribute('aria-label') || '') + ' ' + (node.textContent || '') + ' ' + node.className));
      if (remove) {helpers.click(remove); await sleep(ms('page_control_poll_seconds',.25));start=slots().start;}
    }
    const trigger = start?.querySelector('button.empty-chip') || Array.from(start?.querySelectorAll('button') || []).find(node =>
      !/remove|clear|移除|删除|刪除|清除/i.test((node.getAttribute('aria-label') || '') + ' ' + (node.textContent || '') + ' ' + node.className));
    if (!trigger) throw error('找不到首帧选择入口；未使用页面已有的其他图片。');
    helpers.click(trigger);
    const deadline = Date.now() + ms('page_controls_wait_seconds',45);
    while (Date.now() < deadline) {
      const panels = Array.from(document.querySelectorAll('.cdk-overlay-pane,[role="dialog"],[role="menu"],mat-dialog-container'))
        .filter(node => node.offsetWidth > 0 && node.offsetHeight > 0 && !node.closest('#flow-batch-overlay'));
      for (const panel of panels) {
        const candidates = Array.from(panel.querySelectorAll('flow-grid-tile-container,flow-image-tile,[data-tile-id],[data-media-id],button:has(img)'));
        const match = candidates.find(node => mediaIds(node).has(image.reference.media_id));
        if (!match) continue;
        helpers.click(match.querySelector('button,img') || match);
        await sleep(ms('page_control_poll_seconds',.25));
        const confirm = Array.from(panel.querySelectorAll('button')).find(node => !node.disabled &&
          /^(确认|确定|確認|確定|选择|選取|選擇|添加|新增|完成|使用|Confirm|Select|Add|Done|OK|Apply)$/i.test((node.textContent || node.getAttribute('aria-label') || '').trim()));
        if (confirm?.isConnected) helpers.click(confirm);
        for (let i=0; i<8; i++) {
          if (verify(image, footage)) return;
          await sleep(ms('page_control_poll_seconds',.25));
        }
        throw error('首帧实际素材编号与本条参考图不一致，已停止生成。');
      }
      await sleep(ms('page_control_poll_seconds',.25));
    }
    throw error('媒体选择面板中未找到本条参考图的准确素材编号，已停止生成。');
  }
  function resolve(item) {
    const reference = item?.reference, saved = bindings.get(reference?.upload_id);
    if (!valid(reference) || !saved || saved.project !== project() || !same(reference, saved.reference) || item.local_image_path !== reference.path ||
        item.download_path !== `FlowBridge/${reference.upload_id}/result.mp4`) throw error('任务图片与本次上传凭据不一致，未提交生成。');
    return {...saved.image, reference:saved.reference, localImagePath:reference.path};
  }
  function bind(reference, image, media_id, url, source, reused) {
    const receipt = {...reference, media_id, verified:true, source_upload_id:source, reused};
    const bound = {...image, primaryMediaKey:media_id, reference:receipt};
    bindings.set(reference.upload_id, {reference:receipt, image:bound, project:url});
    while (bindings.size > 16) bindings.delete(bindings.keys().next().value);
    return bound;
  }
  async function upload(params, context={}) {
    const reference = params.reference;
    if (!valid(reference) || params.upload_id !== reference.upload_id || params.paths?.length !== 1 || params.paths[0] !== reference.path)
      throw error('参考图上传请求缺少本次独立副本标识。');
    const url = project();
    const scope = context.page_instance && context.account ?
      JSON.stringify([url, context.page_instance, String(context.account).trim().toLowerCase()]) : '';
    if (scope !== assetScope) {invalidate(); assetScope = scope;}
    const guard = async () => {
      if (url !== project()) {invalidate(); throw error('准备参考图时项目发生变化，未提交生成。');}
      await context.guard?.();
    };
    await guard();
    const cached = scope && assets.get(reference.sha256);
    // This proof originated from our unique upload and a verified live first
    // frame. Never infer the bytes from a matching filename in project history.
    if (cached && (!params.reference_reset || cached.reset === params.reference_reset)) {
      const bound = bind(reference, cached.image, cached.media_id, url, cached.source, true);
      try {
        await attach(bound);
        await guard();
        assets.delete(reference.sha256); assets.set(reference.sha256, cached);
        return {uploaded:[], skipped:[reference.path], failed:[], errors:{}, reused:true,
          reference:bound.reference, upload_id:reference.upload_id};
      } catch (failure) {
        bindings.delete(reference.upload_id); assets.delete(reference.sha256);
        if (failure.code !== 'FLOW_REFERENCE_NOT_CONFIRMED') throw failure;
        await guard();
        // A removed or unmountable cached asset gets one fresh upload. The
        // ordinary bounded upload retries still handle a failed fresh attempt.
        await helpers.cleanup?.();
      }
    }
    await guard();
    const result = await helpers.upload(params.paths, {timeoutMs:ms('page_controls_wait_seconds',45)});
    await guard();
    if (result.failed?.length) throw error('本条参考图上传未确认：' + Object.values(result.errors || {}).join('；'));
    const matches = helpers.images().filter(image => name(image.displayName) === name(reference.filename) ||
      name(image.displayName) === name(reference.filename).replace(/\.[^.]+$/, ''));
    const ids = new Set(matches.map(image => image.primaryMediaKey || image.id).filter(id => id && !String(id).startsWith('img_')));
    if (ids.size !== 1) throw error('未找到唯一对应本条独立副本的图片素材；不按同名旧图或图块数量放行。');
    const media_id = [...ids][0], image = matches.find(item => (item.primaryMediaKey || item.id) === media_id);
    const bound = bind(reference, image, media_id, url, reference.upload_id, false);
    try {await attach(bound); await guard();} catch (failure) {bindings.delete(reference.upload_id);throw failure;}
    if (scope) {
      assets.delete(reference.sha256);
      assets.set(reference.sha256, {image:{...image}, media_id, source:reference.upload_id, reset:params.reference_reset || ''});
      while (assets.size > 16) assets.delete(assets.keys().next().value);
    }
    return {...result, reference:bound.reference, upload_id:reference.upload_id, reused:false};
  }
  window.__lzxReferenceIdentity = {init(api, timing) {helpers=api; ms=timing;}, upload, resolve, attach, verify, invalidate,
    assert(image, footage) {if (!verify(image, footage)) throw error('点击生成前首帧已变化或无法确认，未提交生成。');}, mediaIds};
})();
