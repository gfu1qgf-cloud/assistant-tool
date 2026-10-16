"""Named per-video operation workflows; the batch renderer owns encoding.

Only plain graph/preparation snapshots cross the queue. Registry callbacks are
trusted implementation objects, never GUI services or user-supplied callables.
"""
import copy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import uuid

from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef, TaskSnapshot, RenderPlan
from model.node_workflow.workflow_types import WorkflowTypeDefinition

TYPE_ID = 'batch_text_video.node_effects'
INPUT_KIND = 'batch_text_video.effect_input'
OUTPUT_KIND = 'batch_text_video.effect_output'


def new_effect():
    return {'id': uuid.uuid4().hex, 'kind': 'node_effect', 'name': '节点效果',
            'source': 'workflow', 'workflow_id': '', 'enabled': True, 'opacity': 100}


def normalize_effect(layer):
    value = layer.setdefault('workflow_id', '')
    if not isinstance(value, str) or value and not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('节点效果的工作流编号无效，请从程序工作流库选择。')
    rotations = layer.get('source_rotations', {})
    if not isinstance(rotations, dict) or len(rotations) > 128:
        raise ValueError('节点来源轮换记录格式无效或超过128项。')
    for key, record in rotations.items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 200 or not isinstance(record, dict)
                or set(record) != {'signature', 'cursor', 'next_id'}
                or not isinstance(record['signature'], str) or not re.fullmatch('[0-9a-f]{64}', record['signature'])
                or type(record['cursor']) is not int or record['cursor'] < 0
                or not isinstance(record['next_id'], str) or not 1 <= len(record['next_id']) <= 128):
            raise ValueError('节点来源轮换记录无效。')


def active_effects(settings):
    return [layer for layer in settings.get('layers', []) if layer.get('kind') == 'node_effect'
            and layer.get('enabled', True) and layer.get('opacity', 100) > 0]


def task_snapshot(job):
    fields = {key: copy.deepcopy(job.get(key, '')) for key in
        ('task_id', 'task_type', 'task_date', 'task_name', 'title', 'body', 'source_sheet', 'source_row',
         'copy_id', 'requires_title', 'copy_profile_id')}
    fields['task_audio_text'] = '\n'.join(value for value in (str(job.get('title', '')), str(job.get('body', ''))) if value)
    fields['task_name'] = str(job.get('task_name') or job.get('label') or job.get('name') or '')
    path = str(job.get('task_dir') or Path(job.get('path') or '.').resolve().parent)
    identity_values = [str(Path(job.get('path') or '.').resolve()), path, str(job.get('task_id', ''))]
    if not job.get('task_id'):
        identity_values += [str(job.get('title', '')), str(job.get('body', ''))]
    identity = 'batch-video:' + hashlib.sha256(json.dumps(identity_values, ensure_ascii=False).encode()).hexdigest()
    return TaskSnapshot(identity, str(job.get('task_id') or job.get('name') or '视频'),
        str(Path(path).resolve()), str(job.get('source_sheet') or ''), job.get('source_row'),
        json.dumps(fields, ensure_ascii=False, allow_nan=False), '')


INPUT_PORTS = (
    ('video', '当前已分配背景视频', 'video'), ('background_path', '背景视频路径', 'text'),
    ('subtitles', '任务字幕 · 优先动态逐词记录', 'subtitles'), ('has_subtitles', '有可用字幕', 'boolean'),
    ('audio', '本次使用的人声音频', 'audio'), ('voice_path', '人声文件路径', 'text'), ('has_voice', '本次已启用人声', 'boolean'),
    ('title', '当前标题', 'text'), ('body', '当前正文', 'text'), ('full_text', '标题＋正文', 'text'),
    ('task_directory', '原任务目录', 'text'), ('task_id', '任务编号', 'text'), ('task_name', '任务名称 · 可接随机种子', 'text'), ('task_type', '任务类型', 'text'),
    ('task_date', '任务日期', 'text'), ('duration', '最终视频时长 · 秒', 'number'),
    ('width', '最终画布宽 · 像素', 'integer'), ('height', '最终画布高 · 像素', 'integer'),
    ('iteration', '当前队列循环位置 · 从1开始', 'integer'), ('iteration_count', '当前队列总数', 'integer'),
)


