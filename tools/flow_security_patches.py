"""Small auditable security seams; pristine vendor files remain unchanged."""
def once(text,before,after):
    if text.count(before)!=1:raise ValueError('Flow security seam changed; re-audit required')
    return text.replace(before,after)


def patch_ui(text):
    text=once(text,
        'host.endsWith("labs.google") || host.endsWith("flow.google.com") || isVidsPage()',
        'host === "labs.google" || host.endsWith(".labs.google") || '
        'host === "flow.google.com" || host.endsWith(".flow.google.com") || isVidsPage()')
    text=once(text,
        'console.log(`[FlowUI] 开始同步视频设置: 模式=${targetFootage}, 比例=${targetAspect}, 分辨率=${targetRes}, 数量=${targetCount}, 时长=${duration || \'默认\'}, 模型=${videoModel || \'默认\'}`);',
        'console.log("[FlowUI] 开始同步视频设置");')
    text=once(text,
        'console.log(`[FlowUI] 正在关联素材: Name="${imgName}", Key="${imgKey}", Mode="${footageType}"`);',
        'console.log("[FlowUI] 正在关联素材");')
    text=once(text,
        'const log = (...a) => console.log("[FlowAccount]", ...a);',
        r'const log = (...a) => { const text = a.map(value => String(value)).join(" ").replace(/\n|\r|\u2028|\u2029/g, " "); console.log("[FlowAccount] %s", text); };')
    return text


def patch_isolated(text):
    return once(text,
        'console.log("%c[FlowIsolated] 消息桥梁脚本已成功注入当前页面！URL=" + location.href, "background: #3b82f6; color: white; padding: 2px 6px; border-radius: 4px; font-weight: bold;");',
        'console.log("%c[FlowIsolated] 消息桥梁脚本已成功注入当前页面！URL=%s", "background: #3b82f6; color: white; padding: 2px 6px; border-radius: 4px; font-weight: bold;", location.href);')

def patch_background(text,guard):
    text=once(text,'chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {',
        'chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {\n'
        '  const violation=globalThis.__lzxPageBoundary.check(message,sender);\n'
        '  if(violation){sendResponse({ok:false,error:violation,dispatched:false});return;}')
    start=text.index('async function dispatchNativeFileDrag(')
    end=text.index('async function dispatchNativeClick(',start)
    drag=text[start:end]
    drag=once(drag,'  const uiGuard=globalThis.__lzxUiNativeGuard?.(tabId,uiToken);',
        '  const nativeGrant=globalThis.__lzxNativeUploadGuard?.(tabId,validFiles);\n'
        '  const heldUi=globalThis.__lzxUiNativeGuard?.(tabId,uiToken);\n'
        '  const uiGuard=nativeGrant?{check(){nativeGrant.check();heldUi?.check();},done(){heldUi?.done();}}:heldUi;')
    return guard+'\n'+text[:start]+drag+text[end:]
