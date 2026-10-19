// injects/flow-proxy.js - 核心代理拦截脚本，在 document_start 运行于 MAIN 页面上下文

(function() {
  "use strict";
  if (window.top !== window) return;
  
  console.log("[FlowProxy] 代理脚本注入成功，正在启动拦截...");

  // ----------------------------------------------------
  // 0. 适配 Trusted Types 安全策略 (针对 flow.google.com 等开启了 require-trusted-types-for 'script' 的站点)
  // ----------------------------------------------------
  if (typeof window !== "undefined" && window.trustedTypes && window.trustedTypes.createPolicy) {
    try {
      if (!window.trustedTypes.defaultPolicy) {
        window.trustedTypes.createPolicy("default", {
          createHTML: (string) => string,
          createScript: (string) => string,
          createScriptURL: (string) => string,
        });
        console.log("[FlowProxy] 成功在 document_start 注册 default TrustedTypePolicy！");
      }
    } catch (e) {
      console.warn("[FlowProxy] 注册 default TrustedTypePolicy 提示:", e);
    }
  }

  // Google Vids (docs.google.com) 使用 DOM 级自动化且包含 Object.prototype SEAL 检测，无需注入 Flow 专属的网络/原型钩子
  if (location.hostname === "docs.google.com") {
    console.log("[FlowProxy] 检测到 Google Vids 页面，跳过 Flow 专属原型钩子，交由 ui.js Vids 引擎接管。");
    return;
  }

  // ----------------------------------------------------
  // 1. Hook Object.prototype.hasOwnProperty
  // ----------------------------------------------------
  // 通过 Hook hasOwnProperty 拦截 React/Next.js 组件上下文，获取 promptBoxStore 和 onSubmit 方法
  const origHasOwnProperty = Object.prototype.hasOwnProperty;
  Object.prototype.hasOwnProperty = function(prop) {
    const obj = this;
    if (obj && obj.promptBoxStore && obj.onSubmit) {
      window.promptBoxStore = obj.promptBoxStore;
      
      try {
        console.log("[FlowProxy] 成功拦截 promptBoxStore! 动作列表:", Object.keys(obj.promptBoxStore.getState().actions || {}));
        console.log("[FlowProxy] 拦截时 promptBoxStore 的当前状态:", obj.promptBoxStore.getState());
      } catch (err) {
        console.error("[FlowProxy] 打印 promptBoxStore 信息失败:", err);
      }
      
      // 当不是提示词编辑框模式且拥有 placeholder 时，暴露出全局的生成触发方法
      if (!obj.promptBoxId && obj.placeholder) {
        window.generateVideo = () => {
          console.log("[FlowProxy] 触发 window.generateVideo()");
          return obj.onSubmit(true, true);
        };
      }
    }
    return origHasOwnProperty.call(this, prop);
  };

  // ----------------------------------------------------
  // ----------------------------------------------------
  // 2. Hook window.fetch 与 XMLHttpRequest 深度拦截 API 响应与流式传输
  // ----------------------------------------------------
  window.currentProcess = null; // 由 ui.js 设置，表示当前正在处理的行元数据

  // 辅助函数：深度反转义 BoQ 响应文本 (支持单层/多层嵌套 JSON 的 \\u003d、\\u0026、\\/、\" 等，防止残留反斜杠截断签名 URL)
  function unescapeBoQ(text) {
    if (typeof text !== "string" || !text) return "";
    try {
      let res = text;
      for (let i = 0; i < 3; i++) {
        res = res
          .replace(/\\+u002f/gi, "/")
          .replace(/\\+u0026/gi, "&")
          .replace(/\\+u003d/gi, "=")
          .replace(/\\+u0022/gi, '"')
          .replace(/\\+u003c/gi, "<")
          .replace(/\\+u003e/gi, ">")
          .replace(/\\+\//g, "/")
          .replace(/\\+"/g, '"')
          .replace(/\\\\/g, "\\");
      }
      return res;
    } catch {
      return text;
    }
  }

  // 校验 flow-content.google 视频直链是否包含完整的 CDN 签名参数 (防止截断的 ?Expires 导致下载成 .xml 错误文件)
  function isCompleteSignedFlowVideoUrl(url) {
    try {
      const parsed = new URL(url);
      if (parsed.hostname === "flow-content.google") {
        return parsed.pathname.startsWith("/video/") &&
               Boolean(parsed.searchParams.get("Expires")) &&
               Boolean(parsed.searchParams.get("Signature"));
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  // 过滤 Google 官方展示页及公共静态样例视频
  function isStaticShowcaseAsset(url) {
    if (!url || typeof url !== "string") return true;
    const u = url.toLowerCase();
    return u.includes("/website/flow/") || 
           u.includes("/flow_camera/") || 
           u.includes("/showcase/") || 
           u.includes("gstatic.com") || 
           u.includes("recaptcha") ||
           u.includes("left.mp4") ||
           u.includes("right.mp4") ||
           u.includes("center.mp4") ||
           u.includes("high.mp4");
  }

  // 辅助函数：提取字符串中所有的 http/https 视频直链与生成的 mediaName
  function extractMediaAssets(text, mediaUrlResponse = false) {
    if (typeof text !== "string" || !text) return { urls: [], mediaNames: [] };
    const cleanText = unescapeBoQ(text);
    const urls = new Set();
    const mediaNames = new Set();
    // 生成后页面的真实视频 CDN：无 .mp4 后缀，图片使用独立的 /image/ 路径。必须包含完整签名参数。
    for (const value of cleanText.match(/https:\/\/flow-content\.google\/video\/[^\s"'<>\\`]+/gi) || []) {
      const candidate = value.replace(/&amp;/g, "&");
      if (isCompleteSignedFlowVideoUrl(candidate)) {
        urls.add(candidate);
      }
    }

    // 保存页确认 uurnC 是 GetMediaUrl；此接口可返回不带 .mp4 后缀的签名视频地址。
    if (mediaUrlResponse) {
      const matches = cleanText.match(/https?:\/\/[^\s"'<>\\]+/g) || [];
      for (const value of matches) {
        try {
          const cleanVal = value.replace(/&amp;/g, "&");
          const parsed = new URL(cleanVal);
          if (/(^|\.)(googleusercontent\.com|googlevideo\.com|storage\.googleapis\.com)$/.test(parsed.hostname) && !isStaticShowcaseAsset(cleanVal)) {
            urls.add(cleanVal);
          }
        } catch (_) {}
      }
    }

    // 1. 匹配标准直链 (mp4, googlevideo, googleapis, video-downloads)
    const directRegex = /https?:\/\/[a-zA-Z0-9_.-]+(?:google[a-zA-Z0-9_.-]*|[a-zA-Z0-9_.-]+)\/[^\s"'\<\>\\\`]+(?:\.mp4|videoplayback|storage\.googleapis\.com\/[^\s"'\<\>\\\`]+|video-downloads[^\s"'\<\>\\\`]+)[^\s"'\<\>\\\`]*/gi;
    let match;
    while ((match = directRegex.exec(cleanText)) !== null) {
      let cleanUrl = match[0].replace(/&amp;/g, "&");
      if (!isStaticShowcaseAsset(cleanUrl)) {
        urls.add(cleanUrl);
      }
    }

    // 2. 匹配 media.getMediaUrlRedirect 直链
    const redirectRegex = /https?:\/\/labs\.google\/fx\/api\/trpc\/media\.getMediaUrlRedirect[^\s"'\<\>\\\`]*/gi;
    while ((match = redirectRegex.exec(cleanText)) !== null) {
      urls.add(match[0]);
    }

    // 3. 提取 mediaName (形如 projects/.../locations/.../media/...)
    const mediaNameRegex = /(projects\/[^\s"'\<\>\`\/]+\/locations\/[^\s"'\<\>\`\/]+(?:\/publishers\/[^\s"'\<\>\`\/]+\/models\/[^\s"'\<\>\`\/]+)?\/media\/[a-zA-Z0-9_-]+)/g;
    while ((match = mediaNameRegex.exec(cleanText)) !== null) {
      const mName = match[1];
      mediaNames.add(mName);
    }

    // 4. 提取相对格式 mediaName (形如 "media/xyz")
    const shortMediaRegex = /["'](media\/[a-zA-Z0-9_-]{8,})["']/g;
    while ((match = shortMediaRegex.exec(cleanText)) !== null) {
      const mName = match[1];
      mediaNames.add(mName);
    }

    return {
      urls: Array.from(urls),
      mediaNames: Array.from(mediaNames)
    };
  }

  // 辅助函数：深度剥离 Google 防注入前缀 (兼容 BoQ )]}' 与 Next.js for(;;);)
  function cleanBoQResponseText(rawText) {
    if (typeof rawText !== "string") return "";
    return rawText
      .replace(/^for\s*\(\s*;;\s*\);/, "")
      .replace(/^\)\]\}'/, "")
      .trim();
  }

  async function parseFetchResponse(response) {
    const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
    try {
      if (contentType.includes("application/octet-stream") || contentType.includes("blob") || contentType.includes("video/") || contentType.includes("image/") || contentType.includes("audio/")) {
        return await response.blob();
      } else if (contentType.includes("form-data")) {
        return await response.formData();
      } else {
        const text = await response.text();
        const cleaned = cleanBoQResponseText(text);
        if (!cleaned) return "";
        try {
          return JSON.parse(cleaned);
        } catch {
          // Google BoQ batchexecute 响应包含长度前缀与多行帧 (如 5014\n[["wrb.fr",...]])，直接返回清洗后的文本供 readRpcFrames 逐帧解析
          return cleaned;
        }
      }
    } catch (e) {
      console.warn("[FlowProxy] 解析普通响应体提示:", e);
    }
    return null;
  }

  function getUrlString(urlObj) {
    if (typeof urlObj === "string") return urlObj;
    if (urlObj instanceof URL) return urlObj.href;
    if (urlObj instanceof Request) return urlObj.url;
    return String(urlObj ?? "");
  }

  // 只接受服务端生成响应中的 media.name；任何 URL 扫描都不能建立归属。
  function mediaIdOf(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    const name = value.trim();
    if (/^[a-zA-Z0-9_-]+$/.test(name)) return name;
    const match = name.match(/(?:^|\/)media\/([a-zA-Z0-9_-]+)$/);
    return match ? match[1] : null;
  }

  function responseMediaLists(data) {
    const lists = [];
    function visit(value, depth = 0) {
      if (depth > 12 || value == null) return;
      if (typeof value === "string") {
        try { visit(JSON.parse(cleanBoQResponseText(value)), depth + 1); } catch (_) {}
      } else if (Array.isArray(value)) {
        value.forEach(item => visit(item, depth + 1));
      } else if (typeof value === "object") {
        if (Array.isArray(value.media)) lists.push(value.media);
        // 只解包已知传输包装，不递归搜输入素材、历史记录等任意字段。
        for (const key of ["result", "data", "json", "response"]) {
          if (value[key] != null) visit(value[key], depth + 1);
        }
      }
    }
    visit(data);
    return lists;
  }

  // BoQ 响应由长度行与独立 JSON 帧组成，不能整体 JSON.parse。
  function readRpcFrames(data) {
    const frames = [];
    const seenFrameKeys = new Set();
    const visit = value => {
      if (!Array.isArray(value)) return;
      if (value[0] === "wrb.fr" && typeof value[1] === "string") {
        try {
          const payload = typeof value[2] === "string" ? JSON.parse(value[2]) : value[2];
          const key = `${value[1]}:${typeof value[2] === "string" ? value[2].length : ""}`;
          if (!seenFrameKeys.has(key)) {
            seenFrameKeys.add(key);
            frames.push({ rpc: value[1], payload, rawPayload: value[2] });
          }
        } catch (_) {}
      } else {
        value.forEach(visit);
      }
    };
    if (typeof data === "string") {
      // 优先按 BoQ 字节长度标记分割块（避免含多行文本的 JSON 块被按行切碎），失败时回退逐行解析
      const lengthBlocks = data.split(/(?:^|\r?\n)\d+\r?\n/).map(s => s.trim()).filter(Boolean);
      for (const block of lengthBlocks) {
        try {
          visit(JSON.parse(block));
        } catch (_) {
          for (const line of block.split(/\r?\n/).map(s => s.trim()).filter(Boolean)) {
            try { visit(JSON.parse(line)); } catch (__) {}
          }
        }
      }
    } else {
      visit(data);
    }
    if (frames.length > 0) {
      console.log("[FlowProxy] 🔍 readRpcFrames 提取到的 RPC 帧列表:", frames.map(f => f.rpc));
    }
    return frames;
  }

  function receiveGenerationData(data, process, isGenerateRequest) {
    const activeProcess = process || window.currentProcess;
    console.log(`[FlowProxy] ⚙️ receiveGenerationData 开始处理: isGenerateRequest=${isGenerateRequest}, process=${activeProcess ? `Row=${activeProcess.rowIndex}, Line=${activeProcess.lineIndex}, SubId=${activeProcess.submissionId}` : 'null'}`);
    if (data == null) return;

    const frames = readRpcFrames(data);
    let boundInThisResponse = false;

    const submission = frames.find(frame => frame.rpc === "eb1hJf" || (Array.isArray(frame.payload?.[2]) && Array.isArray(frame.payload?.[3])));
    if (submission && activeProcess?.submissionId && window.currentProcess === activeProcess) {
      const groups = submission.payload?.[2];
      const media = submission.payload?.[3];
      console.log(`[FlowProxy] 🎯 捕获到提交帧 [${submission.rpc}]:`, {
        submissionId: activeProcess.submissionId,
        groupsCount: Array.isArray(groups) ? groups.length : 0,
        mediaCount: Array.isArray(media) ? media.length : 0,
        groupsSample: groups?.[0],
        mediaSample: media?.[0]
      });
      // 1. 优先使用严格交叉核对
      let ids = Array.isArray(media) && Array.isArray(groups) ? media.filter(item =>
        Array.isArray(item) && typeof item[0] === "string" &&
        groups.some(group => group?.[0] === item[2] && group?.[4] === item[1] && group?.[3]?.[4] === item[0])
      ).map(item => item[0]) : [];

      // 2. 宽松交叉核对兜底：若 groups 结构微调，只要 item[0] (UUID) 出现在 groups 序列化文本中即认领
      if (!ids.length && Array.isArray(media) && Array.isArray(groups)) {
        const groupsJson = JSON.stringify(groups) || "";
        ids = media.filter(item =>
          Array.isArray(item) && typeof item[0] === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(item[0]) &&
          groupsJson.includes(item[0])
        ).map(item => item[0]);
      }

      // 3. 终极兜底：从 groups 或 media 的首项提取新生成的媒体 UUID（排除纯图片素材）
      if (!ids.length && Array.isArray(media) && media.length > 0) {
        ids = media.filter(item => {
          if (!Array.isArray(item) || typeof item[0] !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(item[0])) return false;
          const s = JSON.stringify(item) || "";
          return !s.includes("flow-content.google/image/") || s.includes("flow-content.google/video/");
        }).map(item => item[0]);
      }

      console.log(`[FlowProxy] [${submission.rpc}] 提取到的 mediaIds:`, ids);
      if (ids.length) {
        boundInThisResponse = true;
        activeProcess.mediaBound = true;
        console.log(`[FlowProxy] 🚀 [${submission.rpc}] 成功匹配媒体 ID! 派发 flow-task-media-bound: submissionId=${activeProcess.submissionId}, mediaIds=`, ids);
        window.dispatchEvent(new CustomEvent("flow-task-media-bound", { detail: { submissionId: activeProcess.submissionId, mediaIds: [...new Set(ids)], source: "submission" } }));
      } else {
        console.warn(`[FlowProxy] ⚠️ [${submission.rpc}] 帧存在，暂未直接提取到 mediaIds，将继续通过轮询帧/DOM 认领`);
      }
    }

    // 检查所有 BoQ 轮询帧 (仅在当前任务尚未绑定 mediaId 时尝试通过当前行独有文案认领，严禁使用预设前缀匹配或覆盖已有绑定)
    if (!boundInThisResponse && activeProcess?.submissionId && window.currentProcess === activeProcess && !activeProcess.mediaBound) {
      for (const frame of frames) {
        if (frame.payload) {
          const candidateItems = [];
          const scanItems = obj => {
            if (!obj || typeof obj !== "object") return;
            if (Array.isArray(obj)) {
              if (typeof obj[0] === "string" && /^[a-zA-Z0-9_-]{8,64}$/.test(obj[0]) &&
                  typeof obj[1] === "string" && /^[a-zA-Z0-9_-]{8,64}$/.test(obj[1])) {
                candidateItems.push(obj);
              } else {
                obj.forEach(scanItems);
              }
            } else {
              Object.values(obj).forEach(scanItems);
            }
          };
          scanItems(frame.payload);

          const targetLine = (activeProcess.lineText || "").trim();
          const cleanTargetLine = targetLine.replace(/\s+/g, "");
          const lineSnippet = cleanTargetLine.length >= 6 ? cleanTargetLine.substring(0, Math.min(cleanTargetLine.length, 28)) : "";

          for (const item of candidateItems) {
            const mId = item[0];
            const itemStrings = [];
            const collectStrings = v => {
              if (!v) return;
              if (typeof v === "string" && v.trim().length >= 8) itemStrings.push(v.trim());
              else if (Array.isArray(v)) v.forEach(collectStrings);
              else if (typeof v === "object") Object.values(v).forEach(collectStrings);
            };
            collectStrings(item);

            const promptMatches = lineSnippet && itemStrings.some(rpcStr => {
              const cleanRpc = rpcStr.replace(/\s+/g, "");
              return cleanRpc.includes(lineSnippet);
            });
            if (promptMatches) {
              boundInThisResponse = true;
              activeProcess.mediaBound = true;
              console.log(`[FlowProxy] 🚀 [${frame.rpc}] 通过当前行独有文案识别到任务媒体 ID! 派发 flow-task-media-bound: submissionId=${activeProcess.submissionId}, mediaId=${mId}`);
              window.dispatchEvent(new CustomEvent("flow-task-media-bound", { detail: { submissionId: activeProcess.submissionId, mediaIds: [mId], source: "poll" } }));
              break;
            }
          }
          if (boundInThisResponse) break;
        }
      }
    }

    // 从已解析的 RPC 帧 (JSON.parse 后天然消除转义) 与原始响应文本中提取带完整 CDN 签名的 flow-content.google/video/<mediaId> 视频直链
    const parsedFramesText = frames.length > 0 ? frames.map(f => JSON.stringify(f.payload || "")).join("\n") : "";
    const rawTextForScan = [parsedFramesText, typeof data === "string" ? data : JSON.stringify(data || "")].filter(Boolean).join("\n");
    if (rawTextForScan) {
      const extracted = extractMediaAssets(rawTextForScan, true);
      const dispatchedIds = new Set();
      for (const url of extracted.urls) {
        try {
          const parsed = new URL(url);
          if (parsed.hostname === "flow-content.google" && parsed.pathname.startsWith("/video/") && isCompleteSignedFlowVideoUrl(url)) {
            const mId = parsed.pathname.match(/^\/video\/([a-zA-Z0-9_-]+)/)?.[1];
            if (mId && !dispatchedIds.has(mId)) {
              dispatchedIds.add(mId);
              console.log(`[FlowProxy] 🎉 从网络响应捕获到完整签名视频直链! mediaId=${mId}, URL=${url.substring(0, 120)}`);
              window.dispatchEvent(new CustomEvent("flow-task-media-ready", { detail: { mediaId: mId, url } }));
            }
          }
        } catch (_) {}
      }
    }

    const lists = responseMediaLists(data);
    if (lists.length > 0) {
      console.log(`[FlowProxy] responseMediaLists 解析到 ${lists.length} 组数据，展开后共 ${lists.flat().length} 个媒体条目`);
    }
    if (!boundInThisResponse && isGenerateRequest && activeProcess?.submissionId && window.currentProcess === activeProcess) {
      const ids = lists.flat().map(media => mediaIdOf(media?.name)).filter(Boolean);
      if (ids.length) {
        activeProcess.mediaBound = true;
        console.log(`[FlowProxy] 🚀 [mediaLists] 派发 flow-task-media-bound: submissionId=${activeProcess.submissionId}, mediaIds=`, ids);
        window.dispatchEvent(new CustomEvent("flow-task-media-bound", {
          detail: { submissionId: activeProcess.submissionId, mediaIds: [...new Set(ids)], source: "submission" }
        }));
      } else {
        console.log(`[FlowProxy] ℹ️ 生成请求响应未直接返回 mediaLists ID (submissionId=${activeProcess.submissionId})，继续等待后续轮询帧或 DOM 视频渲染认领`);
      }
    }
    for (const media of lists.flat()) {
      const mediaId = mediaIdOf(media?.name);
      const status = media?.mediaMetadata?.mediaStatus?.mediaGenerationStatus;
      console.log(`[FlowProxy] 媒体项状态检查: name=${media?.name}, mediaId=${mediaId}, status=${status}`);
      if (!mediaId || status !== "MEDIA_GENERATION_STATUS_SUCCESSFUL") continue;
      const candidates = extractMediaAssets(JSON.stringify(media)).urls;
      const url = candidates.find(value => {
        try {
          const parsed = new URL(value);
          return parsed.hostname === "flow-content.google" && parsed.pathname === `/video/${mediaId}` && isCompleteSignedFlowVideoUrl(value);
        } catch (_) { return false; }
      }) || `https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=${encodeURIComponent(media.name)}&mediaUrlType=MEDIA_URL_TYPE_FULL_MEDIA`;
      console.log(`[FlowProxy] 🎉 发现已就绪视频! mediaId=${mediaId}, 最终选定 URL=${url}`);
      window.dispatchEvent(new CustomEvent("flow-task-media-ready", { detail: { mediaId, url } }));
    }
  }

  function classifyRequest(url, body) {
    let decoded = body || "";
    try { decoded = decodeURIComponent(decoded); } catch (_) {}
    return {
      generate: /video:batchAsyncGenerateVideo|StreamGenerateContent|L2jnw|eb1hJf/.test(url + decoded),
      status: /batchCheckAsyncVideoGenerationStatus|BatchGetMedia|Iyc41d|ListMedia|bOKtO|jwpduf|as29s|nzlxg|batchexecute/.test(url + decoded)
    };
  }

  function setupFetchHook() {
    async function getFetchBodyText(input, init) {
      if (typeof init?.body === "string") return init.body;
      if (typeof URLSearchParams !== "undefined" && init?.body instanceof URLSearchParams) return init.body.toString();
      if (typeof FormData !== "undefined" && init?.body instanceof FormData) {
        try {
          const pairs = [];
          for (const [k, v] of init.body.entries()) {
            pairs.push(`${k}=${typeof v === "string" ? v : ""}`);
          }
          return pairs.join("&");
        } catch (_) { return ""; }
      }
      if (typeof Blob !== "undefined" && init?.body instanceof Blob) {
        try { return await init.body.text(); } catch (_) { return ""; }
      }
      if (typeof Request !== "undefined" && input instanceof Request) {
        try { return await input.clone().text(); } catch (_) { return ""; }
      }
      return "";
    }

    const classify = typeof classifyRequest !== "undefined" ? classifyRequest : function(u, b) {
      let d = b || "";
      try { d = decodeURIComponent(d); } catch (_) {}
      return {
        generate: /video:batchAsyncGenerateVideo|StreamGenerateContent|L2jnw|eb1hJf/.test(u + d),
        status: /batchCheckAsyncVideoGenerationStatus|BatchGetMedia|Iyc41d|ListMedia|bOKtO|jwpduf|as29s|batchexecute/.test(u + d)
      };
    };

    const originalFetch = window.fetch;
    window.fetch = function(input, init) {
      const url = getUrlString(input);
      const process = window.currentProcess;
      const isFlowInternal = /labs\.google|_\/LabsFlowUi|_\/AiSandboxAngularFrontend|FlowService|batchexecute|flow-content\.google|trpc/i.test(url);
      if (process && classify(url, "").generate) {
        process.generateRequestSent = true;
      }
      const bodyPromise = getFetchBodyText(input, init);
      void bodyPromise.then(text => {
        const type = classify(url, text);
        if (type.generate && process) {
          process.generateRequestSent = true;
        }
      }).catch(() => {});
      const result = originalFetch.apply(this, arguments);
      result.then(response => {
        const copy = response.clone();
        void bodyPromise.then(async text => {
          const type = classify(url, text);
          if (isFlowInternal || type.generate || type.status) {
            console.log(`[FlowProxy] 🌐 捕获网络请求 (Fetch): URL=${url.substring(0, 120)}, generate=${type.generate}, status=${type.status}, activeProcess=${process?.submissionId || '无'}`);
          }
          if (!type.generate && !type.status) return;
          if (response.ok === false) {
            console.warn(`[FlowProxy] ⚠️ 拦截到的请求响应非 OK (${response.status}): URL=${url.substring(0, 120)}`);
            return;
          }
          console.log(`[FlowProxy] 📥 开始解析请求响应: URL=${url.substring(0, 120)}`);
          const data = await parseFetchResponse(copy);
          receiveGenerationData(data, process, type.generate);
        }).catch(err => console.warn("[FlowProxy] ID 响应解析失败", err));
      }).catch(() => {});
      return result;
    };
  }

  function getXhrBodyText(body) {
    if (typeof body === "string") return body;
    if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) return body.toString();
    if (typeof FormData !== "undefined" && body instanceof FormData) {
      try {
        const pairs = [];
        for (const [k, v] of body.entries()) {
          pairs.push(`${k}=${typeof v === "string" ? v : ""}`);
        }
        return pairs.join("&");
      } catch (_) {}
    }
    return "";
  }

  // Hook XMLHttpRequest
  function setupXhrHook() {
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
      this._flowUrl = getUrlString(url);
      return open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
      const process = window.currentProcess;
      const bodyText = getXhrBodyText(body);
      const isFlowInternal = /labs\.google|_\/LabsFlowUi|FlowService|batchexecute|flow-content\.google|trpc/i.test(this._flowUrl || "");
      const type = classifyRequest(this._flowUrl || "", bodyText);
      if (type.generate && process) {
        process.generateRequestSent = true;
      }
      if (isFlowInternal || type.generate || type.status) {
        console.log(`[FlowProxy] 🌐 捕获网络请求 (XHR): URL=${(this._flowUrl || "").substring(0, 120)}, generate=${type.generate}, status=${type.status}, activeProcess=${process?.submissionId || '无'}`);
      }
      if (type.generate || type.status) this.addEventListener("load", function() {
        if (this.status < 200 || this.status >= 300) {
          console.warn(`[FlowProxy] ⚠️ XHR 响应非成功状态 (${this.status}): URL=${(this._flowUrl || "").substring(0, 120)}`);
          return;
        }
        try {
          console.log(`[FlowProxy] 📥 开始解析 XHR 响应: URL=${(this._flowUrl || "").substring(0, 120)}`);
          const data = this.responseType === "json" ? this.response : cleanBoQResponseText(this.responseText || "");
          receiveGenerationData(data, process, type.generate);
        } catch (err) { console.warn("[FlowProxy] XHR ID 解析失败", err); }
      }, { once: true });
      return send.apply(this, arguments);
    };
  }

  // ----------------------------------------------------
  // 3. Hook webpackChunk_N_E 窃取 React 模块
  // ----------------------------------------------------
  function hookModuleFactory(chunk, index) {
    if (window.modulefactory) return;
    const firstKey = Object.keys(chunk)[0];
    const originalFactory = chunk[firstKey];
    console.log("[FlowProxy] 发现 Webpack 模块加载，开始挂载 modulefactory...");
    chunk[firstKey] = function() {
      window.modulefactory = arguments[2]; // 保存 Webpack module map
      originalFactory.apply(this, arguments);
    };
  }

  const moduleState = { Modules: [] };

  function refreshModules() {
    if (!window.modulefactory) return;
    console.log("[FlowProxy] 刷新模块缓存中...");
    
    moduleState.Modules = Object.keys(window.modulefactory.m).map(id => {
      let moduleExports = null;
      try {
        moduleExports = window.modulefactory(id);
      } catch {}
      return moduleExports;
    });

    const findModule = (predicate) => {
      const results = [];
      moduleState.Modules.forEach(mod => {
        if (typeof mod !== "undefined") {
          if (typeof predicate === "string") {
            if (typeof mod.default === "object") {
              for (const k in mod.default) if (k === predicate) results.push(mod);
            }
            for (const k in mod) if (k === predicate) results.push(mod);
          } else if (typeof predicate === "function") {
            if (predicate(mod)) results.push(mod);
          }
        }
      });
      return results;
    };

    const getModule = (predicate) => {
      const results = [];
      moduleState.Modules.forEach(mod => {
        if (typeof mod !== "undefined") {
          if (typeof predicate === "string") {
            if (typeof mod.default === "object") {
              for (const k in mod.default) if (k === predicate) results.push(mod);
            }
            for (const k in mod) if (k === predicate) results.push(mod);
          } else if (typeof predicate === "function") {
            const mapped = predicate(mod);
            if (mapped) results.push(mapped);
          }
        }
      });
      return results;
    };

    const findFunction = (query) => {
      if (moduleState.Modules.length === 0) throw Error("无模块缓存");
      const results = [];
      if (typeof query === "string") {
        moduleState.Modules.forEach(mod => {
          if (mod.toString().includes(query)) results.push(mod);
        });
      } else if (typeof query === "function") {
        moduleState.Modules.forEach(mod => {
          if (query(mod)) results.push(mod);
        });
      }
      return results;
    };

    window.moduleManager = {
      modules: moduleState.Modules,
      findModule,
      getModule,
      findFunction,
      get: window.modulefactory ? (id => window.modulefactory(id)) : null
    };
  }

  function setupWebpackHook() {
    if (window.webpackChunk_N_E) {
      console.log("[FlowProxy] webpackChunk_N_E 已经存在，将无法自动 hook chunk 加载");
    } else {
      window.webpackChunk_N_E = [];
      const originalPush = window.webpackChunk_N_E.push;
      window.fbmodules = {};
      const pendingModules = {};
      window.replaceIds = [];
      
      const storePredicate = (storeKey) => (mod) => {
        if (typeof mod === "object") {
          for (const k of Object.keys(mod)) {
            try {
              const keys = Object.keys(mod[k]);
              if (keys.includes("getState") && keys.includes("setState") && storeKey in mod[k].getState()) {
                return mod[k];
              }
            } catch {}
          }
        }
      };

      // 暴露全局 require，模拟 CJS 加载核心 React Store
      window.require = function(moduleName) {
        if (!fbmodules[moduleName]) {
          if (!window.moduleManager) return {};
          let targetModule;
          if (["workflows", "projectDetails", "collections"].includes(moduleName)) {
            targetModule = window.moduleManager.getModule(storePredicate(moduleName));
          } else {
            // 如需获取 react, react-dom 等模块
            if (moduleName === "react") {
              targetModule = window.moduleManager.findModule(mod => mod && mod.createElement);
            } else if (moduleName === "react-dom") {
              targetModule = window.moduleManager.findModule(mod => mod && mod.hydrateRoot && mod.render);
            }
          }
          if (!targetModule?.[0]) return {};
          fbmodules[moduleName] = targetModule?.[0];
        }
        return fbmodules[moduleName];
      };

      let pushCount = 0;
      const registeredPushes = { 0: originalPush };
      let refreshTimeout;

      Object.defineProperty(webpackChunk_N_E, "push", {
        get: () => {
          if (pushCount > 0) {
            const currentIdx = pushCount;
            return function() {
              // 触发模块 hook
              const chunk = arguments[0][1];
              replaceIds.forEach(id => {
                if (pendingModules[id]) {
                  pendingModules[id](chunk, fbmodules);
                } else {
                  window.replaceIds = window.replaceIds.filter(x => x !== id);
                }
              });

              clearTimeout(refreshTimeout);
              refreshTimeout = setTimeout(() => {
                refreshModules();
              }, 1000);

              return registeredPushes[currentIdx].apply(webpackChunk_N_E, arguments);
            };
          }
          return originalPush;
        },
        set: val => {
          pushCount++;
          registeredPushes[pushCount] = val;
        }
      });

      // 注册初始 Hook
      const initialId = "hook-initial";
      pendingModules[initialId] = hookModuleFactory;
      window.replaceIds.push(initialId);
    }
  }

  // ----------------------------------------------------
  // 4. 执行初始化 Hook
  // ----------------------------------------------------
  setupFetchHook();
  setupXhrHook();
  setupWebpackHook();
  
  console.log("[FlowProxy] 代理拦截已就绪。");
})();