def build_input(_inputs, _params, context):
    source = getattr(context, 'workflow_input', None)
    if not isinstance(source, dict) or source.get('type') != TYPE_ID:
        raise ValueError('请由文案视频节点效果控件提供当前视频输入，或使用模拟预览。')
    job = source['job']
    globals_value = context.workflow_globals
    return {'video': source['video'], 'background_path': str(job.get('path', '')),
        'subtitles': source.get('subtitles'), 'has_subtitles': source.get('subtitles') is not None,
        'audio': source.get('audio'), 'voice_path': source.get('voice_path', ''), 'has_voice': source.get('audio') is not None,
        'title': str(job.get('title', '')), 'body': str(job.get('body', '')),
        'full_text': '\n'.join(str(job.get(key, '')) for key in ('title', 'body') if job.get(key)),
        'task_directory': str(job.get('task_dir', '')), 'task_id': str(job.get('task_id', '')),
        'task_name': str(getattr(context, 'task', None).fields.get('task_name', '') if getattr(context, 'task', None) else job.get('task_name', '')),
        'task_type': str(job.get('task_type', '')), 'task_date': str(job.get('task_date', '')),
        'duration': source['duration'], 'width': source['width'], 'height': source['height'],
        'iteration': globals_value.iteration, 'iteration_count': globals_value.iteration_count}


def build_output(inputs, _params, context):
    from model.node_workflow.operations import flatten_operations
    operations = flatten_operations(inputs.get('operations'))
    if len(operations) > 128:
        raise ValueError('一个节点效果最多返回128项操作，未静默截断。')
    source = context.workflow_input['video']
    from model.node_workflow.video_stream import VideoStream
    root = source.source if isinstance(source, VideoStream) else source
    for operation in operations:
        if operation.video is not None and operation.video != root:
            raise ValueError('节点效果只返回当前画布的操作，不接受替换成另一段视频；视频导出归文案视频负责。')
    return {'operations': tuple(replace(operation, video=None) for operation in operations)}


def entry_prepare(_task, _params, _services):
    # Warm/register fonts ONLY in GUI-side preparation, not a worker callback.
    from .layout import ensure_fonts
    ensure_fonts()
    return {}


def definitions():
    return (NodeDefinition(INPUT_KIND, '文案视频输入',
        outputs=tuple(NodePort(key, title, dtype, capabilities=('seek', 'replay') if dtype == 'video' else ())
                      for key, title, dtype in INPUT_PORTS),
        build=build_input, prepare=entry_prepare, category='批量文案视频/节点效果',
        description='当前队列实际分配的背景、实际选用的人声／字幕、当前标题正文、最终画布与时长。字幕优先使用已验证的ELE动态逐词记录，否则对应SRT。不是空白视频或另一任务字幕。没有已有字幕时端口为空；不自动调用识别模型。'),
        NodeDefinition(OUTPUT_KIND, '返回视频操作',
            inputs=(NodePort('operations', '视频操作 · 按连线顺序', 'operations', multiple=True, required=False),),
            terminal_result=NodePort('operations', '交回文案视频的操作', 'operations'),
            build=build_output, category='批量文案视频/节点效果',
            description='可连接多个字幕／文字／图片／GIF／时间段操作，按连线顺序执行；没有输出端口，不生成视频文件。文案视频在此控件所在图层位置统一合成，各节点不另导出完整视频。'))


def example(registry):
    from model.node_workflow.graph import empty_graph, new_node
    graph = empty_graph()
    graph.update(workflow_type=TYPE_ID, workflow_type_version=1, execution_mode='line')
    nodes = [new_node(registry.definitions[kind], position) for kind, position in
        ((INPUT_KIND, (0, 0)), ('core.text_palette', (340, 440)),
         ('batch_text_video.caption', (680, 0)), (OUTPUT_KIND, (1010, 0)))]
    entry, palette_node, caption, output = nodes
    from .captions import new_caption
    caption['params']['caption'] = new_caption()
    caption['params']['caption'].update(caption_timing='task', background_enabled=True, background_opacity=100,
        caption_preset='appear', font='Open Sans', font_weight=700, outline_width=0, shadow_enabled=False)
    graph.update(nodes=nodes, output_node=output['id'], connections=[
        [entry['id'], 'subtitles', caption['id'], 'subtitles'],
        [palette_node['id'], 'text', caption['id'], 'color'],
        [palette_node['id'], 'background', caption['id'], 'background_color'],
        [palette_node['id'], 'outline', caption['id'], 'outline_color'],
        [caption['id'], 'operations', output['id'], 'operations']])
    return graph


def preview_task(graph):
    from model.node_workflow.library import WorkflowLibrary
    return task_snapshot({'id': 'node-effect-simulation', 'name': '节点效果模拟', 'title': '模拟标题',
        'body': '这是字幕颜色与效果的模拟预览。', 'task_dir': str(WorkflowLibrary().root / '_simulation'), 'task_id': '1000'})


