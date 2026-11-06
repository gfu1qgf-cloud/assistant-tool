"""Explicit, header-based business schemas. Worksheet prose is never executable."""
IMAGE_FIELDS = [
    ('reference', '文件名', ('原图链接',)), ('image_size', '尺寸', ()), ('subcategory', '子类别', ()),
    ('requirements', '画面/文案要点', ()), ('notes', '备注', ()), ('status', '结果', ()),
    ('result', '视频链接', ()), ('has_jesus', '是否有耶稣', ()), ('black_keywords', '黑色关键字', ()),
    ('task_id', '任务编号', ()), ('job_id', '任务ID', ()), ('heartbeat', '最后心跳', ()),
    ('generation_attempts', '生成尝试次数', ()), ('quality', '质检状态', ()),
    ('archive', '下载状态', ()), ('error', '最后错误', ()), ('user_requirements', '用户需求', ()),
]
IMAGE_REQUIRED = {'job_id', 'task_id', 'reference', 'status', 'result', 'archive'}
ORAL_DONE = {'已完成', '完成', '制作完成', '生成完成', 'completed', 'complete', 'done'}
ORAL_USER_FILL = 'AI已完成，需用户补段'
IMAGE_DONE = {'已交付'}
ARCHIVED = {'已下载', '已归档'}


def task_kind(payload): return 'image' if payload.get('task_kind') == 'image' else 'oral'


def completed(kind, status):
    return str(status or '').strip().casefold() in (IMAGE_DONE if kind == 'image' else ORAL_DONE)


def needs_user_segments(kind, status):
    """AI handoff permits receipt, never complete-task acknowledgement."""
    return kind=='oral' and str(status or '').strip().casefold()==ORAL_USER_FILL.casefold()


def archive_value(kind, options=None):
    """Respect both the current oral table and legacy/image dropdowns."""
    choices = ('已下载', '已归档') if kind == 'image' else ('已归档', '已下载')
    if options is None: return choices[0]
    for value in choices:
        if value in options: return value
    raise ValueError('归档下拉框不含“已归档”或“已下载”，未修改格式或验证。')
