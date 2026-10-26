"""Non-encoding design preview of native and safe deferred overlay layers."""
import copy
import hashlib
import json

from .node_effects import active_effects, prepare_preview_bundle, EffectRuntime


def resolve_preview_sources(layers, title='', body='', *, job=None, cancel=None):
    """Isolate source errors in a private editing snapshot; export stays strict."""
    from .components import resolve_sources
    resolved, notices = [], []
    for layer in layers:
        if cancel is not None and cancel.is_set():
            from .engine import Cancelled
            raise Cancelled('已取消图层预览。')
        try:
            items, _ = resolve_sources([layer], title, body, job=job)
            resolved.extend(items)
        except (OSError, ValueError, RuntimeError) as error:
            if cancel is not None and cancel.is_set(): raise
            snapshot = copy.deepcopy(layer)
            snapshot['enabled'] = False
            resolved.append(snapshot)
            notices.append(f"{layer.get('name', '图层')}暂未显示：{error}（仅编辑预览跳过，导出仍需处理）")
    return resolved, notices


def check_design_graph(graph, registry):
    from model.node_workflow.graph import validate
    errors, order, _ = validate(graph, registry)
    if errors:
        raise ValueError(errors[0][1])
    from model.node_workflow.subgraphs import flatten
    flat, _, _ = flatten(graph, registry)
    # Check every compiled node, not only a currently selected lazy branch:
    # scrubbing must never unexpectedly start AI, arbitrary code or a model.
    for node in flat['nodes']:
        definition = registry.definitions[node['kind']]
        if (registry.owners.get(node['kind']) not in {'core', 'batch_text_video'}
                or definition.execution != 'plan'
                or node['kind'] == 'batch_text_video.component_source'
                and node['params']['source'].get('kind') == 'caption'):
            raise ValueError(definition.title + '需要执行计算；动画快览不自动调用，请使用节点预览或播放预览。')


def prepare_design_bundle(state, job, services=None):
    return prepare_preview_bundle(state, job, services, graph_check=check_design_graph)


def design_layers(settings, job, bundle, registry, width, height, directory, cancel=None):
    """Use export's operation builders for text and visual-poster layers."""
    result, notices = [], []
    value = settings.get('_editor_timing')
    if not active_effects(settings) or value is None:
        return settings, notices
    runtime = EffectRuntime(bundle, registry, cancel, preview=True) if bundle else None
    info = {'duration': value['duration'], 'audio': False,
            'video': {'width': width, 'height': height, 'display_width': width, 'display_height': height}}
    for layer in settings['layers']:
        if layer['kind'] != 'node_effect':
            result.append(copy.deepcopy(layer)); continue
        if not layer['enabled'] or not layer['opacity']:
            continue
        # Keep a harmless node-effect marker so an intentionally empty result
        # remains a valid empty canvas, rather than a "no text" layout error.
        result.append(copy.deepcopy(layer))
        if runtime is None:
            notices.append(settings.get('_design_notice') or '节点快览尚未准备，请读取背景和成品时长后重试。')
            continue
        try:
            if layer['opacity'] != 100:
                raise ValueError('半透明节点组合请使用播放预览核对完整混合。')
            with runtime.evaluated(job, layer, directory, info, settings.get('_preview_voice', ''),
                                   settings.get('ffmpeg_path', '')) as (_context, _video, operations):
                local = []
                for index, operation in enumerate(operations):
                    if operation.name == 'null':
                        continue
                    if operation.name not in {'batch_text_video.text_layer', 'batch_text_video.image_layer',
                                              'batch_text_video.animated_layer', 'batch_text_video.visual_layer'}:
                        raise ValueError('该节点组合含未支持快览的字幕或视频操作，请使用播放预览。')
                    item = json.loads(dict(operation.options)['layer'])
                    item['id'] = 'preview_' + hashlib.sha256(f"{layer['id']}:{index}".encode()).hexdigest()[:24]
                    # The node's builder already resolved task text, pool item,
                    # random colours and source paths; do not resolve them twice.
                    item.update(source='fixed', source_file='', sequence_items=[], sequence_cursor=0)
                    local.append(item)
                result.extend(local)
        except Exception as error:
            if cancel is not None and cancel.is_set():
                raise
            notices.append(layer['name'] + '：' + str(error))
    if runtime:
        runtime.verify()
    return {**settings, 'layers': result}, list(dict.fromkeys(notices))
