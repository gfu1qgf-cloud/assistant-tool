"""Lossless component-to-workflow conversion, using existing canvas nodes."""
import copy

from .components import CONTENT_FIELDS
from .layers import componentize_layers
from .node_effects import TYPE_ID, INPUT_KIND, OUTPUT_KIND, new_effect

SOURCE_KIND = 'batch_text_video.component_source'
SOURCE_KEYS = ('id', 'kind', 'name', 'enabled', 'opacity', 'source', 'source_file',
               'text', 'path', 'caption_file', 'sequence_items', 'sequence_cursor',
               'pool_id', 'pool_field', 'pool_name', 'pool_cursor', 'pool_next_id', 'copy_binding',
               'caption_timing', 'caption_source', 'caption_language', 'caption_cloud_consent')


def task_text_port(source):
    """Task text is a graph wire, never a hidden lookup in a source node."""
    kind = source['kind']
    if kind not in {'title', 'body', 'text'}:
        return None
    if source.get('source') == 'job':
        return 'title' if kind == 'title' else 'body'
    if (source.get('source') == 'pool' and not source.get('pool_id')
            and source.get('copy_binding', 'task') == 'task'):
        return {'title': 'title', 'body': 'body', 'full': 'full_text'}[source['pool_field']]
    return None


def component_graph(layer, settings, registry):
    from model.node_workflow.graph import empty_graph, new_node
    from .watermarks import expand_watermarks
    original = copy.deepcopy(layer)
    if original['kind'] == 'node_effect':
        raise ValueError('这个控件已经是节点效果，请直接编辑它的工作流。')
    # Legacy title/body styles are materialized before copying parameters.
    layer = next(v for v in componentize_layers([original], settings) if v['id'] == original['id'])
    graph = empty_graph()
    graph.update(workflow_type=TYPE_ID, workflow_type_version=1, execution_mode='line')
    entry = new_node(registry.definitions[INPUT_KIND], (0, 0))
    output = new_node(registry.definitions[OUTPUT_KIND], (1040, 0))
    graph.update(nodes=[entry, output], connections=[], output_node=output['id'])
    # The replacement component owns the top-level visibility switch. Build a
    # fully connected graph even when converting a currently hidden component,
    # so enabling it later works without repairing missing wires.
    drawing = dict(layer, enabled=True)
    pieces = expand_watermarks([drawing]) if layer['kind'] == 'watermark' else [drawing]
    for index, piece in enumerate(pieces):
        source = {k: copy.deepcopy(piece[k]) for k in SOURCE_KEYS if k in piece}
        source.update(enabled=True, opacity=100)
        from .workflow_component_source import normalized
        source = normalized(source)
        style = copy.deepcopy(piece)
        field = CONTENT_FIELDS[piece['kind']]
        if piece['kind'] == 'caption':
            kind = 'batch_text_video.caption'
            style.update(source='voice', source_file='', caption_file='', sequence_items=[],
                         sequence_cursor=0, caption_timing='task')
        else:
            if piece['kind'] in {'title', 'body'}:
                style.update(kind='text', id=piece['id']+'_node')
            media = 'text'
            if piece['kind'] == 'image':
                media = 'visual'
            kind = 'batch_text_video.' + media + '_layer'
            style.update(source='fixed', source_file='', sequence_items=[], sequence_cursor=0)
            if style['kind'] == 'image' and not style.get('path'):
                style['source'] = 'file'  # Content will arrive through the source node.
        operation = new_node(registry.definitions[kind], (680, index * 270))
        operation['params']['caption' if piece['kind'] == 'caption' else 'layer'] = style
        if style['kind'] == 'text': operation['params']['skip_empty'] = True
        graph['nodes'].append(operation)
        active = piece.get('enabled', True) if layer['kind'] == 'watermark' else True
        # Disabled/empty watermark corners stay in the graph with all settings,
        # but do not force subtitle/audio/file work during graph evaluation.
        if active:
            task_port = task_text_port(source)
            if task_port is not None:
                graph['connections'].append([entry['id'], task_port, operation['id'], 'content'])
            elif piece['kind'] == 'caption' or piece.get('source') in {'sequence', 'pool'}:
                adapter = new_node(registry.definitions[SOURCE_KIND], (340, index * 270))
                adapter['params'].update(source=source)
                graph['nodes'].append(adapter)
                graph['connections'].append([adapter['id'], 'subtitles' if piece['kind'] == 'caption' else 'content',
                    operation['id'], 'subtitles' if piece['kind'] == 'caption' else 'content'])
                for key in ('title', 'body', 'voice_path', 'task_directory', 'duration', 'subtitles') if piece['kind'] == 'caption' else ():
                    graph['connections'].append([entry['id'], key, adapter['id'], key])
            else:
                # Fixed files use the existing layer node's bounded reader and
                # captured file stamps, rather than freezing the current image.
                operation['params']['layer'].update(source=piece.get('source', 'fixed'),
                    source_file=piece.get('source_file', ''), **{field: piece.get(field, '')})
            graph['connections'].append([operation['id'], 'operations', output['id'], 'operations'])
    effect = new_effect()
    effect.update(id=original['id'], name=original['name'] + ' · 节点', enabled=original.get('enabled', True))
    # Opacity is already preserved inside every operation (watermarks expanded
    # with parent opacity). Do not multiply it again at the outer effect.
    return graph, effect


