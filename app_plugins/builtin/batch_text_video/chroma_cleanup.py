"""Remove detached thin components by opening by reconstruction, not erosion.

A component survives if it contains a sufficiently thick core or a protected
pixel. Reconstructing within the original support restores attached fine hair,
holes and original edge alpha. There is no largest-component assumption.
"""
DEFAULTS = {"key_cleanup": False, "key_cleanup_size": 2}


def normalize_cleanup(layer):
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    if type(layer["key_cleanup"]) is not bool:
        raise ValueError("去碎块开关无效。")
    size = layer["key_cleanup_size"]
    if type(size) is not int or not 1 <= size <= 12:
        raise ValueError("碎块尺度必须为1–12个源素材像素。")


def clean_alpha(alpha, layer, protection=None):
    """Return uint8 alpha; retained components keep every original alpha value."""
    import cv2
    import numpy as np
    if not layer.get("key_cleanup", False):
        return alpha
    support = (alpha > 0).astype(np.uint8)
    core = cv2.erode(support, np.ones((3, 3), np.uint8),
                     iterations=layer.get("key_cleanup_size", 2), borderType=cv2.BORDER_REPLICATE)
    if protection is not None:
        core |= ((protection > 0) & (support > 0)).astype(np.uint8)
    count, labels = cv2.connectedComponents(support, connectivity=8)
    keep = np.zeros(count, dtype=bool)
    keep[np.unique(labels[core > 0])] = True
    keep[0] = False
    return np.where(keep[labels], alpha, 0).astype(np.uint8)


def add_native(filters, layer, alpha, protection, root):
    """FFmpeg binary reconstruction, then select the unchanged original alpha."""
    morph = ",".join(["erosion=coordinates=255"]*layer.get("key_cleanup_size", 2))
    filters.append(f"[{alpha}]split=2[{root}_original][{root}_support_input]")
    filters.append(f"[{root}_support_input]lut=y='if(gt(val,0),255,0)',"
                   f"split=2[{root}_support][{root}_core_input]")
    filters.append(f"[{root}_core_input]{morph}[{root}_core]")
    seed = root + "_core"
    if protection:
        filters.append(f"[{protection}]lut=y='if(gt(val,0),255,0)'[{root}_protected]")
        filters.append(f"[{seed}][{root}_protected]blend=all_mode=lighten[{root}_seed]")
        seed = root + "_seed"
    filters.append(f"[{seed}][{root}_support]hysteresis=planes=1:threshold=0:shortest=1[{root}_keep]")
    filters.append(f"[{root}_original][{root}_keep]blend=all_expr='if(gt(B,0),A,0)':shortest=1,"
                   f"format=gray[{root}_result]")
    return root + "_result"
