"""Project output routing and collision-free final publication."""
import os
from pathlib import Path
import re

DEFAULTS = {'output_destination': 'auto', 'output_relative_root': '', 'output_project_prefix': False}


def normalized(settings):
    result = {key: settings.get(key, value) for key, value in DEFAULTS.items()}
    if not isinstance(result['output_destination'], str) or result['output_destination'] not in {'auto', 'directory', 'relative'}:
        raise ValueError('导出位置选项无效。')
    if not isinstance(result['output_relative_root'], str):
        raise ValueError('相对目录基准无效。')
    if type(result['output_project_prefix']) is not bool:
        raise ValueError('项目名文件前缀选项无效。')
    result['output_relative_root'] = result['output_relative_root'].strip()
    return result


def source_folder(job):
    return str(job.get('task_dir') or (Path(job['path']).parent if job.get('path') else ''))


def settings_for(state):
    settings = dict(state['settings'])
    settings.update(normalized(settings))
    settings['_output_project_name'] = next((p['name'] for p in state.get('profiles', [])
        if p['id'] == state.get('active_profile')), '')
    if settings['output_project_prefix'] and not settings['_output_project_name']:
        raise ValueError('无法确定当前项目名，未生成带项目名前缀的文件。')
    if settings['output_destination'] != 'relative':
        return settings
    base = settings['output_relative_root']
    if not base:
        folders = list(dict.fromkeys(os.path.abspath(value) for job in
            (state.get('jobs') or state.get('copy_pool', [])) if (value := source_folder(job))))
        if folders:
            try: base = os.path.commonpath(folders)
            except ValueError:
                raise ValueError('素材来自多个盘，请指定相对目录基准，或改用“指定文件夹”。') from None
            if base in folders:
                base = os.path.dirname(base)
    settings['_output_relative_base'] = base
    return settings


def folder(job, settings):
    mode = normalized(settings)['output_destination']
    output = str(settings.get('output_dir') or '').strip()
    if mode == 'auto':
        result = job.get('task_dir') or output or source_folder(job)
        if not result: raise ValueError('请选择输出文件夹。')
        return result
    if not output or not os.path.isabs(output):
        raise ValueError('请选择指定导出文件夹（完整路径）。')
    if mode == 'directory':
        return output
    source = source_folder(job)
    if not source:
        return output  # Pure composition has no source directory hierarchy.
    base = settings.get('_output_relative_base') or settings.get('output_relative_root') or str(Path(source).parent)
    if not os.path.isabs(base):
        raise ValueError('相对目录基准必须是完整路径。')
    try: relative = os.path.relpath(os.path.abspath(source), os.path.abspath(base))
    except ValueError:
        raise ValueError('当前任务不在相对目录基准内，请调整基准或改用“指定文件夹”。') from None
    if relative == '..' or relative.startswith('..'+os.sep) or os.path.isabs(relative):
        raise ValueError('当前任务不在相对目录基准内，未写入指定文件夹之外。')
    root = Path(output).resolve()
    target = (root/relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('导出子目录指向指定文件夹之外，请更换目录。')
    return str(target)


def output_stem(job, settings):
    from .task_naming import output_stem as task_stem
    name = task_stem(job, settings.get('task_naming_rules', {}))
    if settings.get('output_project_prefix', False):
        project = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', settings.get('_output_project_name', '')).strip(' .')
        if not project:
            raise ValueError('当前项目名不能用作文件名前缀。')
        name = project+'_'+name
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
        if not filename.lower().endswith('.mp4'): filename += '.mp4'
        if len(filename.encode('utf-16-le'))//2 > 255:
            raise ValueError('加上项目名后的文件名过长，请缩短项目名或输出名称。')
    return name


def numbered(path, index):
    path = Path(path)
    if index == 1: return path
    # Keep the naming family already recognized by task delivery.
    match = re.fullmatch(r'((?:.*_)?\d{4}年\d{1,2}月\d{1,2}日-(?:reels|口播))(\d*)', path.stem, re.I)
    stem = match[1]+str(int(match[2] or 1)+index-1) if match else path.stem+'_'+str(index)
    return path.with_name(stem+path.suffix)


class OutputNames:
    """One linear-time allocator per batch; no placeholder media files."""
    def __init__(self):
        self.claimed, self.next = set(), {}

    def choose(self, base, inputs=(), *, input_identities=None):
        from .engine import safe_output
        from .store import file_identity
        identities = input_identities if input_identities is not None else {file_identity(p) for p in dict.fromkeys(inputs)}
        base = safe_output(Path(base).parent, Path(base).name, (), input_identities=identities)
        key = file_identity(base)
        index = self.next.get(key, 1)
        while True:
            candidate = numbered(base, index)
            token = file_identity(candidate)
            index += 1
            if token in identities or token in self.claimed or candidate.exists():
                continue
            self.claimed.add(token)
            self.next[key] = index
            return candidate


def publish(prepared):
    """Move the already verified file; no-clobber is atomic on Windows."""
    from model.AppDataStorage import data_io
    from .engine import safe_output, Cancelled
    from .store import file_identity
    if prepared.overwrite:
        safe_output(prepared.output.parent, prepared.output.name, prepared.source_paths)
        data_io.replace(prepared.target, prepared.output)
        return prepared.output
    allocator = OutputNames()
    original = getattr(prepared, 'requested_output', None) or prepared.output
    requested = original if prepared.output.exists() else prepared.output
    while True:
        if prepared.cancel and prepared.cancel.is_set(): raise Cancelled('已取消，未发布临时成品。')
        identities = {file_identity(path) for path in dict.fromkeys(prepared.source_paths)}
        candidate = allocator.choose(requested, input_identities=identities)
        # Recheck sources after choosing a suffix, including changed symlinks.
        safe_output(candidate.parent, candidate.name, (), input_identities=identities)
        try:
            if os.name == 'nt':
                data_io.rename(prepared.target, candidate)
            else:
                data_io.link(prepared.target, candidate)
                data_io.unlink(prepared.target)
        except FileExistsError:
            requested = original
            continue  # Another writer won this name; preserve its result.
        if candidate != prepared.output:
            prepared.report('同名文件已保留，另存为：'+candidate.name)
        prepared.output = candidate
        return candidate