class ComponentNodeController:
    def convert_component_to_nodes(self, identifier):
        from qt_compat import QtWidgets
        if self.is_batch_busy(): return
        self.flush_editor_history()
        current = next((v for v in self.layer_panel.values() if v['id'] == identifier), None)
        if current is None: return
        services = self.context
        try:
            if not callable(getattr(services, 'open_workflow_editor', None)):
                raise ValueError('请从主程序打开插件后使用转换功能。')
            if current['kind'] == 'node_effect':
                services.open_workflow_editor(workflow_id=current.get('workflow_id', ''), workflow_type=TYPE_ID)
                return
            name, accepted = QtWidgets.QInputDialog.getText(self, '控件转换为节点',
                '工作流名称（自动保存到程序库；转换可用 Ctrl+Z 撤销）：', text=current['name'] + ' · 节点效果')
            if not accepted: return
            if not str(name).strip(): raise ValueError('请输入工作流名称。')
            if not self.persist(): raise ValueError('当前配置尚未保存成功，未转换控件。')
            before = copy.deepcopy(self.state)
            layers = self.layer_panel.values()
            registry = services.available_nodes()
            graph, effect = component_graph(current, self.state['settings'], registry)
            # Validate component count/shape before creating a library entry.
            from .layers import normalize_layers
            replacement = [effect if v['id'] == identifier else v for v in layers]
            normalize_layers(replacement)
            from model.node_workflow.graph import validate
            errors = validate(graph, registry)[0]
            if errors: raise ValueError('节点转换未通过检查：'+'；'.join(str(v) for v in errors))
            saved = services.workflow_library.save(graph, str(name).strip(), registry)
            effect['workflow_id'] = saved['workflow_id']
            replacement = [effect if v['id'] == identifier else v for v in layers]
            self.layer_panel.set_layers(replacement)
            self.layer_panel.select_layer_id(identifier)
            if not self.persist():
                self.state = before
                self.layer_panel.set_layers(layers)
                self.layer_panel.select_layer_id(identifier)
                self.reset_editor_history()
                raise ValueError('工作流已保存，但控件替换未保存；原控件已恢复。可在工作流库找到“'+str(name)+'”。')
            self.invalidate_playback()
            self.update_preview()
            self._log('已将“'+current['name']+'”转为节点，保留参数与图层位置；播放预览查看效果。Ctrl+Z 可撤销转换。')
            # Opening failure must not roll back an already committed conversion.
            try:
                services.open_workflow_editor(workflow_id=saved['workflow_id'], workflow_type=TYPE_ID)
            except Exception as error:
                self._log('转换已保存；打开编辑器失败，可从节点效果页重试：'+str(error))
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, '控件转换为节点', str(error))
