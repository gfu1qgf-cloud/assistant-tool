"""Explicitly trusted user code, executed outside Qt/the model process."""
import copy
from dataclasses import fields, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .contracts import NodeDefinition, NodeParameter, NodePort
from .python_values import MAX_DATA_BYTES, validate_data

TYPES = ('text', 'integer', 'float', 'number', 'boolean', 'list', 'mapping', 'data', 'color',
         'video', 'audio', 'subtitles', 'directory', 'files', 'resource_pool', 'operations', 'render_plan')
MAX_PORTS = 32
MAX_WIRE = 16 * 1024 * 1024
DEFAULT_INPUTS = [{'id': 'in_value', 'name': 'value', 'title': '输入值', 'type': 'number', 'required': True, 'default': None}]
DEFAULT_OUTPUTS = [{'id': 'out_value', 'name': 'value', 'title': '输出值', 'type': 'number', 'required': True}]


def port_specs(params, direction):
    values = params.get(direction, DEFAULT_INPUTS if direction == 'inputs' else DEFAULT_OUTPUTS)
    if not isinstance(values, list) or len(values) > MAX_PORTS:
        raise ValueError('Python 每侧端口最多32个。')
    ids, names = set(), set()
    for value in values:
        if (not isinstance(value, dict) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}', str(value.get('id', '')))
                or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}', str(value.get('name', '')))
                or not isinstance(value.get('title'), str) or not 1 <= len(value['title']) <= 100
                or value.get('type') not in TYPES or type(value.get('required')) is not bool
                or value['id'] in ids or value['name'] in names):
            raise ValueError('端口ID／代码名必须为唯一英文标识符；标题、类型与必填状态必须有效。')
        ids.add(value['id']); names.add(value['name'])
        if direction == 'inputs' and value.get('default') is not None:
            validate_data(value['default'])
    return copy.deepcopy(values)


def ports(params, direction):
    return tuple(NodePort(p['id'], p['title'] + ' · ' + p['name'], p['type'], required=p['required'])
                 for p in port_specs(params, direction))


