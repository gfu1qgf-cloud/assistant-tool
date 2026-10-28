"""GUI-only adapters. The shared executor never imports Qt/widget modules."""
from dataclasses import replace


def install_core_inspectors(registry):
    from .node_code_editor import CodeNodeEditor
    from .node_rgb_editor import RgbEditor
    from .node_lut_editor import LutEditor
    from .node_subgraph_editor import SubgraphInspector
    from .node_compression_ui import CompressionInspector
    for kind, factory in (('core.python_code', CodeNodeEditor), ('core.rgb', RgbEditor),
                          ('core.lut', LutEditor), ('core.subgraph', SubgraphInspector),
                          ('core.compress',CompressionInspector)):
        if kind in registry.definitions:
            registry.definitions[kind] = replace(registry.definitions[kind], inspector_factory=factory)
