"""Build only the pinned, reviewed extension into the public application layout.

Never copy the active browser_extensions tree or the whole repository: a local
extension tree can be stale and can contain unrelated settings or files.
"""
import ast
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile


BRIDGE_FILES = frozenset({
    'background.js', 'content-isolated.js', 'lzx-background.js', 'lzx-content.js',
    'lzx-options.js', 'lzx-page.js', 'lzx-reference.js', 'manifest.json',
    'options.html', 'SOURCE.json', 'UPSTREAM_LICENSE', 'UPSTREAM_LICENSE.LIST',
    'injects/flow-proxy.js', 'injects/ui.css', 'injects/ui.js',
})


def _expected_version(root):
    source = root / 'app_plugins/builtin/flow_assistant/setup_support.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == 'EXTENSION_VERSION' for target in node.targets):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and version:
                return version
    raise ValueError('未找到主程序要求的 Flow 扩展版本。')


def validate_bridge(directory, version):
    directory = Path(directory)
    actual = set()
    total = 0
    for path in directory.rglob('*'):
        stat = path.lstat()
        if path.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Flow 发行资源不能使用链接或重解析点。')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
            if not stat.st_size:
                raise ValueError('Flow 发行资源为空：' + path.name)
            total += stat.st_size
    if actual != BRIDGE_FILES:
        raise ValueError('Flow 发行资源缺失或含未声明文件：' + ', '.join(sorted(actual ^ BRIDGE_FILES)))
    if total > 128 * 1024 * 1024:
        raise ValueError('Flow 发行资源超过大小上限。')
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    provenance = json.loads((directory / 'SOURCE.json').read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('manifest_version') != 3
            or manifest.get('version') != version or not isinstance(provenance, dict)
            or provenance.get('bridge_version') != version or provenance.get('protocol') != 1):
        raise ValueError('Flow 主程序、扩展与来源清单版本不一致。')
    references = [manifest.get('background', {}).get('service_worker'), manifest.get('options_page')]
    for script in manifest.get('content_scripts', []):
        references.extend(script.get('js', []))
        references.extend(script.get('css', []))
    references.extend(manifest.get('icons', {}).values())
    action = manifest.get('action', {})
    icon = action.get('default_icon', {})
    references.extend(icon.values() if isinstance(icon, dict) else [icon])
    if action.get('default_popup'):
        references.append(action['default_popup'])
    for resources in manifest.get('web_accessible_resources', []):
        references.extend(resources.get('resources', []))
    for name in references:
        if name is None:
            continue
        if (not isinstance(name, str) or '\\' in name or ':' in name
                or any(part in {'', '.', '..'} for part in name.split('/'))
                or name not in actual):
            raise ValueError('Flow manifest 引用了不安全或未声明的资源：' + str(name))
    return actual


def stage_flow_bridge(project_root, application_directory):
    root, application = Path(project_root), Path(application_directory)
    destination = application / 'browser_extensions/flow_bridge'
    if not application.is_dir():
        raise ValueError('应先完成程序目录收集，再添加 Flow 发行资源。')
    if destination.exists():
        raise ValueError('Flow 发行目标已存在，未混入或覆盖旧扩展文件。')
    expected = _expected_version(root)
    spec = importlib.util.spec_from_file_location('flow_release_builder', root / 'tools/build_flow_bridge.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    if builder.BRIDGE_VERSION != expected:
        raise ValueError('Flow 主程序与扩展构建器版本不一致。')
    with tempfile.TemporaryDirectory(prefix='lzx-flow-release-') as name:
        built = Path(name) / 'flow_bridge'
        builder.build(root, destination=built)
        validate_bridge(built, expected)
        # Publish only the complete, validated build. No source tools, private
        # distribution material, active extension files or config are copied.
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(built, destination)
    return destination
