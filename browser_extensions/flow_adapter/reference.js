// Each attempt keeps its own proof; verified assets may be reused in this page.
(() => {
  if (window.top !== window || window.__lzxReferenceIdentity) return;
  let helpers, ms = (_key, fallback) => fallback * 1000;
  const bindings = new Map(), assets = new Map(), uploaded = new Map();
  let assetScope = '';
  const error = text => Object.assign(Error(text), {code:'FLOW_REFERENCE_NOT_CONFIRMED'});
  const sleep = duration => new Promise(resolve => setTimeout(resolve, duration));
  const name = value => String(value || '').split(/[\\/]/).at(-1).toLowerCase();
  const project = () => {
    const url = new URL(location.href);
    return url.origin + url.pathname.replace(/\/$/,'').replace(/\/edit\/[A-Za-z0-9_-]+$/,'');
  };
  const uploadIntents = new Map(), intentStorage = 'lzx_reference_upload_intents_v1';
  function intentKey(reference, account) {return JSON.stringify([project(),String(account).trim().toLowerCase(),reference.sha256]);}
  function readIntent(key, reset) {
    let item = uploadIntents.get(key);
    try {item = JSON.parse(localStorage.getItem(intentStorage) || '{}')[key] || item;} catch {}
    if (!item || item.reset !== reset || !Number.isFinite(item.at) || Date.now()-item.at > 12*60*60*1000 ||
        !['pending','confirmed'].includes(item.state) || !Array.isArray(item.before_ids)) return null;
    uploadIntents.set(key,item);return item;
  }
  function saveIntent(key, item) {
    uploadIntents.set(key,item);
    while(uploadIntents.size>64)uploadIntents.delete(uploadIntents.keys().next().value);
    try {
      const saved=JSON.parse(localStorage.getItem(intentStorage) || '{}');
      saved[key]=item;
      const entries=Object.entries(saved).filter(([,value])=>Number.isFinite(value?.at) && Date.now()-value.at<12*60*60*1000)
        .sort((a,b)=>b[1].at-a[1].at).slice(0,64);
      localStorage.setItem(intentStorage,JSON.stringify(Object.fromEntries(entries)));
    } catch {} // The in-page latch still protects retries when storage is full.
  }
  function clearIntent(key, source) {
    const current=uploadIntents.get(key);
    if(current?.source!==source)return;
    uploadIntents.delete(key);
    try {const saved=JSON.parse(localStorage.getItem(intentStorage) || '{}');if(saved[key]?.source===source){delete saved[key];localStorage.setItem(intentStorage,JSON.stringify(saved));}} catch {}
  }
  function invalidate() {bindings.clear(); assets.clear(); uploaded.clear(); assetScope = '';}
  function valid(reference) {
    return reference && /^[a-f0-9]{32}$/.test(reference.upload_id || '') && /^[a-f0-9]{64}$/.test(reference.sha256 || '') &&
      /^(?:LZXREF[a-f0-9]{32}|IMG_[a-f0-9]{64}(?:_[a-f0-9]{32})?)\.(png|jpe?g|webp|bmp)$/.test(reference.filename || '') &&
      (reference.filename.startsWith('LZXREF' + reference.upload_id + '.') ||
       reference.filename.startsWith('IMG_' + reference.sha256 + '_' + reference.upload_id + '.') ||
       reference.filename.startsWith('IMG_' + reference.sha256 + '.')) &&
      name(reference.path) === name(reference.filename);
  }
  function same(a, b) {return a && b && ['upload_id','path','filename','sha256','media_id'].every(key => a[key] === b[key]);}
  function mediaKey(value) {
    const text=String(value || '').trim();
    // The same exact resource may be exposed as a full name and as its leaf
    // ID. Do not strip arbitrary prefixes or guess IDs from display names.
    const resource=text.match(/^projects\/[^/]+\/(?:locations\/[^/]+\/)?(?:media|images)\/([A-Za-z0-9_-]{1,256})$/);
    return resource ? resource[1] : text;
  }
  function mediaIds(node) {
    const ids = new Set();
    for (const item of [node, ...Array.from(node?.querySelectorAll?.('[data-media-id],[data-tile-id],img') || [])]) {
      for (const key of ['data-media-id','data-tile-id']) {
        const value = item?.getAttribute?.(key); if (value) ids.add(mediaKey(value));
      }
      const src = item?.currentSrc || item?.src || item?.getAttribute?.('src');
      if (src) try {
        const url = new URL(src, location.href);
        for (const key of ['name','key']) if (url.searchParams.get(key)) ids.add(mediaKey(url.searchParams.get(key)));
        const path = url.pathname.match(/\/(?:image|asb)\/([A-Za-z0-9_-]+)/) || url.pathname.match(/\/([a-f0-9-]{36})(?:[=/]|$)/i);
        if (path) ids.add(path[1]);
      } catch {}
    }
    return ids;
  }
  function images(original) {
    const result = [...original];
    // The ordinary reader may choose a generic card label before reaching the
    // actual filename. Recover only our attempt-unique name and one real ID;
    // tile counts, thumbnails and loose name similarity are never identity.
    for (const tile of document.querySelectorAll('flow-grid-tile-container,flow-image-tile,flow-tile,flow-media-tile,[data-tile-id]')) {
      if (tile.closest?.('flow-video-tile,#flow-batch-overlay,#flow-import-dialog,flow-account-panel') || tile.querySelector?.('flow-video-tile')) continue;
      const texts = [];
      for (const node of [tile,...tile.querySelectorAll('[title],[aria-label],[alt],input,[data-filename]')]) {
        for (const attr of ['title','aria-label','alt','data-filename']) texts.push(node.getAttribute?.(attr) || '');
        if (node.tagName === 'INPUT') texts.push(node.value || '');
      }
      texts.push(String(tile.textContent || '').slice(0,4000));
      const names = new Set(texts.flatMap(text => [...String(text).matchAll(/\b(?:LZXREF[a-f0-9]{32}|IMG_[a-f0-9]{64}(?:_[a-f0-9]{32})?)(?:\.(?:png|jpe?g|webp|bmp))?\b/gi)].map(match=>match[0].toLowerCase())));
      // With/without extension is the same upload. Two different attempt IDs
      // in one container cannot identify a single reference image.
      if (new Set([...names].map(value=>value.replace(/\.[^.]+$/,''))).size !== 1) continue;
      const ids = [...mediaIds(tile)].filter(id=>!String(id).startsWith('img_'));
      if (ids.length !== 1) continue;
      for (const displayName of names) if (!result.some(image=>name(image.displayName)===displayName && (image.primaryMediaKey || image.id)===ids[0])) {
        result.push({id:ids[0],primaryMediaKey:ids[0],displayName,src:tile.querySelector?.('img')?.src || ''});
      }
    }
    return result;
  }
  function visualCandidates() {
    const result=new Map();
    for(const tile of document.querySelectorAll('flow-image-tile,flow-grid-tile-container,flow-media-tile,[data-tile-id]')) {
      if(tile.closest?.('#flow-batch-overlay,#flow-import-dialog,flow-video-tile,flow-account-panel'))continue;
      // A video poster is also an <img>. Require explicit image type as well
      // as excluding video components, playback controls and duration labels.
      if(tile.querySelector?.('video,flow-video-tile,.video-duration,[data-duration],[aria-label*="Play"],[aria-label*="播放"]') ||
          /video/i.test(String(tile.getAttribute?.('data-media-type') || tile.getAttribute?.('data-type') || '')) ||
          [...(tile.querySelectorAll?.('mat-icon') || [])].some(icon=>/^(play_arrow|play_circle|movie|videocam)$/.test(String(icon.textContent || '').trim())))continue;
      if(!tile.matches?.('flow-image-tile') && !tile.querySelector?.('flow-image-tile') &&
          !/^image(?:\/|$)/i.test(String(tile.getAttribute?.('data-media-type') || '')))continue;
      const img=tile.querySelector?.('img');
      if(!img || img.classList?.contains('thumbnail') || img.isConnected===false || !(img.naturalWidth>0))continue;
      const ids=[...mediaIds(tile)].filter(id=>/^[A-Za-z0-9_-]{1,256}$/.test(id) && !id.startsWith('img_'));
      if(ids.length!==1)continue;
      const rect=img.getBoundingClientRect?.();
      if(!rect || rect.width<48 || rect.height<48 || rect.left<0 || rect.top<0 || rect.right>innerWidth || rect.bottom>innerHeight)continue;
      const style=typeof getComputedStyle==='function'?getComputedStyle(img):{};
      if(style.visibility==='hidden' || style.display==='none' || style.opacity==='0')continue;
      let x=rect.left,y=rect.top,width=rect.width,height=rect.height;
      const fit=style.objectFit || 'fill';
      if(fit==='contain' || fit==='scale-down') {
        const scale=Math.min(width/img.naturalWidth,height/img.naturalHeight,fit==='scale-down'?1:Infinity);
        const w=img.naturalWidth*scale,h=img.naturalHeight*scale;
        x+=(width-w)/2;y+=(height-h)/2;width=w;height=h;
      }
      if(width<48 || height<48)continue;
      const covering=document.elementFromPoint?.(x+width/2,y+height/2);
      if(covering && covering!==img && !img.contains?.(covering) && !tile.contains?.(covering))continue;
      result.set(ids[0],{kind:'image',media_id:ids[0],x,y,width,height,fit});
    }
    return [...result.values()];
  }
  function existingReference(reference, candidates=null) {
    const pattern=new RegExp('^IMG_'+reference.sha256+'(?:_([a-f0-9]{32}))?(?:\\.(?:png|jpe?g|webp|bmp))?$','i');
    const named=images(helpers.images()), allowed=new Set();
    for(const tile of document.querySelectorAll('flow-image-tile,flow-grid-tile-container,flow-media-tile,[data-tile-id]')) {
      if(tile.closest?.('flow-video-tile,#flow-batch-overlay,#flow-import-dialog,flow-account-panel') ||
         tile.querySelector?.('video,flow-video-tile,.video-duration,[data-duration],[aria-label*="Play"],[aria-label*="播放"]') ||
         /video/i.test(String(tile.getAttribute?.('data-media-type') || tile.getAttribute?.('data-type') || '')))continue;
      if(!tile.matches?.('flow-image-tile') && !tile.querySelector?.('flow-image-tile') &&
         !/^image(?:\/|$)/i.test(String(tile.getAttribute?.('data-media-type') || '')))continue;
      if([...(tile.querySelectorAll?.('mat-icon') || [])].some(icon=>/^(play_arrow|play_circle|movie|videocam)$/.test(String(icon.textContent || '').trim())))continue;
      const ids=[...mediaIds(tile)].filter(id=>/^[A-Za-z0-9_-]{1,256}$/.test(id) && !id.startsWith('img_'));
      if(ids.length===1)allowed.add(ids[0]);
    }
    // Full content hashes identify duplicate copies of the same image. Pick
    // one exact image ID deterministically; never infer identity from a poster.
    return named.filter(image=>allowed.has(image.primaryMediaKey || image.id) && (!candidates || candidates.has(image.primaryMediaKey || image.id)) && pattern.test(name(image.displayName)))
      .sort((a,b)=>String(a.primaryMediaKey || a.id).localeCompare(String(b.primaryMediaKey || b.id)))[0];
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
  const pickerCards='flow-grid-tile-container,flow-image-tile,flow-tile,flow-media-tile,.media-item,[role="option"],[role="listitem"],mat-option,[data-tile-id],[data-media-id],.tile-row > div,button:has(img),div:has(> img),img';
  function pickerImages(panel) {
    return Array.from(panel.querySelectorAll(pickerCards)).filter(node => node.isConnected!==false &&
      node.tagName!=='FLOW-VIDEO-TILE' && !node.closest?.('flow-video-tile,[data-media-type="video"],[data-type="video"]') &&
      !/video/i.test(String(node.getAttribute?.('data-media-type') || node.getAttribute?.('data-type') || '')) &&
      !node.querySelector?.('video,flow-video-tile,.video-duration,[data-duration],[aria-label*="Play"],[aria-label*="播放"]'));
  }
  function pickerViewport(panel) {
    const explicit=new Set(panel.querySelectorAll('cdk-virtual-scroll-viewport,.cdk-virtual-scroll-viewport,.virtual-scroll-container,.tiles-container,[role="listbox"],[data-radix-scroll-area-viewport],[data-virtuoso-scroller]'));
    const candidates=new Set(explicit);
    for(const card of pickerImages(panel)) {
      let parent=card.parentElement;
      for(let i=0;parent && parent!==panel && i<12;i++,parent=parent.parentElement)candidates.add(parent);
    }
    candidates.add(panel);
    const sized=[...candidates].filter(node=>node && node!==document.body && node!==document.documentElement &&
      node.isConnected!==false && node.clientHeight>0 && node.scrollHeight>=node.clientHeight &&
      (node===panel || !panel.contains || panel.contains(node)))
      .map(node=>{
        let style={};try {if(typeof getComputedStyle==='function')style=getComputedStyle(node);} catch {}
        const overflow=String(style.overflowY || style.overflow || '').toLowerCase();
        const insideCard=node.closest?.('flow-image-tile,flow-video-tile,flow-media-tile,.media-item,[role="option"],mat-option,[data-media-id],[data-tile-id]');
        return {node,overflow,explicit:explicit.has(node),insideCard,
          hidden:style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse',
          count:new Set(Array.from(node.querySelectorAll?.(pickerCards) || []).flatMap(card=>[...mediaIds(card)])).size};
      }).filter(item=>!item.hidden && !item.insideCard && !/^(hidden|clip)$/.test(item.overflow));
    const scrolling=sized.filter(item=>item.node.scrollHeight>item.node.clientHeight+1 &&
      (/^(auto|scroll|overlay)$/.test(item.overflow) || !item.overflow && item.explicit));
    // Overflow geometry alone also describes cropped thumbnails and previews.
    // Prefer the real material viewport, then use unique assets as coverage.
    return (scrolling.length?scrolling:sized.filter(item=>item.explicit || item.node===panel))
      .sort((a,b)=>Number(b.explicit)-Number(a.explicit) || b.count-a.count ||
        (a.node.contains?.(b.node)?1:b.node.contains?.(a.node)?-1:0))[0]?.node;
  }
  function pickerSignature(viewport) {
    return [...new Set(Array.from(viewport.querySelectorAll?.(pickerCards) || []).flatMap(node=>[...mediaIds(node)]))].sort().join('|');
  }
  function scanPicker(scan, panel, viewport, waitMs, poll) {
    const now=Date.now(), top=viewport.scrollTop, height=viewport.scrollHeight, signature=pickerSignature(viewport);
    const settle=Math.min(500,Math.max(poll,waitMs/16)), maximum=Math.min(settle*3,waitMs/4);
    if(scan.viewport!==viewport) {
      const remount=!!scan.viewport;
      Object.assign(scan,{viewport,lastTop:top,height,signature,changed:now,moved:now,before:remount?signature:null,bottom:0,stuck:0});
      if(!remount && top>0){viewport.scrollTop=0;viewport.dispatchEvent?.(new Event('scroll'));scan.lastTop=viewport.scrollTop;scan.before=signature;}
      return;
    }
    if(signature!==scan.signature || height!==scan.height) {
      scan.changed=now;scan.bottom=0;scan.signature=signature;scan.height=height;
    }
    // A virtual list can still expose the previous page after scrollTop moves.
    // Wait for the cards to change before advancing again, with a bounded
    // fallback for static lists whose DOM contains all cards already.
    if(now-scan.moved<settle || signature===scan.before && now-scan.moved<maximum || now-scan.changed<poll)return;
    const loading=!!panel.querySelector?.('[aria-busy="true"],[role="progressbar"],.loading-spinner,.loading-placeholder');
    if(loading){scan.bottom=0;return;}
    if(top>=height-viewport.clientHeight-1) {
      if(!scan.bottom)scan.bottom=now;
      if(now-scan.bottom>=Math.min(1000,Math.max(poll*2,waitMs/8)))
        throw error('素材列表已扫描到底部，未找到本条参考图的准确素材编号；请核对当前素材来源／项目。已停止滚动，不重复上传或选择其他图片。');
      return;
    }
    const next=Math.min(height-viewport.clientHeight,top+Math.max(1,Math.floor(viewport.clientHeight*.6)));
    viewport.scrollTop=next;viewport.dispatchEvent?.(new Event('scroll'));
    if(Math.abs(viewport.scrollTop-top)<1) {
      if(++scan.stuck>=2)throw error('素材列表未响应滚动，已停止查找；保留原图片，不重复上传。');
    } else scan.stuck=0;
    Object.assign(scan,{lastTop:viewport.scrollTop,moved:now,before:signature,bottom:0});
  }
  function pickerPanels() {
    const panels=Array.from(document.querySelectorAll('.cdk-overlay-pane,[role="dialog"],[role="menu"],mat-dialog-container'))
      .filter(node=>node.isConnected!==false && node.offsetWidth>0 && node.offsetHeight>0 && !node.closest('#flow-batch-overlay'));
    const titled=panels.filter(panel=>/选择画面图像|選擇畫面圖像|选择图片|選擇圖片|(?:choose|select)\s+(?:an?\s+)?(?:frame|image)/i.test(
      (panel.getAttribute?.('aria-label') || '')+' '+String(panel.textContent || '').slice(0,180)));
    // Keep the picker shell: a re-rendered inner dialog may have its action
    // footer as a sibling. Never broaden the search to unrelated overlays.
    return titled.length?titled.filter(panel=>!titled.some(other=>other!==panel && other.contains?.(panel))):
      panels.filter(panel=>!panels.some(other=>other!==panel && panel.contains?.(other)));
  }
  function pickerConfirm(panel) {
    const label=/^(?:确认|确定|確認|確定|选择|選取|選擇|添加|新增|完成|使用|插入|设为首帧|設為首幀|添加到提示词|新增至提示|Confirm|Select|Add|Done|OK|Apply|Insert|Use(?: image)?|Use as (?:start|first) frame)$/i;
    return Array.from(panel.querySelectorAll('button')).find(node=>node.isConnected!==false && !node.disabled &&
      (typeof node.getClientRects!=='function' || node.getClientRects().length>0) &&
      node.getAttribute?.('aria-disabled')!=='true' &&
      [node.getAttribute?.('aria-label'),node.textContent].some(text=>label.test(String(text || '').trim())));
  }
  function pickerTarget(row, expected) {
    // Older grids put their selection handler on an inner thumbnail button.
    // Use it only when it identifies this same asset and is not a menu action.
    const inner=row.querySelector?.('button:has(img),[role="option"],[role="button"]:has(img),.container');
    if(inner && inner.getAttribute?.('aria-haspopup')!=='true' &&
        !/menu|more|delete|remove|更多|菜单|選單|删除|刪除|移除/i.test(String(inner.getAttribute?.('aria-label') || ''))) {
      const ids=mediaIds(inner);
      if(ids.size===1 && ids.has(expected))return inner;
    }
    return row;
  }
  function outsidePicker(rect,bounds) {
    return rect && bounds && (rect.bottom<=bounds.top || rect.top>=bounds.bottom ||
      Number.isFinite(rect.right)&&Number.isFinite(bounds.left)&&rect.right<=bounds.left ||
      Number.isFinite(rect.left)&&Number.isFinite(bounds.right)&&rect.left>=bounds.right);
  }
  async function attach(image, footage='VIDEO_FRAMES', waitMs=ms('page_controls_wait_seconds',45)) {
    window.__lzxFlowTransport?.assertUi?.();
    if (!trusted(image) || footage !== 'VIDEO_FRAMES') throw error('本条参考图没有本次上传的素材凭据，未提交生成。');
    if (verify(image, footage)) return;
    await helpers.cleanup?.();
    await window.__lzxFlowTransport?.closeAccountPanel?.();
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
    const deadline = Date.now() + waitMs, scan = {}, poll=ms('page_control_poll_seconds',.25);
    let selected=false,confirmed=false,mountDeadline=deadline;
    while (Date.now() < deadline) {
      window.__lzxFlowTransport?.assertUi?.();
      if(verify(image,footage)){window.__lzxFlowTransport?.renewUiAfterSuccess?.('reference-mounted');return;}
      const panels=pickerPanels();
      if(selected) {
        if(Date.now()>=mountDeadline)break;
        if(!confirmed)for(const panel of panels) {
          if(!pickerImages(panel).some(node=>{const ids=mediaIds(node);return ids.size===1 && ids.has(image.reference.media_id);}))continue;
          const confirm=pickerConfirm(panel);
          if(confirm){helpers.click(confirm);confirmed=true;break;}
        }
        await sleep(poll);continue;
      }
      let searching=null, revealing=false;
      for (const panel of panels) {
        const candidates = pickerImages(panel);
        const viewport=pickerViewport(panel);
        const inList=viewport && viewport!==panel && candidates.some(node=>viewport.contains?.(node));
        const match = candidates.filter(node=>!inList || viewport.contains(node))
          .sort((a,b)=>(a.tagName==='IMG')-(b.tagName==='IMG'))
          .find(node => {const ids=mediaIds(node);return ids.size===1 && ids.has(image.reference.media_id);});
        if (!match) {
          if(viewport && (!searching || candidates.length>searching.count))searching={panel,viewport,count:candidates.length};
          continue;
        }
        // Overscan cards can be in the DOM while outside the visible list.
        // Reveal, then re-query on the next pass rather than clicking a stale
        // virtual node which may now represent a different asset.
        const rect=match.getBoundingClientRect?.(), bounds=viewport?.getBoundingClientRect?.();
        if(outsidePicker(rect,bounds) && match.scrollIntoView) {
          const pending=scan.reveal;
          if(!pending || pending.viewport!==viewport) {
            scan.revealUntil ||= Math.min(deadline,Date.now()+5000);
            if(Date.now()>=scan.revealUntil)throw error('参考图编号已找到，但滚动后素材卡片未能显示；等待已达本次时限，不重复上传。');
            scan.reveal={viewport,until:scan.revealUntil};
            match.scrollIntoView({block:'nearest',inline:'nearest'});
          } else if(Date.now()>=pending.until) {
            throw error('参考图编号已找到，但滚动后素材卡片未能显示；已停止选择，不重复上传。');
          }
          // Do not restart scrolling on every poll: virtualized cards may
          // take several frames to render or temporarily leave the DOM.
          revealing=true;break;
        }
        scan.reveal=null;
        // A child button can be a menu/delete action; a thumbnail may only
        // update the preview. Dispatch on the exact material row itself.
        helpers.click(pickerTarget(match,image.reference.media_id));
        if(verify(image,footage)){window.__lzxFlowTransport?.renewUiAfterSuccess?.('reference-mounted');return;}
        selected=true;mountDeadline=Math.min(deadline,Date.now()+10000);
        break;
      }
      if(scan.reveal?.viewport?.isConnected===false)scan.reveal=null;
      if(!selected && scan.revealUntil && Date.now()>=scan.revealUntil)
        throw error('参考图编号已找到，但滚动后素材卡片未能显示；列表未完成渲染，已停止选择，不重复上传。');
      if(searching && !revealing && !scan.reveal)scanPicker(scan,searching.panel,searching.viewport,waitMs,poll);
      await sleep(poll);
    }
    window.__lzxFlowTransport?.assertUi?.();
    if(verify(image,footage)){window.__lzxFlowTransport?.renewUiAfterSuccess?.('reference-mounted');return;}
    throw error(scan.revealUntil&&!selected?'参考图编号已找到，但滚动后素材卡片未能显示；等待已达本次时限，已停止选择，不重复上传。':
      selected?'已选中准确参考图，但首帧挂载未确认；保留原素材重试，不重复上传。':
      '媒体选择面板中未找到本条参考图的准确素材编号，已停止生成。');
  }
  function resolve(item) {
    const reference = item?.reference, saved = bindings.get(reference?.upload_id);
    if (!valid(reference) || !saved || saved.project !== project() || !same(reference, saved.reference) || item.local_image_path !== reference.path ||
        item.download_path !== `FlowBridge/${reference.upload_id}/result.mp4`) throw error('任务图片与本次上传凭据不一致，未提交生成。');
    return {...saved.image, reference:saved.reference, localImagePath:reference.path};
  }
  function bind(reference, image, media_id, url, source, reused) {
    const receipt = {...reference, media_id, verified:true, source_upload_id:source, reused,
      verification_method:image.visual_source==='local_screenshot'?'local_screenshot':'unique_filename'};
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
    const key = scope ? intentKey(reference,context.account) : '', reset = params.reference_reset || '';
    let intent = key && readIntent(key,reset);
    if (scope !== assetScope) {invalidate(); assetScope = scope;}
    const guard = async () => {
      context.check?.();
      if (url !== project()) {invalidate(); throw error('准备参考图时项目发生变化，未提交生成。');}
      await context.guard?.();
    };
    await guard();
    context.progress?.('checking');
    const pending = uploaded.get(reference.upload_id);
    if (scope && pending && pending.project === url && same(reference,pending.reference) && pending.reset === (params.reference_reset || '')) {
      // An exact upload was already seen before a picker/mounting failure.
      // Retry that ID, even when its card is virtualized or has lost its label.
      const source=pending.source || reference.upload_id;
      const bound = bind(reference,pending.image,pending.media_id,url,source,!!pending.reused);
      context.progress?.('mounting');
      try {await attach(bound); await guard();}
      catch (failure) {
        bindings.delete(reference.upload_id);
        try {await helpers.cleanup?.();} catch {}
        if (failure.code === 'FLOW_REFERENCE_NOT_CONFIRMED') throw error('图片已确认上传，首帧挂载尚未完成；保留素材编号重试，不重复上传：'+failure.message);
        throw failure;
      }
      if (scope) assets.set(reference.sha256,{image:pending.image,media_id:pending.media_id,source,reset:pending.reset});
      while (assets.size > 16) assets.delete(assets.keys().next().value);
      return {uploaded:[],skipped:[reference.path],failed:[],errors:{},reference:bound.reference,upload_id:reference.upload_id,recovered:true,reused:!!pending.reused};
    }
    let cached = scope && assets.get(reference.sha256);
    if (!cached && intent?.state==='confirmed' && /^[A-Za-z0-9_-]{1,256}$/.test(intent.media_id || '') && intent.image)
      cached={image:intent.image,media_id:intent.media_id,source:intent.source,reset};
    if(!cached && scope && !params.reference_reset) {
      const existing=existingReference(reference);
      if(existing)cached={image:existing,media_id:existing.primaryMediaKey || existing.id,
        source:(name(existing.displayName).match(/_([a-f0-9]{32})(?:\.[^.]+)?$/)||[])[1] || reference.upload_id,reset:''};
    }
    // This proof originated from our unique upload and a verified live first
    // frame. Never infer the bytes from a matching filename in project history.
    if (cached && (!params.reference_reset || cached.reset === params.reference_reset)) {
      const bound = bind(reference, cached.image, cached.media_id, url, cached.source, true);
      context.progress?.('mounting');
      try {
        await attach(bound, 'VIDEO_FRAMES', ms('page_reference_reuse_wait_seconds',5));
        await guard();
        assets.delete(reference.sha256); assets.set(reference.sha256, cached);
        return {uploaded:[], skipped:[reference.path], failed:[], errors:{}, reused:true,
          reference:bound.reference, upload_id:reference.upload_id};
      } catch (failure) {
        bindings.delete(reference.upload_id);
        if (failure.code !== 'FLOW_REFERENCE_NOT_CONFIRMED') throw failure;
        await guard();
        uploaded.set(reference.upload_id,{reference:{...reference},image:cached.image,media_id:cached.media_id,
          source:cached.source,reused:true,project:url,reset:params.reference_reset || ''});
        while (uploaded.size>16)uploaded.delete(uploaded.keys().next().value);
        try {await helpers.cleanup?.();} catch {}
        throw error('图片已确认上传，复用素材的首帧挂载尚未完成；保留素材编号重试，不重复上传：'+failure.message);
      }
    }
    await guard();
    await helpers.cleanup?.();
    await guard();
    await window.__lzxFlowTransport?.closeAccountPanel?.();
    const visualEnabled=!!params.visual_reference && typeof helpers.visual==='function';
    const beforeIds=intent?.before_ids || [...new Set([...helpers.images().map(item=>item.primaryMediaKey || item.id),
      ...(visualEnabled?visualCandidates().map(item=>item.media_id):[])].filter(Boolean))];
    let verifyOnly=intent?.state==='pending',legacyRecovery=null;
    const visualFallback=visualEnabled?async()=>{
      const capture=async()=>{
        await guard();
        const candidates=visualCandidates();
        if(!candidates.length || !candidates.some(item=>!beforeIds.includes(item.media_id)))return {pending:true};
        if(candidates.length>40)return null;
        const response=await helpers.visual({upload_id:reference.upload_id,candidates,before_ids:beforeIds,
          viewport:{width:innerWidth,height:innerHeight}});
        await guard();
        const selected=candidates.find(item=>item.media_id===response?.match?.media_id);
        const current=selected&&visualCandidates().find(item=>item.media_id===selected.media_id);
        if(!response?.ok || !selected || !current || beforeIds.includes(selected.media_id) ||
            ['x','y','width','height'].some(key=>Math.abs(selected[key]-current[key])>2))return null;
        return {displayName:reference.filename,primaryMediaKey:selected.media_id,visual_source:'local_screenshot'};
      };
      const ui=window.__lzxFlowTransport?.uiOperation;
      return ui?await ui('upload_images',capture):await capture();
    }:null;
    context.progress?.(verifyOnly?'verifying_upload':'uploading');
    let result, reservedHere=false;
    const undoUnsent=()=>{
      if(!key || !reservedHere)return;
      if(legacyRecovery)saveIntent(key,{...legacyRecovery,dispatch_tracking:2,legacy_recovery:true});
      else clearIntent(key,reference.upload_id);
    };
    const sendUpload=async()=>await helpers.upload(params.paths, {timeoutMs:verifyOnly?Math.min(15000,ms('page_upload_idle_seconds',60)):ms('page_upload_idle_seconds',60),
      maxWaitMs:verifyOnly?Math.min(15000,ms('page_upload_max_seconds',180)):ms('page_upload_max_seconds',180),
      visualFallback,operationGuard:context.check,verifyOnly,
      knownBeforeIds:(verifyOnly || reset)?beforeIds:null,
      beforeDispatch:async()=>{
        context.check?.();
        window.__lzxFlowTransport?.assertUi?.();
        if(url!==project())throw error('准备上传时项目已变化，未拖放图片。');
        if (!key) return;
        const previous=readIntent(key,reset);
        if(previous && !(legacyRecovery && !previous.dispatch_tracking && previous.at===legacyRecovery.at &&
            previous.source===legacyRecovery.source))throw error('此图已有上传记录，正在核对原素材；未重复拖放。');
        intent={state:'pending',at:Date.now(),reset,before_ids:beforeIds,source:reference.upload_id,
          dispatch_tracking:2,...(legacyRecovery?{legacy_recovery:true}:{})};
        saveIntent(key,intent);
        reservedHere=true;
      },
      forceUpload:!!params.reference_reset});
    try {
      result=await sendUpload();
      // Older scripts wrote "pending" before checking the UI lease. Their
      // historical records cannot tell an unsent drag from a sent one. After
      // the full original upload deadline, permit ONE migration replacement
      // only when a fresh verification finds no new/pending image at all and
      // no upload is active. New-version unknown drops remain observe-only.
      if(result.failed?.length && verifyOnly && key && intent && !intent.dispatch_tracking &&
          Date.now()-intent.at>=ms('page_upload_max_seconds',180) &&
          !window.__lzxFlowTransport?.uploadActivity?.(params.paths)?.active &&
          images(helpers.images()).every(item=>beforeIds.includes(item.primaryMediaKey || item.id)) &&
          (!visualEnabled || visualCandidates().every(item=>beforeIds.includes(item.media_id)))) {
        await guard();
        await window.__lzxFlowTransport?.closeAccountPanel?.();
        legacyRecovery={...intent};verifyOnly=false;reservedHere=false;
        context.progress?.('uploading');
        result=await sendUpload();
      }
    }
    catch(failure) {
      if(failure.uploadDispatched===false)undoUnsent();
      throw failure;
    }
    if(result.not_dispatched?.includes(reference.path))undoUnsent();
    await guard();
    if (result.failed?.length) throw error((verifyOnly?'此图此前已发起上传，本次仅核对原素材，未重复拖放：':'本条参考图上传未确认：') + Object.values(result.errors || {}).join('；'));
    const matches = [...images(helpers.images()),...(result.visual_matches || []).filter(item=>item.path===reference.path).map(item=>item.image)].filter(image => name(image.displayName) === name(reference.filename) ||
      name(image.displayName) === name(reference.filename).replace(/\.[^.]+$/, ''));
     const fresh=result.uploaded?.includes(reference.path) || !!params.reference_reset;
     const ids = new Set(matches.map(image => image.primaryMediaKey || image.id).filter(id => id && !String(id).startsWith('img_') && (!fresh || !beforeIds.includes(id))));
    const duplicate = ids.size>1 && reference.filename.startsWith('IMG_'+reference.sha256) ? existingReference(reference,ids) : null;
    if (!ids.size || ids.size>1 && !duplicate) throw error('未找到准确对应本条图片的素材编号；保留原上传核对，不按图块数量放行。');
    const media_id = duplicate ? duplicate.primaryMediaKey || duplicate.id : [...ids][0];
    const image = duplicate || matches.find(item => (item.primaryMediaKey || item.id) === media_id);
    if(key)saveIntent(key,{state:'confirmed',at:Date.now(),reset,before_ids:beforeIds,source:intent?.source || reference.upload_id,
      media_id,image:{id:media_id,primaryMediaKey:media_id,displayName:image.displayName,src:image.src || '',visual_source:image.visual_source || ''}});
    uploaded.set(reference.upload_id,{reference:{...reference},image:{...image},media_id,project:url,reset:params.reference_reset || ''});
    while (uploaded.size > 16) uploaded.delete(uploaded.keys().next().value);
    const bound = bind(reference, image, media_id, url, reference.upload_id, false);
    context.progress?.('mounting');
    try {await attach(bound); await guard();} catch (failure) {
      bindings.delete(reference.upload_id);
      try {await helpers.cleanup?.();} catch {}
      throw failure;
    }
    if (scope) {
      assets.delete(reference.sha256);
      assets.set(reference.sha256, {image:{...image}, media_id, source:reference.upload_id, reset:params.reference_reset || ''});
      while (assets.size > 16) assets.delete(assets.keys().next().value);
    }
    return {...result, reference:bound.reference, upload_id:reference.upload_id, reused:false};
  }
  window.__lzxReferenceIdentity = {sha256_names:true,stable_sha256_names:true, init(api, timing) {helpers=api; ms=timing;}, ready:()=>!!helpers, upload, resolve, attach, verify, invalidate, images, visualCandidates,
    assert(image, footage) {if (!verify(image, footage)) throw error('点击生成前首帧已变化或无法确认，未提交生成。');}, mediaIds};
  window.__lzxFlowTransport?.initReference?.();
})();
