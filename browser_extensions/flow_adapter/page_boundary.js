// The website may use its own UI state, never desktop credentials or controls.
(() => {
  const actions=new Set(['getBgVersion','getInstanceInfo','resetInstanceId','setInstanceName',
    'focusMyTab','setActiveTask','clearActiveTask','download','getDownloadStatus',
    'getStorage','setStorage','nativeFileDrag','nativeClick','nativeHover','closeBrowser']);
  const stateKey=key=>typeof key==='string' && key.length<=2048 &&
    (['settings','presets'].includes(key) || /^(tasks|taskRecord|downloadedUrls|isRunning|currentIndex|currentLineIndex|retryCount)_.+$/.test(key));
  function check(message,sender) {
    if(!message || typeof message!=='object' || Array.isArray(message))return '网页消息格式无效。';
    if(!actions.has(message?.action))return '';
    let url;
    try {url=new URL(sender?.url || sender?.tab?.url);}catch{return '网页消息来源无效。';}
    if(sender?.id!==chrome.runtime.id || sender.frameId!==0 || !Number.isInteger(sender.tab?.id) ||
       url.protocol!=='https:' || url.username || url.password || url.port && url.port!=='443' ||
       !['labs.google','flow.google.com','docs.google.com'].includes(url.hostname) ||
       url.hostname==='docs.google.com' && !url.pathname.startsWith('/videos/'))return '网页消息来源无效。';
    if(message.action==='closeBrowser')return '关闭浏览器须由主程序确认，网页不能关闭全部窗口。';
    if(message.action==='getStorage') {
      const raw=message.keys;
      const keys=typeof raw==='string'?[raw]:Array.isArray(raw)?raw:
        raw && typeof raw==='object'?Object.keys(raw):null;
      if(!keys || keys.length>64 || !keys.every(stateKey))return '网页只允许读取界面状态，不能读取配对及主程序数据。';
    }
    if(message.action==='setStorage') {
      const raw=message.data;
      if(!raw || typeof raw!=='object' || Array.isArray(raw) || Object.keys(raw).length>64 ||
          !Object.keys(raw).every(stateKey))return '网页只允许保存界面状态，不能改写配对及主程序数据。';
    }
    if(message.action==='nativeFileDrag' && typeof globalThis.__lzxNativeUploadGuard!=='function')return '本地图片授权通道未就绪，未上传。';
    return '';
  }
  globalThis.__lzxPageBoundary={check};
})();
