"""Task delivery owns its workflow contract and final file publication."""
from pathlib import Path
import re
import subprocess
import time

from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef, RenderPlan
from model.node_workflow.workflow_types import WorkflowTypeDefinition
from model.node_workflow.video_stream import stream, unchanged_source
from model.AppDataStorage import data_file
from model.TaskDataSnapshot import TASK_DATA_FIELDS, snapshot_task_data

TYPE_ID = 'task_delivery.video_postprocess'
CONFIG_KEY = 'task_delivery_postprocess_workflow'


def filename(value):
    value = str(value).strip()
    if (not value or len(value) > 200 or value in {'.', '..'}
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', value) or value.endswith(('.', ' '))
            or value.split('.')[0].rstrip(' ').upper() in {'CON', 'PRN', 'AUX', 'NUL',
                *('COM'+str(i) for i in range(1, 10)), *('LPT'+str(i) for i in range(1, 10))}):
        raise ValueError('返回结果需要有效的文件名，不接受目录路径或Windows保留名称。')
    suffix = Path(value).suffix.casefold()
    if suffix in {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}:
        return str(Path(value).with_suffix('.mp4'))
    return value + '.mp4'


def build_input(_inputs, _params, context):
    value = getattr(context, 'workflow_input', None)
    if not isinstance(value, dict) or not {'video', 'path'} <= value.keys():
        raise ValueError('请由任务交付插件注入真实视频，或使用纯色模拟预览。')
    fields = snapshot_task_data(value.get('task')) or {}
    outputs = {'video': stream(value['video']), 'path': value['path'],
               'has_task': value.get('task') is not None,
               'review_required_set': fields.get('review_required') is not None}
    for key, _title, kind in TASK_DATA_FIELDS:
        item = fields.get(key)
        if kind == 'text':
            item = str(item) if item is not None else ''
        elif kind == 'list' and item is None:
            item = []
        elif kind == 'mapping' and item is None:
            item = {}
        outputs[key] = item
    from model.node_workflow.basic import parse_number
    try:
        outputs['task_id_number'] = parse_number(outputs['task_id'])
    except ValueError:
        outputs['task_id_number'] = None  # Non-numeric IDs must not silently become zero.
    return outputs


def build_output(inputs, _params, _context):
    # The user owns the workflow: composition and intentional replacement are
    # both allowed. Delivery identity comes from the organizer's immutable
    # source record, never from the selected output's picture provenance.
    return {'video': stream(inputs['video'])}


def migrate_graph(graph):
    """Remove the former filename wire in memory; keep saved user graphs intact."""
    if graph.get('workflow_type_version') != 1:
        return graph
    import copy
    graph = copy.deepcopy(graph)
    outputs = {node['id'] for node in graph['nodes'] if node['kind'] == 'task_delivery.video_output'}
    graph['connections'] = [edge for edge in graph['connections']
                            if not (edge[2] in outputs and edge[3] == 'filename')]
    for node in graph['nodes']:
        if node['id'] in outputs:
            node['version'] = 2
    graph['workflow_type_version'] = 2
    return graph


def example(registry):
    from model.node_workflow.graph import empty_graph, new_node
    graph = empty_graph()
    graph.update(workflow_type=TYPE_ID, workflow_type_version=2)
    kinds = ('task_delivery.video_input', 'core.compress', 'task_delivery.video_output')
    nodes = [new_node(registry.definitions[kind], pos) for kind, pos in zip(kinds, ((0, 0), (300, 0), (620, 0)))]
    a, b, d = (n['id'] for n in nodes)
    graph.update(nodes=nodes, connections=[[a, 'video', b, 'video'], [b, 'video', d, 'video']], output_node=d)
    return graph


