"""Small auditable security seams; pristine vendor files remain unchanged."""
def once(text,before,after):
    if text.count(before)!=1:raise ValueError('Flow security seam changed; re-audit required')
    return text.replace(before,after)

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
