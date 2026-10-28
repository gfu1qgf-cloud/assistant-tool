"""Assemble a local bridge from immutable FlowCue originals and audited seams."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PIN = 'a5a4a3607e55c85c9c0c2e8e4700f2140eefb36d'
UPSTREAM_VERSION = '1.1.6'
BRIDGE_VERSION = '1.1.59'
KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnvFDGkX5m/XE6cRqK89rmU6CkCtcOue7G3zgttpk45gF21Y9SQi4L6IvLh4SgLIPcU3Gm5MsXjB4UqmC03ssHqI9Q5SG4TqkGpS+LunJanIVy9Q7ak3IevqA1A3QgA28iAbMHsX9a8sEL/qhi13afdr7n42eF6A4XzJs3+9An6pjzAPNjK4AJLF7PytIzY9YxJ4eg7ANPQUPVTC233hvW5OGMXOxdo4QX1/qGe1npm6E8wwnGfp85NkRPLohy9t1cfJreXcqfswkRa1QlyeKsCfTIKQX2vclCDavsoddro3w98YS4SRb4Tz8NRnbJITsOoPqCbMtXyaWP2QIyy/S6QIDAQAB'
HASHES = {
    'background.js': '756e503f5612b4f1218ea5b7f87ee7a3b17fdcd36009058d7442867df5b87139',
    'content-isolated.js': '7688010740c451e349d95d5bf9bde45c2cbfefe6e50e6e73ac255482936c1c5c',
    'injects/flow-proxy.js': '2d9a170f412a6d623f60a699265b1758b2e008c3338ba650855f87d272b4c4c1',
    'injects/ui.js': '446071a45886b7a2a9514d7cb3d2fe7f1bdad1f80b86684b2954f20e02f0bf2b',
    'manifest.json': 'ff6f81d8ebddafbda818735270d807c4f14518d5a7f63aebbdba03f064ea6394',
}


def build(root=ROOT, *, destination=None):
    source = root / 'third_party/flowcue/extension'
    destination = Path(destination) if destination is not None else root / 'browser_extensions/flow_bridge'
    for name, expected in HASHES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError('上游源码校验不通过：' + name)
    spec = importlib.util.spec_from_file_location('flow_bridge_patches', Path(__file__).with_name('flow_bridge_patches.py'))
    patches = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patches)
    ui, proxy, background = patches.patch_upstream(source)
    guard_spec = importlib.util.spec_from_file_location('flow_transfer_guard_patches', Path(__file__).with_name('flow_transfer_guard_patches.py'))
    guards = importlib.util.module_from_spec(guard_spec)
    guard_spec.loader.exec_module(guards)
    ui, background = guards.patch_ui(ui), guards.patch_background(background,
        (root / 'browser_extensions/flow_adapter/download_guard.js').read_text('utf-8'))
    pack_spec = importlib.util.spec_from_file_location('flow_credit_pack_patches', Path(__file__).with_name('flow_credit_pack_patches.py'))
    pack_patches = importlib.util.module_from_spec(pack_spec)
    pack_spec.loader.exec_module(pack_patches)
    ui = pack_patches.patch_ui(ui)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    for name, content in {'injects/ui.js': ui, 'injects/flow-proxy.js': proxy, 'background.js': background}.items():
        (destination / name).write_text(content, encoding='utf-8')
    (destination / 'content-isolated.js').write_text(guards.patch_isolated(patches.patch_isolated(
        (source / 'content-isolated.js').read_text('utf-8'))), encoding='utf-8')
    adapter = root / 'browser_extensions/flow_adapter'
    for original, target in {'background.js': 'lzx-background.js', 'page.js': 'lzx-page.js',
                             'content.js': 'lzx-content.js', 'reference.js': 'lzx-reference.js', 'options.js': 'lzx-options.js', 'options.html': 'options.html'}.items():
        shutil.copy2(adapter / original, destination / target)
    (destination / 'lzx-background.js').write_text((adapter / 'visual.js').read_text('utf-8') + '\n' +
        (adapter / 'background.js').read_text('utf-8'),encoding='utf-8')
    manifest = json.loads((source / 'manifest.json').read_text('utf-8'))
    manifest.update(name='Flow 视频助手 · 辅助工具桥接', version=BRIDGE_VERSION, minimum_chrome_version='120', key=KEY,
                    options_page='options.html', description=f'基于 FlowCue {UPSTREAM_VERSION} 的辅助工具桥接：任务调度、自动下载与任务目录归档。')
    manifest['permissions'].append('alarms')
    manifest['permissions'].append('scripting')
    manifest['host_permissions'].append('http://127.0.0.1/*')
    manifest['background']['service_worker'] = 'lzx-background.js'
    for script in manifest['content_scripts']:
        if script.get('world') == 'MAIN':
            script['js'][0:0] = ['lzx-reference.js', 'lzx-page.js']
        elif script.get('world') == 'ISOLATED':
            bootstrap = {'matches': list(script['matches']), 'js': ['lzx-content.js'],
                'run_at': 'document_start', 'world': 'ISOLATED', 'all_frames': True}
    manifest['content_scripts'].append(bootstrap)
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    for name in ('LICENSE', 'LICENSE.LIST'):
        shutil.copy2(source.parent / name, destination / ('UPSTREAM_' + name))
    (destination / 'SOURCE.json').write_text(json.dumps({
        'repository': 'https://github.com/dedeyuyu/FlowCue', 'commit': PIN,
        'upstream_version': UPSTREAM_VERSION, 'bridge_version': BRIDGE_VERSION, 'protocol': 1,
        'patch_module': 'tools/flow_bridge_patches.py',
        'patches': [
            'optional confirmed-progress UI lease renewal on the same token; serialized desktop expiry and stop acknowledgement include in-flight native input before handoff',
            'read-only loaded project-video catalog with bounded thumbnail previews; explicit desktop user selection creates a separate download-only binding without altering generation receipts or page ownership',
            'explicit pasted video detail URLs select the exact existing tab without moving the generation owner; bounded authenticated exact-ID lookup also works without a rendered result tile',
            'resolve lookup RPC in original page context with a bounded request; exact-ID signed media outranks lookup anchors and unresolved RPC URLs never enter Chrome downloads',
            'session-persistent per-task rejected video URL quarantine; validate completed historical download metadata before success/reuse',
            'durable account/project/SHA-256 upload intents; verification-only recovery after uncertain drag and exact-ID reuse across document handoff',
            'bounded MP4 response probe before managed download; stale content-script retirement and live-reply precedence',
            'bounded exact-generation-ID lazy video preview wake during download-only retry; optional desktop UI lease, no paid replay',
            'same-image credit packs: append one receipt-bound submission at a time; independent multi-task snapshots/downloads, no cloud wait between sends',
            'stable SHA-256-only upload filenames; image-only historical reuse with independent internal attempts, cooperative unpaid reset cancellation and phase receipts',
            'passive account-panel credit observations; manually used windows never receive automatic probe, focus, repair or closure without Flow work',
            'permanent SHA-256 image filenames with independent attempt tokens; legacy filename compatibility negotiated from the live page',
            'optional desktop FIFO foreground leases around UI phases only; early ready checks and immediate release before cloud generation/download waits',
            'close account panels and wait for upload progress; recover exact unique-name media identities and retain successful uploads across mounting retries',
            'visible website alert classification excludes own hidden alarm, prompt editors and hidden nodes; managed safety events carry exact-project page evidence',
            'one-time authenticated exact-window Windows foreground handoff; document-pinned title cleanup and bounded failure fallback',
            'idle skipped-backup rotation and authenticated native zero-window proofs; queued tasks and paid results retained',
            'durable bounded automatic browser reservations; live low-credit close checks and final native-close acknowledgement',
            'reuse SHA-256-proven reference assets per page/account/project with new per-attempt proofs and explicit reset invalidation',
            'page-instance-bound verified preflight snapshots; live settings/account/reference checks retained at submission',
            'bounded exact-ID ready-receipt reconciliation independent of video DOM rendering',
            'FlowCue 1.1.5 dormant manual mode; no managed legacy badge timer or endpoint storage; stable bridge identity',
            'frozen attempt-unique reference upload, exact media-ID first-frame checks and page-operation mutual exclusion',
            'configurable socket handshake and silence watchdogs with no page navigation or paid replay',
            'creation RPC frame isolation and single output identity; no main-batch DOM/thumbnail adoption of historical videos',
            'document-pinned missing-script bootstrap with idempotent page/native controllers and no paid replay',
            'durable exact-media terminal-failure receipts and bounded re-generation after a fresh balance check',
            'page transport seam; fresh upstream account/credits readers',
            'strict selected model checks; single paid submission attempt',
            'explicit account alerts and login redirects; durable accepted/rejected batch receipts',
            'startup reconciliation of missed terminal downloads and durable receipt acknowledgement',
            'live settings panel reacquisition and bounded selected-control stability wait',
            'duration panel reopening after rerender; verified selected seconds and scoped unsupported durations',
            'cold-start bridge health and idle-only bounded page recovery; no saved paid-batch auto restart',
            'profile-scoped low-credit close only after page idle, acknowledged receipts and all downloads complete',
            'desktop timing settings persisted in each profile and relayed without private data to page and options',
            'durable download notification and exact saved-binding download-only retry',
            'per-profile project setup; desktop-owned navigation and no extra blank tabs',
            'generation-response ownership only; no DOM/prompt/polling ownership guesses',
            'late exact-submission responses and bounded encrypted diagnostics',
            'JSON/BoQ decoding; distinct equal-length RPC frames are preserved',
            'no global Object/Webpack hook or automatic post-submit page reload in bridge',
            'short native-operation debugger cleanup; no persistent lease',
            'strict ASB URL/path binding and no flat-filename fallback for managed jobs',
            'upstream signed URL parsing, upload/download idempotency and DOM performance fixes',
            'page batch idle handoff after teardown; no final-job interval in managed batches',
            'actual page-task snapshot batch ownership; busy preflight before paid dispatch',
        ]}, ensure_ascii=False, indent=2), encoding='utf-8')
    return destination


if __name__ == '__main__':
    print(build())
