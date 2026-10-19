"""Plugin-owned workflow contracts; business rules do not live in the host."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class WorkflowTypeDefinition:
    type_id: str
    title: str
    entry_kind: str
    output_kind: str
    version: int = 1
    example_factory: object = None
    preview_factory: object = None
    description: str = ''
    migrate_graph: object = None
    execution_mode: str = 'detail'
    preview_task_factory: object = None
    preview_plan_factory: object = None


def register_type(registry, owner, definition):
    if (not isinstance(definition, WorkflowTypeDefinition)
            or not re.fullmatch(r'[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*', definition.type_id)
            or not definition.type_id.startswith(owner + '.')
            or type(definition.version) is not int or definition.version < 1
            or not isinstance(definition.title, str) or not definition.title or len(definition.title) > 120
            or not all(kind.startswith(owner + '.') and kind in registry.definitions
                       for kind in (definition.entry_kind, definition.output_kind))
            or not callable(definition.example_factory)
            or definition.preview_factory is not None and not callable(definition.preview_factory)
            or definition.execution_mode not in {'detail', 'line'}
            or definition.execution_mode == 'line' and definition.preview_factory is not None and not callable(definition.preview_task_factory)
            or definition.preview_task_factory is not None and not callable(definition.preview_task_factory)
            or definition.preview_plan_factory is not None and definition.preview_factory is None
            or definition.preview_plan_factory is not None and not callable(definition.preview_plan_factory)
            or definition.migrate_graph is not None and not callable(definition.migrate_graph)):
        raise ValueError('工作流类型必须有插件命名空间、版本、已注册的输入输出节点及示例接口；提供预览时还需有效的预览接口。')
    if definition.type_id in registry.workflow_types:
        raise ValueError('工作流类型重复：' + definition.type_id)
    registry.workflow_types[definition.type_id] = definition
    registry.workflow_type_owners[definition.type_id] = owner


def upgrade_workflow(graph, registry):
    """Provider-owned in-memory upgrade; never rewrites the user's saved file."""
    value = registry.workflow_types.get(graph.get('workflow_type', ''))
    if value is not None and value.migrate_graph is not None:
        return value.migrate_graph(graph)
    return graph


def selected_type(graph, registry):
    key = graph.get('workflow_type', '')
    if not key:
        return None
    value = registry.workflow_types.get(key)
    if value is None:
        raise ValueError('工作流类型提供者不可用：' + str(key) + '；请启用对应插件。')
    if type(graph.get('workflow_type_version')) is not int or graph.get('workflow_type_version') != value.version:
        raise ValueError('工作流类型接口版本不兼容；请在节点面板重新创建。')
    if graph.get('execution_mode', 'detail') != value.execution_mode:
        raise ValueError('此插件工作流的执行范围不匹配，请选择对应的工作流类型。')
    return value


def output_kinds(graph, registry):
    from .graph import OUTPUT_KINDS
    value = selected_type(graph, registry)
    return {value.output_kind} if value else OUTPUT_KINDS


def can_preview_operations(graph, registry, node):
    """Only providers with a declared simulator expose direct operation previews."""
    try:
        provider = selected_type(graph, registry)
        definition = registry.definitions.get(node['kind'])
        return bool(provider and provider.preview_factory and provider.preview_plan_factory and definition and any(
            port.data_type == 'operations' for port in definition.result_ports(node['params'])))
    except (ValueError, TypeError, KeyError):
        return False


def preview_available(graph, registry):
    """A plugin may register and run a workflow before adding simulation."""
    try:
        provider = selected_type(graph, registry)
    except ValueError:
        return False
    return provider is None or provider.preview_factory is not None
