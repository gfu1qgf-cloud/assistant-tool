"""Edge-relative editing, while keeping the shared renderer's center geometry."""
ANCHORS = {"x": ("center", "left", "right"), "y": ("center", "top", "bottom")}


def normalize_anchors(layer):
    for axis, choices in ANCHORS.items():
        key = "position_anchor_" + axis
        if key in layer and layer[key] not in choices:
            raise ValueError("位置基准无效，请选择框中心或对应画面边缘。")


def anchor(layer, axis):
    return layer.get("position_anchor_" + axis, "center")


def distance(layer, axis):
    value = layer[axis]
    extent = layer["width" if axis == "x" else "height"]
    side = anchor(layer, axis)
    if side in {"left", "top"}:
        return value - extent / 2
    if side in {"right", "bottom"}:
        return 100 - value - extent / 2
    return value


def center(layer, axis, value):
    extent = layer["width" if axis == "x" else "height"]
    side = anchor(layer, axis)
    if side in {"left", "top"}:
        return value + extent / 2
    if side in {"right", "bottom"}:
        return 100 - value - extent / 2
    return value


def distance_range(layer, axis):
    extent = layer["width" if axis == "x" else "height"]
    return (-extent / 2, 100 - extent / 2) if anchor(layer, axis) != "center" else (0, 100)
