"""Copy the complete TaskData row, never live objects or application settings."""
import json


# Keep this list aligned with OdsHelper.TaskData. Labels/types also describe the
# field-level node ports; the snapshot itself stays inside the delivery job.
TASK_DATA_FIELDS = (
    ("task_id", "任务编号", "text"),
    ("task_name", "任务名称（文件名用）", "text"),
    ("_full_task_name", "完整任务名称（未裁剪）", "text"),
    ("task_date", "任务日期", "text"),
    ("task_type", "任务类型", "text"),
    ("submission_task_type", "交付类型", "text"),
    ("admin", "管理员", "text"),
    ("creator", "制作人", "text"),
    ("task_audio_text", "任务文案", "text"),
    ("task_audio_type", "语音类型", "text"),
    ("task_audio_voice_id", "语音 ID", "text"),
    ("task_requirements", "任务需求", "text"),
    ("task_notes", "备注", "text"),
    ("task_video_prompt", "视频生成提示词（sora2等）", "text"),
    ("task_resolve_requirement", "达芬奇需求（原文）", "text"),
    ("task_reference_link", "参考链接", "text"),
    ("review_required", "需要审核（表格标记）", "boolean"),
    ("review_required_text", "审核标记原文", "text"),
    ("subcategory", "子类别（规范化路径）", "text"),
    ("subcategory_text", "子类别原文", "text"),
    ("daily_stat_sheet", "统计分页", "text"),
    ("daily_stat_category", "统计类别", "text"),
    ("source_row", "表格行号", "number"),
    ("task_type_from_table", "任务类型来自表格", "boolean"),
    ("submission_task_type_from_table", "交付类型来自表格", "boolean"),
    ("defaulted_fields", "使用默认值的字段", "list"),
    ("raw", "原始表格字段（含自定义列）", "mapping"),
)


def snapshot_task_data(task):
    """JSON-shaped, detached data; preserve unknown/empty review markers as None.

    Only declared TaskData attributes are copied, including its original column
    dictionary. Other attributes on a caller's object are not application data
    and must not accidentally expose credentials or a live service to nodes.
    """
    if task is None:
        return None
    source = task if isinstance(task, dict) else vars(task)
    fields = {key: source[key] for key, _title, _kind in TASK_DATA_FIELDS if key in source}
    defaults = fields.get("defaulted_fields")
    if isinstance(defaults, (set, frozenset)):
        fields["defaulted_fields"] = sorted(defaults)
    # Also detaches nested raw columns/lists and rejects non-serializable live
    # objects. Neither source TaskData nor its nested data is ever modified.
    return json.loads(json.dumps(fields, ensure_ascii=False, allow_nan=False))
