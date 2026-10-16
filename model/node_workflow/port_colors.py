"""One palette for input pins, output pins and wires; type text remains visible."""
TYPE_COLORS = {
    'integer': '#6aa9ff', 'float': '#b28dff', 'number': '#a696d9',
    'text': '#66cc88', 'boolean': '#f0b46a', 'color': '#e8cd68',
    **{kind: '#60c9c9' for kind in ('list', 'mapping', 'data', 'directory', 'files', 'resource_pool', 'task')},
    **{kind: '#dd89b4' for kind in ('video', 'audio', 'subtitles', 'operations', 'render_plan')},
    'loop_block': '#d9a1bf', 'loop_results': '#60c9c9', 'any': '#9aa5b1',
}


def color_for(kind):
    return TYPE_COLORS.get(kind, TYPE_COLORS['any'])
