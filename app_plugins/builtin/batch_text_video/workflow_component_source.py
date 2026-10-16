"""Native source/timing bridge; rotation is committed by the batch owner only."""
import copy
from dataclasses import asdict
import hashlib
from itertools import groupby
import json
from pathlib import Path

from app_plugins.api import NodeDefinition, NodeParameter, NodePort
from model.node_workflow.contracts import ResourceRef, SubtitleCue, SubtitleTrack
from .component_nodes import SOURCE_KIND, SOURCE_KEYS
from .components import normalize_source, resolve_sources, CONTENT_FIELDS


def normalized(value):
    if not isinstance(value, dict) or value.get('kind') not in CONTENT_FIELDS:
        raise ValueError('控件来源类型无效。')
    value = copy.deepcopy(value)
    value.setdefault('id', 'component_source')
    value.setdefault('name', '控件来源')
    if not isinstance(value['id'], str) or not 1 <= len(value['id']) <= 128:
        raise ValueError('控件来源编号无效。')
    # A pool is a live reference, not a second copy of the entire copy/media
    # library in a shareable graph. Preserve its next item by stable ID.
    if value.get('source') == 'pool':
        items = value.get('sequence_items') or []
        cursor = value.get('sequence_cursor', 0)
        if type(cursor) is not int or cursor < 0:
            raise ValueError('资源池位置无效。')
        if items:
            value.update(pool_cursor=cursor, pool_next_id=items[cursor % len(items)]['id'])
        value.setdefault('pool_cursor', 0); value.setdefault('pool_next_id', '')
        if (type(value['pool_cursor']) is not int or value['pool_cursor'] < 0
                or not isinstance(value['pool_next_id'], str) or len(value['pool_next_id']) > 128):
            raise ValueError('资源池起点无效。')
        value.update(sequence_items=[], sequence_cursor=0)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 256000:
        raise ValueError('控件来源设置超过256KB。')
    normalize_source(value)
    if value['kind'] == 'caption':
        from .captions import new_caption, normalize_caption
        full = new_caption(); full.update(value); normalize_caption(full)
        value = {k: full[k] for k in SOURCE_KEYS if k in full}
    return value


def signature(value):
    # Default behaviour is an additive setting. Do not invalidate unrelated
    # custom-pool/sequence rotation ledgers created before this field existed.
    value = copy.deepcopy(value)
    if value.get('copy_binding') == 'task':
        value.pop('copy_binding')
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def prepare_source(task, params, services):
    from .pools import bind_pool_layers
    value = normalized(params['source'])
    refs = []
    from .copy_binding import bind_task_copy
    bound = bind_task_copy(value, task.fields if task is not None else None)
    if bound is not None:
        value = bound
    if value.get('source') == 'pool':
        from .project_runtime import project_runtime
        data = project_runtime(services)['data']
        value = bind_pool_layers([value], data)[0]
        items = value['sequence_items']
        value['sequence_cursor'] = next((i for i, item in enumerate(items) if item['id'] == value['pool_next_id']),
            value['pool_cursor'] % len(items) if items else 0)
    field = CONTENT_FIELDS[value['kind']]
    paths = []
    if value.get('source') == 'file': paths.append(value.get('source_file') or value.get(field, ''))
    if field != 'text':
        paths += [v[field] for v in value.get('sequence_items', [])]
        if value.get(field): paths.append(value[field])
    for path in dict.fromkeys(p for p in paths if p):
        if Path(path).is_file(): refs.append(asdict(ResourceRef.capture(path, 'component_source')))
    result = {'source': value, 'files': refs}
    if bound is not None:
        result['task_copy_binding'] = str(task.fields.get('task_id') or task.label)
    if value.get('caption_timing') == 'elevenlabs':
        prepare = getattr(services, 'prepare_elevenlabs', None)
        if not callable(prepare): raise ValueError('ELE 云端对齐需要主程序提供会话。')
        result['elevenlabs_binding'] = prepare()
    return result