def preview_input(context, directory, graph):
    """Tiny synthetic clip only for editor preview; never a production fallback."""
    from model.node_workflow.render import resolve_tools
    options = graph.get('simulation', {})
    if not isinstance(options, dict):
        raise ValueError('模拟预览设置无效。')
    width, height = options.get('width', 720), options.get('height', 1280)
    color = options.get('color', '#334155')
    duration = options.get('duration', 5)
    if (type(width) is not int or type(height) is not int or not 16 <= width <= 1920
            or not 16 <= height <= 1920 or width % 2 or height % 2
            or type(duration) not in (int, float) or not 1 <= duration <= 30
            or not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color)):
        raise ValueError('模拟尺寸须为16–1920偶数、时长1–30秒、颜色为#RRGGBB。')
    context.check_canceled()
    ffmpeg, _ = resolve_tools(getattr(context, 'ffmpeg', ''))
    target = Path(directory) / '模拟视频.mp4'
    process = subprocess.Popen([ffmpeg, '-n', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        f'color=c=0x{color[1:]}:s={width}x{height}:r=25:d={duration}', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        started = time.monotonic()
        while process.poll() is None:
            context.check_canceled()
            if time.monotonic() - started > 30:
                raise TimeoutError('纯色模拟创建超时。')
            time.sleep(.03)
        error = process.stderr.read(4096)
        context.check_canceled()
        if process.returncode:
            raise ValueError('纯色模拟视频创建失败：' + error.decode('utf8', 'replace')[-1000:])
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        process.stderr.close()
    from model.OdsHelper import TaskData
    task = TaskData({'task_id': '1000', 'task_name': '模拟任务（不参与上传）',
        'task_date': '2026-01-01', 'task_type': '口播', 'submission_task_type': 'FL短口播',
        'admin': '模拟管理员', 'creator': '模拟制作人',
        'task_audio_text': '模拟标题\n这是一段用于节点预览的文案。',
        'task_audio_type': '模拟语音', 'task_audio_voice_id': '模拟语音ID',
        'task_requirements': '模拟要求：20秒后叠加标识，不改原始素材。',
        'task_notes': '模拟备注，仅用于节点预览。',
        'task_video_prompt': 'A calm garden, soft light.',
        'task_resolve_requirement': '不需要（模拟原文）',
        'task_reference_link': 'https://example.com/reference', 'review_required': '需要审核',
        'subcategory': '模拟分类/子类', 'daily_stat_sheet': '模拟统计',
        'daily_stat_category': '口播', '_source_row': 2, '_raw': {'自定义列': '模拟值'}})
    return {'video': ResourceRef.capture(target, 'video'), 'path': '模拟视频.mp4',
            'task': snapshot_task_data(task)}


def register(context):
    context.register_node(NodeDefinition('task_delivery.video_input', '整理视频输入',
        outputs=(NodePort('video', '视频流', 'video'), NodePort('path', '源文件路径', 'text'),
                 NodePort('has_task', '已匹配任务信息', 'boolean'))
            + tuple(NodePort(key, title, kind) for key, title, kind in TASK_DATA_FIELDS)
            + (NodePort('task_id_number', '任务编号 · 数值', 'number'),
               NodePort('review_required_set', '表格审核标记已明确', 'boolean')),
        build=build_input, description='逐个视频注入 TaskData 独立字段端口，包含任务需求、备注、视频生成提示词、达芬奇需求原文；原始表格字段可接 Python 原生 → 字典 → 字典 · 取值 get()，按列名读取其他自定义列（包括空列）。不传递程序配置、凭据。未匹配任务时“已匹配任务信息”为否，空审核／非数字编号仍为空。需求只是输入数据，不自动改变审核、上传或达芬奇操作。预览使用明确的模拟数据。'))
    context.register_node(NodeDefinition('task_delivery.video_output', '返回后处理结果',
        inputs=(NodePort('video', '视频流', 'video'),),
        terminal_result=NodePort('video', '处理后视频流', 'video'), version=2,
        build=build_output, description='终点交回你连接的视频流，可拼接或完全替换画面，不限制必须包含原视频。没有输出端口。Python 自动以 [节点处理]源文件名 保存，审核和上传仍归属本次整理任务；不覆盖原始素材。'))
    context.register_workflow_type(WorkflowTypeDefinition(TYPE_ID, '任务交付 · 视频后处理',
        'task_delivery.video_input', 'task_delivery.video_output', example_factory=example,
        preview_factory=preview_input, version=2, migrate_graph=migrate_graph,
        description='整理完成后逐个加工视频，插件负责一次渲染、自动命名、保存和继续审核上传。'))


def prepare(config, services):
    key = str(config.get(CONFIG_KEY) or '').strip()
    if not key:
        return None, None, ()
    registry = services.available_nodes()
    graph = services.workflow_library.load(key, registry)
    if graph.get('workflow_type') != TYPE_ID:
        raise ValueError('所选工作流不是任务交付的视频后处理类型。')
    from model.node_workflow.tasks import prepare_job
    payload, resources = prepare_job(graph, registry, None, services)
    return payload, registry, resources


def process_file(source, original_path, destination, payload, registry, context, *, local=False, task_fields=None):
    from model.node_workflow.graph import evaluate_graph
    from model.node_workflow.scope import initialize_execution
    from model.node_workflow.render import render_plan, resolve_tools
    from model.node_workflow.media import MediaSession
    from model.TaskResultExporter import limit_output_filename
    task_fields = snapshot_task_data(task_fields)
    previous = {key: getattr(context, key, None) for key in ('media', 'task', 'workflow_input')}
    context.ffmpeg = resolve_tools(getattr(context, 'ffmpeg', ''))[0]
    initialize_execution(payload, context)
    context.prepared, context.notes = payload.get('prepared', {}), []
    context.processing_summary, context.generated_subtitles = {}, None
    context.failed_node = {}
    ref = ResourceRef.capture(source, 'video')
    context.workflow_input = {'video': ref, 'path': str(original_path),
                              'task': task_fields}
    try:
        with MediaSession(context, context.ffmpeg) as media:
            context.media = media
            values = evaluate_graph(payload['graph'], registry, context)
            value = stream(values['video'])
            if unchanged_source(value, ref, media):
                context.log('节点未改变视频，直接沿用原文件，不重复渲染：' + Path(original_path).name)
                ref.verify()
                return Path(source)
            source_name = Path(original_path).name
            if not source_name.startswith('[节点处理]'):
                source_name = '[节点处理]' + source_name
            name = limit_output_filename(filename(source_name), 180)
            target = Path(destination) / name
            if local and target.exists():
                index, base = 2, target
                while target.exists():
                    target = base.with_name(f'{base.stem}_{index:03}.mp4'); index += 1
                context.log('保留旧的本地后处理成品，新成品保存为：' + target.name)
            # Upload conflicts fail instead of changing two source identities.
            result = render_plan(RenderPlan(value, (), str(target)), context, context.ffmpeg)
            ref.verify()
            return Path(result['video'])
    finally:
        for key, value in previous.items():
            setattr(context, key, value)


def processor(plan, payload, registry, context, *, local=False):
    """Return a scoped processor; maps remain in the organizer, not nodes."""
    from model.TaskResultOrganizer import file_identity
    counter = [0]
    def process(source, root, _prefix):
        counter[0] += 1
        info = plan['files'].get(file_identity(source), {})
        if not local and not info.get('root'):
            raise ValueError('后处理记录缺少原结果目录，不能安全保留成品；未处理或上传。')
        original = Path(info.get('original') or source)
        relative = Path(source).relative_to(root)
        if local:
            destination = Path(info.get('root') or root).parent / '后处理结果' / relative.parent
        else:
            # This stable sibling preserves relative paths for upload routing.
            destination = Path(root).with_name(Path(root).name + '-processed') / relative.parent
        context.report(f'后处理 {counter[0]}：{original.name}', 0)
        output = process_file(source, original, destination, payload, registry, context,
                              local=local, task_fields=info.get('task'))
        if output.resolve() == Path(source).resolve():
            if local:
                # Do not return the job snapshot: it is deleted with the job.
                from model.TaskDeliveryBackground import version_of
                if info.get('version') and version_of(original) != info['version']:
                    raise ValueError('原视频在排队后发生变化，请重新整理；未用旧快照替换新文件。')
                return original
            return output
        if not local:
            # Upload routing still uses the immutable job snapshot. A separate
            # persistent copy belongs to the user, outside the cleanup directory.
            saved_root = Path(info.get('root') or original.parent).parent / '后处理结果' / relative.parent
            saved = saved_root / output.name
            index = 2
            while data_file(saved).exists():
                saved = saved_root / f'{output.stem}_{index:03}.mp4'
                index += 1
            data_file(saved).copy_from(output)
            paths = getattr(context, 'postprocessed_paths', None)
            if paths is None:
                paths = context.postprocessed_paths = []
            paths.append(str(saved))
            context.log('节点处理成品已保留在本地：' + str(saved))
        return output
    return process