def signature(params):
    value = {'code': params.get('code', ''), 'inputs': port_specs(params, 'inputs'), 'outputs': port_specs(params, 'outputs')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def validate_script(params):
    port_specs(params, 'inputs'); port_specs(params, 'outputs')
    for key, default, low, high in (('timeout', 30, 1, 600), ('memory', 512, 64, 32768)):
        value = params.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Python节点超时／内存限制无效。')
    code = params.get('code', '')
    if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > 256000:
        raise ValueError('Python 代码不能为空，最多256KB。')
    try:
        compile(code, '<节点代码>', 'exec')
    except SyntaxError as error:
        raise ValueError(f'Python 第{error.lineno}行语法错误：{error.msg}') from None


def prepare_code(_task, params, _services):
    validate_script(params)
    if params.get('approved') != signature(params):
        raise ValueError('Python代码尚未授权或已修改。请在节点面板阅读并勾选“我已阅读，允许运行本地代码”。')
    return {'signature': signature(params)}


def public_classes():
    from . import contracts
    from .video_stream import VideoStream, EncodingSettings
    from .python_values import PythonValue
    names = ('ColorValue', 'ResourceRef', 'DirectoryRef', 'FileList', 'ResourcePool', 'VideoSection',
             'VideoComposition', 'VideoTimeline', 'SubtitleCue', 'SubtitleTrack', 'FilterOperation', 'RenderPlan')
    return {**{name: getattr(contracts, name) for name in names}, 'VideoStream': VideoStream, 'EncodingSettings': EncodingSettings, 'PythonValue': PythonValue}


def encode(value, depth=0):
    if depth > 32:
        raise ValueError('Python节点数据嵌套超过32层。')
    if value is None or type(value) in (str, bool, int, float):
        validate_data(value)
        return value
    if type(value) in (tuple, list):
        return {'tag': 'tuple' if type(value) is tuple else 'list', 'items': [encode(item, depth+1) for item in value]}
    if type(value) is dict and all(type(key) is str for key in value):
        return {'tag': 'dict', 'items': {key: encode(item, depth+1) for key, item in value.items()}}
    if is_dataclass(value) and type(value) in public_classes().values():
        return {'tag': type(value).__name__, 'items': {field.name: encode(getattr(value, field.name), depth+1) for field in fields(value)}}
    raise TypeError('Python节点只传公共数据／媒体描述，不接受Qt对象、函数或裸文件句柄：' + type(value).__name__)


def decode(value, depth=0):
    if depth > 32:
        raise ValueError('Python节点返回数据嵌套过深。')
    if value is None or type(value) in (str, bool, int, float):
        validate_data(value)
        return value
    if not isinstance(value, dict) or set(value) != {'tag', 'items'}:
        raise TypeError('Python进程返回的公共数据格式无效。')
    tag, items = value['tag'], value['items']
    if tag in {'tuple', 'list'} and type(items) is list:
        result = [decode(item, depth+1) for item in items]
        return tuple(result) if tag == 'tuple' else result
    if type(items) is not dict:
        raise TypeError('Python描述必须包含字段字典。')
    values = {key: decode(item, depth+1) for key, item in items.items()}
    if tag == 'dict':
        return values
    definition = public_classes().get(tag)
    if definition is None or set(values) != {field.name for field in fields(definition)}:
        raise TypeError('Python节点返回了未知的对象类型或字段。')
    return definition(**values)


def check_value(value, dtype, label):
    from .contracts import ColorValue, ResourceRef, DirectoryRef, FileList, ResourcePool, SubtitleTrack, VideoTimeline, VideoComposition, FilterOperation, RenderPlan
    from .video_stream import VideoStream
    from .python_values import PythonValue
    valid = (value is None
        or dtype == 'text' and type(value) is str
        or dtype == 'integer' and type(value) is int
        or dtype == 'float' and type(value) is float and math.isfinite(value)
        or dtype == 'number' and type(value) in (int, float) and math.isfinite(value)
        or dtype == 'boolean' and type(value) is bool
        or dtype == 'list' and type(value) is list
        or dtype == 'mapping' and type(value) is dict
        or dtype == 'data' and isinstance(value, PythonValue)
        or dtype == 'color' and isinstance(value, ColorValue)
        or dtype in {'video', 'audio', 'subtitles'} and isinstance(value, ResourceRef) and value.data_type == dtype
        or dtype == 'video' and isinstance(value, (VideoStream, VideoTimeline, VideoComposition))
        or dtype == 'subtitles' and isinstance(value, SubtitleTrack)
        or dtype == 'directory' and isinstance(value, DirectoryRef)
        or dtype == 'files' and isinstance(value, FileList)
        or dtype == 'resource_pool' and isinstance(value, ResourcePool)
        or dtype == 'operations' and isinstance(value, tuple) and all(isinstance(item, FilterOperation) for item in value)
        or dtype == 'render_plan' and isinstance(value, RenderPlan))
    if not valid:
        raise TypeError(f'端口“{label}”需要 {dtype}，实际为 {type(value).__name__}。')
    if dtype in {'list', 'mapping'} and value is not None:
        validate_data(value)
    return value


def _kill(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_code(inputs, params, context):
    prepare_code(None, params, None)
    values = {}
    for spec in port_specs(params, 'inputs'):
        value = inputs.get(spec['id'])
        if value is None:
            value = copy.deepcopy(spec.get('default'))
            if value is None and spec['required']:
                raise ValueError('必须输入端没有值：' + spec['name'])
        values[spec['name']] = check_value(value, spec['type'], spec['name'])
    from model.OfflineMode import is_offline
    request = json.dumps({'code': params['code'], 'inputs': encode(values), 'offline': is_offline()}, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(request) > MAX_WIRE:
        raise ValueError('Python节点输入超过16MB，未截断。')
    args = [sys.executable, '--node-python-worker'] if getattr(sys, 'frozen', False) else [sys.executable, '-I', '-B', str(Path(__file__).with_name('code_worker.py'))]
    # Do not inject the host's secrets into scripts via inherited environment.
    allowed_environment = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC', 'SYSTEMDRIVE', 'TEMP', 'TMP',
        'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL', 'PYTHONUTF8'}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed_environment}
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
    from .code_process import ProcessJob
    try:
        job = ProcessJob(process, params.get('memory', 512))
    except Exception:
        _kill(process); process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr): pipe.close()
        raise RuntimeError('无法建立Python节点的进程生命周期隔离，本次未发送代码。') from None
    buffers, failures, overflow = [bytearray(), bytearray()], [], threading.Event()
    def read(pipe, index, maximum):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                if len(buffers[index]) + len(chunk) > maximum:
                    overflow.set(); _kill(process); break
                buffers[index].extend(chunk)
        except OSError as error:
            failures.append(error)
    def write():
        try:
            process.stdin.write(request); process.stdin.close()
        except (OSError, BrokenPipeError) as error:
            failures.append(error)
    threads = [threading.Thread(target=read, args=(process.stdout, 0, MAX_WIRE), daemon=True),
               threading.Thread(target=read, args=(process.stderr, 1, 65536), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + params.get('timeout', 30)
    try:
        while process.poll() is None:
            context.check_canceled()
            if time.monotonic() > deadline:
                raise TimeoutError('Python节点运行超时，已停止独立进程。')
            time.sleep(.03)
        job.close()  # Also close script-created children holding inherited pipes.
        for thread in threads:
            thread.join(1)
        context.check_canceled()
        if overflow.is_set():
            raise ValueError('Python进程返回超过16MB或错误输出超过64KB，已停止；未截断成成功结果。')
        if process.returncode != 0:
            raise RuntimeError('Python独立进程异常退出：' + str(process.returncode) + '\n' + buffers[1].decode('utf-8', 'replace')[-2000:])
        if failures:
            raise RuntimeError('Python独立进程通信失败：' + str(failures[0]))
        reply = json.loads(buffers[0].decode('utf-8'))
        if reply.get('log'):
            context.report('Python节点输出：\n' + reply['log'], None)
        if reply.get('error'):
            raise RuntimeError(reply['error'])
        outputs = decode(reply['outputs'])
        if type(outputs) is not dict:
            raise TypeError('outputs 必须是字典。')
        declared = port_specs(params, 'outputs')
        if set(outputs) - {spec['name'] for spec in declared}:
            raise ValueError('Python返回了未声明的输出端：' + ', '.join(sorted(set(outputs) - {s['name'] for s in declared})))
        result = {}
        for spec in declared:
            value = outputs.get(spec['name'])
            if value is None and spec['required']:
                raise ValueError('Python未返回必填输出端：' + spec['name'])
            result[spec['id']] = check_value(value, spec['type'], spec['name'])
        return result
    finally:
        job.close()
        _kill(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        for thread in threads:
            thread.join(1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if not pipe.closed:
                pipe.close()


def clear_code_approvals(graph):
    """Call at external-import boundaries, not when reopening a local library."""
    for node in graph.get('nodes', []):
        if node.get('kind') == 'core.python_code':
            node['params']['approved'] = ''
        nested = node.get('params', {}).get('graph')
        if isinstance(nested, dict):
            clear_code_approvals(nested)


def register_code_node(registry):
    registry.register('core', NodeDefinition('core.python_code', 'Python · 自定义代码', category='Python 原生/自定义代码',
        parameters=(NodeParameter('code', '代码', 'object', 'outputs["value"] = inputs["value"]\n'),
            NodeParameter('inputs', '输入端定义', 'object', DEFAULT_INPUTS), NodeParameter('outputs', '输出端定义', 'object', DEFAULT_OUTPUTS),
            NodeParameter('timeout', '最长执行秒数', 'integer', 30, 1, 600),
            NodeParameter('memory', '进程组内存上限(MB)', 'integer', 512, 64, 32768),
            NodeParameter('approved', '本地授权指纹', default='', required=False)),
        input_factory=lambda p: ports(p, 'inputs'), output_factory=lambda p: ports(p, 'outputs'),
        build=run_code, prepare=prepare_code, validate_parameters=validate_script, execution='compute',
        description='inputs／outputs 使用代码名作为字典键。端口ID稳定，改标签不丢线。'
            '独立进程提供超时／取消／崩溃隔离，但不是安全沙箱：代码拥有本机用户权限，不要授权来源不明的代码。'
            '不传主程序凭据、Qt对象或裸文件句柄；媒体为公共描述对象。导入的代码不会自动授权。'))