def preview_input(context, directory, graph):
    from model.node_workflow.visual_nodes import build_background
    from model.AppDataStorage import data_file
    options = graph.get('simulation', {})
    if not isinstance(options, dict): raise ValueError('模拟画布设置必须为对象。')
    width, height = options.get('width', 720), options.get('height', 1280)
    duration, color = options.get('duration', 5), options.get('color', '#334155')
    if (type(width) is not int or type(height) is not int or not 16 <= width <= 1920 or not 16 <= height <= 1920
            or type(duration) not in (int, float) or not 1 <= duration <= 30 or not isinstance(color, str)
            or not re.fullmatch(r'#[0-9a-fA-F]{6}', color)):
        raise ValueError('模拟画布须为16–1920像素，时长1–30秒，颜色为#RRGGBB。')
    values = {'width': width, 'height': height, 'duration': duration, 'fps': 25, 'style': 'solid',
        'color': color, 'color2': color, 'x0': 0, 'y0': 0, 'x1': 100, 'y1': 100}
    video = build_background({}, values, context)['video']
    srt = Path(directory) / '模拟字幕.srt'
    # This writes a tiny, explicitly synthetic subtitle, never a user task SRT.
    end = f'00:00:{int(duration):02d},000'
    data_file(srt).write_text('1\n00:00:00,000 --> '+end+'\n这是字幕颜色与效果的模拟预览。\n', 'utf-8')
    return {'type': TYPE_ID, 'job': {'path': '纯色模拟（不是实际背景）', 'title': '模拟标题',
        'body': '这是字幕颜色与效果的模拟预览。', 'task_id': '1000', 'task_dir': ''},
        'video': video, 'subtitles': ResourceRef.capture(srt, 'subtitles'), 'audio': None,
        'voice_path': '', 'duration': float(duration), 'width': width, 'height': height,
        'alignment_directory':str(directory)}


def preview_plan(context, values):
    from model.node_workflow.operations import flatten_operations
    groups = [value for value in values.values() if isinstance(value, tuple)]
    try:
        operations = flatten_operations(groups)
    except TypeError:
        return None
    return RenderPlan(context.workflow_input['video'], operations, '') if groups else None


def register_type(context):
    from .project_runtime import unbind_project_paths
    for definition in definitions(): context.register_node(definition)
    context.register_workflow_type(WorkflowTypeDefinition(TYPE_ID, '文案视频 · 节点效果', INPUT_KIND, OUTPUT_KIND,
        example_factory=example, preview_factory=preview_input, execution_mode='line',
        preview_task_factory=preview_task, preview_plan_factory=preview_plan, migrate_graph=unbind_project_paths,
        description='由文案视频控件逐个注入真实素材和字幕，只交回操作；可以随机与条件组合，统一最终编码。'))


def fallback_registry():
    from model.node_workflow.graph import NodeRegistry, register_core
    from .workflow_nodes import canvas_nodes
    from types import SimpleNamespace
    registry = NodeRegistry(); register_core(registry)
    for definition in canvas_nodes(): registry.register('batch_text_video', definition)
    register_type(SimpleNamespace(register_node=lambda d: registry.register('batch_text_video', d),
        register_workflow_type=lambda d: registry.register_workflow_type('batch_text_video', d)))
    return registry


