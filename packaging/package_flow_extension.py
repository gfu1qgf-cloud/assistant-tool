"""Produce a reproducible extension + corresponding-source review bundle.

Only public, explicitly declared build inputs are read. Never copy an active
Chrome profile, a working extension directory or a whole repository.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


SOURCE_FILES = (
    'third_party/flowcue/SOURCE.md', 'third_party/flowcue/LICENSE',
    'third_party/flowcue/LICENSE.LIST', 'third_party/flowcue/GPL-3.0.txt',
    'third_party/flowcue/extension/background.js',
    'third_party/flowcue/extension/content-isolated.js',
    'third_party/flowcue/extension/manifest.json',
    'third_party/flowcue/extension/injects/flow-proxy.js',
    'third_party/flowcue/extension/injects/ui.css',
    'third_party/flowcue/extension/injects/ui.js',
    'browser_extensions/flow_adapter/background.js',
    'browser_extensions/flow_adapter/content.js',
    'browser_extensions/flow_adapter/download_guard.js',
    'browser_extensions/flow_adapter/options.html',
    'browser_extensions/flow_adapter/options.js',
    'browser_extensions/flow_adapter/page.js',
    'browser_extensions/flow_adapter/page_boundary.js',
    'browser_extensions/flow_adapter/reference.js',
    'browser_extensions/flow_adapter/visual.js',
    'tools/build_flow_bridge.py', 'tools/flow_bridge_patches.py',
    'tools/flow_transfer_guard_patches.py', 'tools/flow_credit_pack_patches.py',
    'tools/flow_security_patches.py', 'packaging/flow_bridge_resources.py',
    'packaging/package_flow_extension.py',
    'app_plugins/builtin/flow_assistant/setup_support.py',
)


def public_file(root, relative):
    path = root / relative
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink() or getattr(candidate.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Public source cannot contain links: ' + relative)
    if not path.is_file() or not 0 < path.stat().st_size < 16 * 1024 * 1024:
        raise ValueError('Invalid public build input: ' + relative)
    return path.read_bytes()


def archive(path, files):
    with ZipFile(path, 'x', compression=ZIP_DEFLATED, compresslevel=9) as output:
        for name, data in sorted(files.items()):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            output.writestr(info, data)


def package(root, output, *, application=None, commit='uncommitted-review'):
    root, output = Path(root).resolve(), Path(output).absolute()
    if output.exists():
        raise ValueError('Output already exists; refusing to mix or overwrite a release.')
    helpers = module('flow_resources_for_package', root / 'packaging/flow_bridge_resources.py')
    builder = module('flow_builder_for_package', root / 'tools/build_flow_bridge.py')
    version = helpers._expected_version(root)
    if version != builder.BRIDGE_VERSION:
        raise ValueError('Host and extension versions do not match.')
    # Snapshot each public input once, then build that exact source snapshot.
    sources = {name: public_file(root, name) for name in SOURCE_FILES}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.flow-review-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        snapshot = temporary / 'source'
        for name, data in sources.items():
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        frozen_builder = module('flow_snapshot_builder', snapshot / 'tools/build_flow_bridge.py')
        built = temporary / 'bridge'
        frozen_builder.build(snapshot, destination=built)
        helpers.validate_bridge(built, version)
        files = {name: (built / name).read_bytes() for name in helpers.BRIDGE_FILES}
        if application is not None:
            delivered = Path(application) / 'browser_extensions/flow_bridge'
            helpers.validate_bridge(delivered, version)
            if any((delivered / name).read_bytes() != data for name, data in files.items()):
                raise ValueError('Application extension differs from the source build.')
        install = ('Flow 视频助手 · 桥接扩展 ' + version + '\n\n'
            '解压整个安装包，Chrome → chrome://extensions → 开发者模式 → 加载已解压的扩展，选择 FlowBridge 文件夹。\n'
            '只安装这个桥接版，不要再同时加载上游原版。主程序与扩展须使用同一发行包。\n'
            '更新前先等生成/下载完成，再关闭对应浏览器并更新扩展；不要在任务中途重新配对或刷新页面。\n'
            '本包不包含账号、Cookie、配对令牌、私人配置和任务数据。Manifest 中的 key 是固定扩展 ID 的公开密钥，不是账号凭据。\n'
            '支持自动配对，但仍需用户首次允许安装本地扩展及登录自己的 Google 账号。\n').encode('utf-8')
        review = ('Flow extension review bundle\n'
            f'Bridge: {version}\nSource label: {commit}\n'
            f'Upstream: https://github.com/dedeyuyu/FlowCue/tree/{builder.PIN}\n'
            'Original upstream files remain unmodified. Rebuild the source ZIP using Python 3.11+ and tools/build_flow_bridge.py; no pip dependencies or network required.\n'
            'Permissions: storage (settings/pairing); tabs/scripting (exact project bridge); downloads (bound result download); alarms (reconnect); debugger (bounded native image input).\n'
            'Host access: declared Google Flow/media domains plus loopback 127.0.0.1 for the desktop bridge. No remote code loader.\n'
            'Security boundary: top-frame same-extension messages; page storage allowlist excludes host credentials/journals; native image paths require a live desktop-approved grant; page cannot close all browser windows.\n'
            'Network/account safety: offline mocked regression is not proof of current paid-service behavior. No paid generation or live browser actions were performed for this audit.\n'
            'Limitations: debugger is powerful; Google-page trust and broad media hosts remain explicit risks. This is a local review, not external certification.\n'
            'Licensing: preserve upstream LICENSE (an upstream compliance report) and LICENSE.LIST; canonical GPL-3.0 text is included separately. This corresponding-source bundle is not a legal opinion or proof of all third-party rights.\n').encode('utf-8')
        readme = b'Rebuild from this directory: python tools/build_flow_bridge.py\nOutput: browser_extensions/flow_bridge\nNo network or pip dependencies required.\n'
        license_text = sources['third_party/flowcue/GPL-3.0.txt']
        bundle = temporary / 'release'
        bundle.mkdir()
        extension_name, source_name = f'FlowBridge-{version}.zip', f'FlowBridge-{version}-source.zip'
        archive(bundle / extension_name, {**{'FlowBridge/' + k: v for k, v in files.items()},
            'INSTALL.txt': install, 'REVIEW.txt': review, 'GPL-3.0.txt': license_text})
        archive(bundle / source_name, {**sources, 'REBUILD.txt': readme, 'REVIEW.txt': review})
        manifest = {'bridge_version': version, 'upstream_commit': builder.PIN, 'source_label': commit,
            'files': {k: digest(v) for k, v in sorted(files.items())},
            'source_files': {k: digest(v) for k, v in sorted(sources.items())},
            'archives': {name: digest((bundle / name).read_bytes()) for name in (extension_name, source_name)}}
        (bundle / 'FLOW_REVIEW.txt').write_bytes(review)
        (bundle / 'FLOW_SHA256.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        # Rename only our fully built new directory. Never replace existing output.
        bundle.rename(output)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--application', type=Path)
    parser.add_argument('--commit', default='uncommitted-review')
    args = parser.parse_args()
    result = package(args.root, args.output, application=args.application, commit=args.commit)
    print('Flow review bundle ready: ' + result['bridge_version'])
