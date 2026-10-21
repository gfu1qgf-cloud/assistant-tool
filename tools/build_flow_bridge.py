"""Assemble a local bridge from immutable FlowCue originals and audited seams."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PIN = '847c928d60114183e42f838ba82577ba10e46d0b'
UPSTREAM_VERSION = '1.1.5'
BRIDGE_VERSION = '1.1.29'
KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnvFDGkX5m/XE6cRqK89rmU6CkCtcOue7G3zgttpk45gF21Y9SQi4L6IvLh4SgLIPcU3Gm5MsXjB4UqmC03ssHqI9Q5SG4TqkGpS+LunJanIVy9Q7ak3IevqA1A3QgA28iAbMHsX9a8sEL/qhi13afdr7n42eF6A4XzJs3+9An6pjzAPNjK4AJLF7PytIzY9YxJ4eg7ANPQUPVTC233hvW5OGMXOxdo4QX1/qGe1npm6E8wwnGfp85NkRPLohy9t1cfJreXcqfswkRa1QlyeKsCfTIKQX2vclCDavsoddro3w98YS4SRb4Tz8NRnbJITsOoPqCbMtXyaWP2QIyy/S6QIDAQAB'
HASHES = {
    'background.js': '756e503f5612b4f1218ea5b7f87ee7a3b17fdcd36009058d7442867df5b87139',
    'content-isolated.js': '7688010740c451e349d95d5bf9bde45c2cbfefe6e50e6e73ac255482936c1c5c',
    'injects/flow-proxy.js': '2d9a170f412a6d623f60a699265b1758b2e008c3338ba650855f87d272b4c4c1',
    'injects/ui.js': '420175ea88f0a907e7f908b4208b78581e378517091f7dbf8687af4c3afa5ef9',
    'manifest.json': '2dd52e476959e9ba23c9a1f09bc26f40cfd1730f73f1d8b86df8f5368e7368da',
}


def build(root=ROOT):
    source = root / 'third_party/flowcue/extension'
    destination = root / 'browser_extensions/flow_bridge'
    for name, expected in HASHES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError('上游源码校验不通过：' + name)
    spec = importlib.util.spec_from_file_location('flow_bridge_patches', Path(__file__).with_name('flow_bridge_patches.py'))
    patches = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patches)
    ui, proxy, background = patches.patch_upstream(source)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    for name, content in {'injects/ui.js': ui, 'injects/flow-proxy.js': proxy, 'background.js': background}.items():
        (destination / name).write_text(content, encoding='utf-8')
    (destination / 'content-isolated.js').write_text(patches.patch_isolated(
        (source / 'content-isolated.js').read_text('utf-8')), encoding='utf-8')
    adapter = root / 'browser_extensions/flow_adapter'
    for original, target in {'background.js': 'lzx-background.js', 'page.js': 'lzx-page.js',
                             'content.js': 'lzx-content.js', 'reference.js': 'lzx-reference.js', 'options.js': 'lzx-options.js', 'options.html': 'options.html'}.items():
        shutil.copy2(adapter / original, destination / target)
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