def prepare_bundle(state, ids, services=None, *, graph_check=None, iteration_ids=None):
    """GUI-side only: snapshot saved graphs and prepare each actual queue row."""
    from model.node_workflow.library import WorkflowLibrary
    from model.node_workflow.tasks import prepare_job
    from model.node_workflow.global_data import WorkflowGlobals
    from .project_runtime import ProjectServices
    active = active_effects(state['settings'])
    if not active: return None, None, ()
    registry = services.available_nodes() if callable(getattr(services, 'available_nodes', None)) else fallback_registry()
    library = services.workflow_library if services is not None and hasattr(services, 'workflow_library') else WorkflowLibrary()
    runtime_services = ProjectServices(services,state)
    graphs, resources, jobs = {}, set(), {}
    for layer in active:
        normalize_effect(layer)
        if not layer['workflow_id']:
            raise ValueError(layer['name']+'：尚未选择工作流，请在“节点效果”分页中选择。')
        key = layer['workflow_id']
        if key not in graphs:
            graph = library.load(key, registry)
            if graph.get('workflow_type') != TYPE_ID:
                raise ValueError(layer['name']+'：请选择“文案视频 · 节点效果”类型，不能使用导出／任务交付工作流。')
            if graph_check is not None:
                graph_check(graph, registry)
            graphs[key] = graph
    wanted = set(ids)
    by_id = {job['id']: job for job in state['jobs']}
    selected = [by_id[identifier] for identifier in (iteration_ids or ids) if identifier in by_id]
    globals_value = WorkflowGlobals.create('line', max(1, len(selected)))
    for index, job in enumerate(selected):
        if job['id'] not in wanted: continue
        task = task_snapshot(dict(job, copy_profile_id=state.get('active_profile', '')))
        jobs[job['id']] = {}
        for key, graph in graphs.items():
            payload, locks = prepare_job(graph, registry, task, runtime_services, global_data=globals_value.at_iteration(index+1))
            jobs[job['id']][key] = payload
            resources.update(locks)
    if wanted - jobs.keys(): raise ValueError('节点效果找不到当前视频的队列记录，请重新选择视频。')
    bundle = {'type': TYPE_ID, 'jobs': jobs}
    if len(json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 32*1024*1024:
        raise ValueError('本次节点快照超过32MB，请减少节点或分批提交；未截断工作流。')
    return bundle, registry, tuple(sorted(resources))


def cache_snapshot(bundle, job_id):
    """Stable default random; transient batch IDs/time are ignored unless wired."""
    if not bundle: return None
    values = copy.deepcopy(bundle['jobs'].get(job_id, {}))
    for payload in values.values():
        for prepared in payload.get('prepared', {}).values():
            prepared.pop('elevenlabs_binding', None)
        nodes = {n['id']: n for n in payload['graph']['nodes']}
        volatile = any(nodes[edge[0]]['kind'] == 'core.global_data' and edge[1] in {'batch_id', 'started_at', 'run_date'}
                       for edge in payload['graph']['connections'])
        if not volatile:
            for key in ('batch_id', 'started_at'): payload['global_data'].pop(key, None)
        if any(n['kind'] == 'batch_text_video.component_source' and
               n['params']['source'].get('caption_timing') in {'auto', 'model'} for n in nodes.values()):
            from globalValue import globalValue
            payload['alignment_model'] = globalValue.whisper_model_name()
    return values


def prepare_preview_bundle(state, job, services=None, *, graph_check=None):
    if not active_effects(state['settings']): return None, None
    state = copy.deepcopy(state)
    existing = next((value for value in state['jobs'] if value['id'] == job['id']), None)
    if existing is None:
        state['jobs'].append(copy.deepcopy(job))
    else:
        existing.update(copy.deepcopy(job))
    plan = next((p for p in state.get('render_plans', [])
                 if p.get('profile_id') == state.get('active_profile')
                 and p.get('status') in {'staged', 'submitted'} and job['id'] in p.get('ids', [])), None)
    iteration_ids = plan['ids'] if plan else [job['id']]
    bundle, registry, _resources = prepare_bundle(state, [job['id']], services,
        graph_check=graph_check, iteration_ids=iteration_ids)
    return bundle, registry


def subtitle_for(job, voice):
    from .captions import caption_subtitle_path, new_caption
    value = new_caption(); value['caption_timing'] = 'task'
    explicit = next((job[key] for key in ('subtitle_path', 'srt_path') if job.get(key)), '')
    if explicit and not Path(explicit).is_file():
        raise FileNotFoundError('当前任务明确指定的字幕不存在：'+str(explicit))
    try:
        return caption_subtitle_path(job, voice, value)
    except FileNotFoundError:
        return None


def input_subtitles(job, voice, duration, *, canceled=None, report=None, task_key=''):
    """Worker-side input adapter; hashing must never run in preview_key/GUI."""
    from model.node_workflow.contracts import SubtitleCue, SubtitleTrack
    if voice:
        from model.ElevenLabsTimings import load_timings
        words = load_timings(voice,'',duration,canceled,report=report)
        if words is not None:
            track = SubtitleTrack(tuple(SubtitleCue(w['start'],w['end'],w['text']) for w in words),
                                  task_key,duration,timing='word_aligned')
            track.verify()
            if report: report('节点任务字幕：使用 ELE 动态逐词记录；普通 task_audio.srt 保持不变。')
            return track
    srt = subtitle_for(job,voice)
    return ResourceRef.capture(srt,'subtitles') if srt else None


class EffectRuntime:
    """One execution context per component; never mutate a shared GUI context."""
    def __init__(self, bundle, registry, cancel=None, report=None, logger=None, *, preview=False):
        self.bundle, self.registry = bundle, registry
        self.preview = bool(preview)
        self.cancel, self.report, self.logger = cancel, report or (lambda _m: None), logger or (lambda _m: None)
        self.refs = []
        self.sequence_plan = []

    def fork(self, *, cancel=None, report=None):
        """Immutable bundle/registry shared; per-video references and ledgers never shared."""
        return EffectRuntime(self.bundle, self.registry, self.cancel if cancel is None else cancel,
            self.report if report is None else report, self.logger, preview=self.preview)

    def start_job(self, _job, *, preview=None):
        self.refs = []
        self.sequence_plan = []
        if preview is not None: self.preview = bool(preview)

    @contextmanager
    def evaluated(self, job, layer, directory, info, voice, ffmpeg):
        """One shared evaluator for final compilation and non-encoding design scrubbing."""
        from model.BackgroundTasks import TaskRunContext
        from model.node_workflow.graph import evaluate_graph
        from model.node_workflow.scope import initialize_execution
        from model.node_workflow.media import MediaSession
        from .engine import Cancelled
        class Token:
            def is_set(_self): return bool(self.cancel and self.cancel.is_set())
        context = TaskRunContext(Token(), lambda m, _p: self.report(m), self.logger)
        payload = self.bundle['jobs'].get(job['id'], {}).get(layer['workflow_id']) if self.bundle else None
        if payload is None: raise ValueError(layer['name']+'：缺少提交时的节点快照，请从文案视频窗口重新提交。')
        initialize_execution(payload, context)
        if self.preview:
            from dataclasses import replace
            context.workflow_globals = replace(context.workflow_globals, is_preview=True)
        context.prepared, context.notes = payload['prepared'], []
        context.ffmpeg, context.workflow_registry = ffmpeg, self.registry
        context.processing_summary, context.generated_subtitles = {}, None
        frozen_task = context.task
        actual = dict(job, task_dir=frozen_task.directory)
        # Real currently selected source (including voice-pool choices), not a
        # private preview-cache directory or a stale source attached to the row.
        video = ResourceRef.capture(job['path'], 'video')
        subtitles = input_subtitles(actual,voice,info['duration'],canceled=Token(),report=self.report,task_key=context.task.key)
        audio = ResourceRef.capture(voice, 'audio') if voice else None
        context.workflow_input = {'type': TYPE_ID, 'job': actual, 'video': video,
            'subtitles': subtitles, 'audio': audio, 'voice_path': voice or '', 'duration': info['duration'],
            'width': info['video']['width'], 'height': info['video']['height'], 'effect_layer': copy.deepcopy(layer),
            'alignment_directory': str(directory)}
        self.refs.extend(value for value in (video, subtitles, audio) if isinstance(value,ResourceRef))
        try:
            with MediaSession(context, ffmpeg) as session:
                context.media = session
                result = evaluate_graph(payload['graph'], self.registry, context)
                operations = result['operations']
                self.refs.extend(getattr(context, 'component_source_refs', []))
                if not self.preview:
                    self.sequence_plan.extend(getattr(context, 'component_source_steps', {}).values())
                for operation in operations: self.refs.extend(ref for _, ref in operation.assets if isinstance(ref, ResourceRef))
                yield context, video, operations
                context.check_canceled()
        except Exception:
            if Token().is_set(): raise Cancelled('节点效果已取消，未推进资源池。') from None
            raise

    def append(self, job, layer, args, filters, background, next_index, label, directory, info, voice, ffmpeg):
        from model.node_workflow.render import compile_filters
        from model.node_workflow.filter_graph import append_chain
        with self.evaluated(job, layer, directory, info, voice, ffmpeg) as (context, video, operations):
            if not operations:
                filters.append(f'[{background}]null[{label}]'); return next_index
            chain = compile_filters(RenderPlan(video, operations, ''), directory, context,
                                    video_info=info, asset_prefix=label+'_')
            opacity = layer['opacity']/100
            if opacity < 1:
                filters.append(f'[{background}]split=2[{label}_before][{label}_input]')
                next_index = append_chain(chain, args, filters, label+'_input', next_index,
                    final=label+'_effect', prefix=label+'_')
                filters.append(f'[{label}_before]format=gbrap[{label}_base_rgba]')
                filters.append(f'[{label}_effect]format=gbrap[{label}_effect_rgba]')
                filters.append(f"[{label}_base_rgba][{label}_effect_rgba]blend=all_expr='A*{1-opacity:.9f}+B*{opacity:.9f}',format=rgba[{label}]")
            else:
                next_index = append_chain(chain, args, filters, background, next_index, final=label, prefix=label+'_')
            return next_index

    def verify(self):
        for ref in self.refs: ref.verify()

    def protect_output(self, output):
        from .store import file_identity
        identity = file_identity(output)
        if any(ref.storage == 'file' and file_identity(ref.path) == identity for ref in self.refs):
            raise ValueError('成品文件名与节点使用的原始素材相同，已阻止覆盖；请修改成品名称。')