def build_source(inputs, params, context):
    from .node_effects import TYPE_ID
    entry = getattr(context, 'workflow_input', {})
    if entry.get('type') != TYPE_ID:
        raise ValueError('控件来源节点需要文案视频节点效果的运行上下文。')
    value = copy.deepcopy(context.node_prepared.get('source'))
    if not value: raise ValueError('缺少控件来源快照，请重新提交或预览。')
    if context.node_prepared.get('task_copy_binding'):
        context.report('文案来源：跟随当前任务 ' + context.node_prepared['task_copy_binding']
                       + '，不使用独立轮换游标。', None)
    token = signature(normalized(params['source']))
    owner = entry.get('effect_layer', {})
    old = owner.get('source_rotations', {}).get(context.node_id)
    valid_old = old if old and old.get('signature') == token else None
    items = value.get('sequence_items', [])
    if items and valid_old:
        value['sequence_cursor'] = next((i for i, v in enumerate(items) if v['id'] == valid_old['next_id']),
            valid_old['cursor'] % len(items))
    title, body = inputs.get('title', ''), inputs.get('body', '')
    resolved, steps = resolve_sources([value], title or '', body or '')
    layer = resolved[0]
    field = CONTENT_FIELDS[layer['kind']]
    path = layer.get('source_file') if layer.get('source') == 'file' else layer.get(field) if field != 'text' else ''
    refs = [ResourceRef(**v) for v in context.node_prepared.get('files', [])]
    if path:
        selected = next((r for r in refs if r.path == str(Path(path).resolve())), None)
        if selected is None: raise FileNotFoundError('提交时来源文件不存在：'+str(path))
        selected.verify()
        context.component_source_refs = getattr(context, 'component_source_refs', []) + [selected]
    if steps and owner:
        next_cursor = steps[0]['next_cursor']
        step = {'node_source': True, 'id': owner['id'], 'workflow_id': owner['workflow_id'],
            'node_id': context.node_id, 'before': copy.deepcopy(old),
            'after': {'signature': token, 'cursor': next_cursor, 'next_id': items[next_cursor]['id']}}
        plans = getattr(context, 'component_source_steps', {})
        if context.node_id in plans:
            raise ValueError('同一轮换来源在循环中重复执行，请使用池索引节点明确选择。')
        plans[context.node_id] = step
        context.component_source_steps = plans
    if layer['kind'] != 'caption':
        return {'content': layer.get(field, ''), 'subtitles': None}
    if type(inputs.get('duration')) not in (int, float) or inputs['duration'] <= 0:
        raise ValueError('字幕来源需要连接文案视频输入的“最终时长”。')
    from .captions import task_word_timings
    from model.BackgroundTasks import TaskCancellationToken
    # Reuse the native, small JSON alignment cache and its key. No rendered
    # media or new intermediate video is produced by this source node.
    directory = entry.get('alignment_directory')
    if not directory:
        raise ValueError('运行方没有提供字幕对齐缓存目录；节点不读取历史项目路径。')
    job = copy.deepcopy(entry['job'])
    job['task_dir'] = inputs.get('task_directory') or job.get('task_dir', '')
    subtitles = inputs.get('subtitles')
    if isinstance(subtitles, ResourceRef):
        subtitles.verify()
        job['caption_subtitles'] = {**job.get('caption_subtitles', {}), layer['id']: subtitles.path}
        context.component_source_refs = getattr(context, 'component_source_refs', []) + [subtitles]
    if (entry.get('audio') is None and not Path(job.get('path') or '__missing__').is_file()
            and isinstance(subtitles, ResourceRef)):
        from .workflow_caption import subtitle_words
        context.report('纯色模拟：使用示例字幕展示样式，不运行实际人声模型。', None)
        words = subtitle_words(subtitles, inputs['duration'], layer['caption_timing'] == 'uniform')
    else:
        from model.ElevenLabsService import alignment_session, current_alignment_session
        with alignment_session(context.node_prepared.get('elevenlabs_binding') or current_alignment_session()):
            words = task_word_timings(job, inputs.get('voice_path') or '', layer, title or '', body or '',
                inputs['duration'], directory, TaskCancellationToken(context), lambda m: context.report(m, None))
    cues = []
    for _, group in groupby(words, lambda w: (w.get('cue'), w['start'], w['end'])):
        values = list(group)
        cues.append(SubtitleCue(values[0]['start'], values[0]['end'], ' '.join(v['text'] for v in values)))
    track = SubtitleTrack(tuple(cues), context.task.key, inputs['duration'],
        timing='word_aligned' if all('cue' not in w for w in words) else 'source_words_mapped_to_edit')
    track.verify()
    return {'content': '', 'subtitles': track}


def commit_steps(layers, steps):
    by_id = {layer['id']: layer for layer in layers}
    seen = set()
    for step in steps:
        layer = by_id.get(step['id'])
        key = (step['id'], step['node_id'])
        if (not layer or layer['kind'] != 'node_effect' or not layer.get('enabled') or not layer.get('opacity')
                or layer['workflow_id'] != step['workflow_id'] or key in seen):
            raise ValueError('节点轮换控件已改变，未推进来源。')
        seen.add(key)
        rotations = layer.setdefault('source_rotations', {})
        if rotations.get(step['node_id']) != step['before']:
            raise ValueError('节点轮换位置已改变，未推进来源。')
        rotations[step['node_id']] = copy.deepcopy(step['after'])


def source_node():
    from .workflow_component_source_ui import source_inspector
    return NodeDefinition(SOURCE_KIND, '资源轮换／字幕对齐',
        inputs=tuple(NodePort(key, label, dtype, required=False) for key, label, dtype in (
            ('title', '任务标题', 'text'), ('body', '任务正文', 'text'), ('voice_path', '人声路径', 'text'),
            ('subtitles', '当前任务字幕', 'subtitles'), ('task_directory', '任务目录', 'text')))
            + (NodePort('duration', '最终时长 · 秒', 'number', required=False),),
        outputs=(NodePort('content', '文字／素材路径', 'text'), NodePort('subtitles', '原时间戳／对齐字幕', 'subtitles')),
        parameters=(NodeParameter('source', '原控件来源', 'object', {'id':'source', 'kind':'text', 'source':'fixed', 'text':''}),),
        build=build_source, prepare=prepare_source, inspector_factory=source_inspector,
        validate_parameters=lambda p: normalized(p['source']), category='批量文案视频/节点效果',
        description='仅用于独立资源轮换或字幕对齐，不是任务文字的必经节点。'
            '任务标题／正文请直接从“文案视频输入”连接“添加文字”的文字输入。'
            '沿用原任务字幕、模型对齐、匀速动画及资源池来源；仅最终导出成功后推进轮换，预览不消耗。')
