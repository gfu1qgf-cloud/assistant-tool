"""Keep the author's plain-spoken prompt style; vision only selects a family."""

SCENE_LABELS = {
    "person": "单个人物", "group": "多人/队伍", "poster": "带文字的画面",
    "statue": "雕像/圣像", "landscape": "风景/场景", "object": "物品/符号",
    "unknown": "暂不确定",
}


def clean_scene(value):
    return value if value in SCENE_LABELS else "unknown"


def build_suggestions(scene="unknown", subject="", has_text=False,
                      expression="unknown", full_body=False):
    scene = clean_scene(scene)
    subject = " ".join(str(subject or "").split())[:40]
    if not subject:
        subject = {
            "person": "画面中的人物", "group": "画面中的人们",
            "poster": "画面中的元素", "statue": "画面中的雕像",
            "landscape": "画面中的景物", "object": "画面中的主体",
        }.get(scene, "画面中的主体")
    text_rule = "画面中的文字部分不要变，不要出现任何额外的字和字幕。" if has_text or scene == "poster" else ""
    if scene == "poster":
        bodies = [
            ("稳妥微动", f"镜头景别不要变，镜头位置不要变。{text_rule}让画面鲜活起来，{subject}可以有轻微动画，但不要额外添加元素。要始终保持画面中文字的可读性。"),
            ("元素动画", f"镜头景别不要变，镜头位置不要变。{text_rule}{subject}可以自然地动起来，只在现有的画面元素上做动画。要始终保持画面中文字的可读性。"),
            ("轻微气氛", f"镜头景别不要变，镜头位置不要变。{text_rule}画面有轻微的光影和粒子变化，不要额外添加元素。要始终保持画面中文字的可读性。"),
        ]
    elif scene == "group":
        bodies = [
            ("稳妥前行", f"{subject}向前行进。镜头保持固定距离跟随，画面要鲜活。"),
            ("跟随运镜", f"{subject}向前行进，镜头始终跟随。可以适当改变角度，展现整个场景。"),
            ("宏大场面", f"{subject}向前行进。镜头自由运镜，展现宏大场景，但不要改变人物和场景原有的样子。"),
        ]
    elif scene == "person":
        face = {"smile": "面带微笑", "tears": "默默流泪",
                "solemn": "面容凝重"}.get(expression, "保持原有表情")
        movement = (
            f"{subject}{face}向前行走。镜头保持固定距离始终跟随，人物全身保持在画面中。"
            if full_body else
            f"{subject}{face}保持姿势。镜头缓缓推近，不要改变人物原有的样子。"
        )
        bodies = [
            ("稳妥微动", f"{subject}{face}保持姿势。微弱的风吹拂，头发微动但发型不乱，衣服保持整齐美观。{subject}全程不说话。"),
            ("自然动作", f"{movement}{subject}全程不说话。"),
            ("环绕镜头", f"{subject}{face}保持姿势。镜头保持固定半径缓慢环绕，衣服保持整齐美观。{subject}全程不说话。"),
        ]
    elif scene == "statue":
        bodies = [
            ("稳妥微动", f"{subject}保持原样，只有轻微的光影变化，画面要鲜活。"),
            ("环绕镜头", f"镜头保持固定半径，缓慢环绕到{subject}的另一侧。{subject}不要变形。"),
            ("缓慢推近", f"镜头从远处缓缓推近{subject}，保持原来的外观和周围环境。"),
        ]
    elif scene == "landscape":
        bodies = [
            ("稳妥微动", f"{subject}缓慢流动，画面保持原来的样子，镜头位置不要变。"),
            ("缓慢推近", f"镜头从远处慢慢靠近{subject}，画面要鲜活，不要额外添加元素。"),
            ("缓慢拉远", f"镜头从{subject}慢慢拉远，展现周围的场景，不要改变画面原有的风格。"),
        ]
    else:
        bodies = [
            ("稳妥微动", f"{subject}保持原来的样子，画面有轻微的自然动态。镜头位置不要变，不要额外添加元素。"),
            ("缓慢推近", f"镜头缓缓推近{subject}。保持原始内容不变，动作轻缓自然。"),
            ("轻微环绕", f"镜头保持固定半径，缓慢环绕{subject}。保持原始内容不变，不要让主体变形。"),
        ]
    if text_rule and scene != "poster":
        bodies = [(title, f"{body} {text_rule}") for title, body in bodies]
    return bodies
