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
