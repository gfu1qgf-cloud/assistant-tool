"""Edge-limited RGB correction; never modify the compositing alpha.

The soft band is derived from source-space alpha, not a semantic foreground
guess. Native video uses gray morphology and RGB expressions in FFmpeg; Qt
uses the same math for the editing poster. Protected trimap colours win last.
"""
import math

DEFAULTS = {"key_despill_mode": "basic", "key_despill_amount": 100.0,
            "key_despill_width": 4, "key_despill_luma": 50.0}


def normalize_despill(layer):
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    if layer["key_despill_mode"] not in {"basic", "edge"}:
        raise ValueError("去色溢方式必须是基础或边缘遮罩。")
    for key in ("key_despill_amount", "key_despill_luma"):
        value = layer[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("去色溢强度和亮度补偿必须为0–100%。")
    value = layer["key_despill_width"]
    if type(value) is not int or not 1 <= value <= 12:
        raise ValueError("去色溢边缘范围必须为1–12个源素材像素。")


def enabled(layer):
    from .chroma_key import despill_type
    return (layer.get("key_mode", "none") != "none" and layer.get("key_despill", False)
            and layer.get("key_despill_mode", "basic") == "edge"
            and layer.get("key_despill_amount", 100) > 0 and bool(despill_type(layer)))


def edge_mask(alpha, layer):
    """uint8 alpha -> correction band; replicated erosion, reflected blur."""
    import cv2
    import numpy as np
    radius = layer.get("key_despill_width", 4)
    core = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=radius,
                     borderType=cv2.BORDER_REPLICATE)
    # Squared alpha gives fine semi-transparent strands enough correction.
    values = np.arange(256, dtype=np.float64)
    table = np.floor(255 * (1 - (values/255)**2)).astype(np.uint8)
    band = table[core]
    blur = min(radius, min(alpha.shape)//2)
    soft = cv2.blur(band, (blur*2+1, blur*2+1), borderType=cv2.BORDER_REFLECT)
    gain = np.floor(np.minimum(255, values*1.5)).astype(np.uint8)
    return gain[soft]


def correct_rgb(data, layer, protection):
    import numpy as np
    from .chroma_key import despill_type
    mask = edge_mask(data[:, :, 3], layer).astype(np.float32)/255
    rgb = data[:, :, :3].astype(np.float32)
    channel = 1 if despill_type(layer) == "green" else 2
    others = [i for i in range(3) if i != channel]
    excess = np.maximum(0, rgb[:, :, channel] - (rgb[:, :, others[0]]+rgb[:, :, others[1]])/2)
    excess *= mask
    excess *= layer.get("key_despill_amount", 100)/100
    # A neutral addition restores part of the removed luminance while leaving
    # the corrected channel difference intact. Clipping is bounded to RGB8.
    luma = (.587 if channel == 1 else .114) * layer.get("key_despill_luma", 50)/100
    # Process planes separately to bound memory on a 4096px editing poster.
    for index in range(3):
        cleaned = rgb[:, :, index] + excess*(luma-(index == channel))
        np.clip(cleaned, 0, 255, out=cleaned)
        np.floor(cleaned, out=cleaned)
        cleaned *= 1-protection
        cleaned += rgb[:, :, index]*protection
        data[:, :, index] = np.floor(cleaned).astype(np.uint8)


def add_native(filters, layer, rgb, alpha, protection, root):
    """Consume RGB, mask alpha and optional protection; return corrected RGB."""
    from .chroma_key import despill_type
    radius = layer.get("key_despill_width", 4)
    morph = ",".join(["erosion=coordinates=255"]*radius)
    filters.append(f"[{alpha}]{morph},lut=y='255*(1-pow(val/255,2))',"
        f"boxblur=lr='min({radius},floor(min(w,h)/2))':lp=1,"
        f"lut=y='min(255,val*1.5)',format=gray[{root}_band]")
    if protection:
        filters.append(f"[{rgb}]split=2[{root}_input][{root}_saved]")
        rgb = root + "_input"
    filters.append(f"[{rgb}][{root}_band]alphamerge,format=gbrap[{root}_masked]")
    channels = {c: f"{c}(X,Y)" for c in ("r", "g", "b")}
    channel = "g" if despill_type(layer) == "green" else "b"
    others = [c for c in channels if c != channel]
    delta = (f"max(0,{channels[channel]}-({channels[others[0]]}+{channels[others[1]]})/2)"
             f"*alpha(X,Y)/255*{layer.get('key_despill_amount', 100)/100:.9f}")
    luma = (.587 if channel == "g" else .114) * layer.get("key_despill_luma", 50)/100
    expr = ":".join(f"{c}='clip({value}+({delta})*{luma-(c == channel):.9f},0,255)'"
                    for c, value in channels.items())
    filters.append(f"[{root}_masked]geq={expr}:a=255:interpolation=nearest,format=gbrp[{root}_clean]")
    if protection:
        filters.append(f"[{root}_saved]format=gbrp[{root}_original]")
        filters.append(f"[{protection}]format=gbrp[{root}_protect]")
        filters.append(f"[{root}_clean][{root}_original][{root}_protect]maskedmerge=planes=7[{root}_result]")
        return root + "_result"
    return root + "_clean"
