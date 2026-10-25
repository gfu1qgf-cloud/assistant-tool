"""Colour-key settings and native contour protection shared by every renderer.

This is deterministic region analysis, NOT a human-recognition model. Enclosed
key-coloured regions may be props or genuine holes; an optional trimap resolves
that ambiguity without silently guessing semantic identity.
"""
from pathlib import Path
import re
from .chroma_despill import DEFAULTS as DESPILL_DEFAULTS, normalize_despill
from .chroma_cleanup import DEFAULTS as CLEANUP_DEFAULTS, normalize_cleanup
from .matte_geometry import DEFAULTS as GEOMETRY_DEFAULTS, normalize as normalize_geometry

KEY_COLORS = {"green": (0, 255, 0), "blue": (0, 0, 255), "black": (0, 0, 0)}
DEFAULTS = {"key_method": "normal", "key_color": "#00ff00",
            "key_protect_inside": True, "key_mask_path": "", **DESPILL_DEFAULTS, **CLEANUP_DEFAULTS, **GEOMETRY_DEFAULTS}
COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}\Z")
MASK_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
                          ".gif", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})


def normalize_key(layer):
    normalize_despill(layer)
    normalize_cleanup(layer)
    normalize_geometry(layer)
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    if layer.get("key_mode", "none") not in {"none", "custom", *KEY_COLORS}:
        raise ValueError("颜色抠像预设无效。")
    if layer["key_method"] not in {"normal", "enhanced", "ai"}:
        raise ValueError("抠像算法必须是普通、增强或AI主体抠像。")
    if not isinstance(layer["key_color"], str) or not COLOR_PATTERN.fullmatch(layer["key_color"]):
        raise ValueError("抠像基准颜色须为 #RRGGBB，例如 #00ff00。")
    layer["key_color"] = layer["key_color"].lower()
    if type(layer["key_protect_inside"]) is not bool:
        raise ValueError("轮廓内同色区域保护开关无效。")
    path = layer["key_mask_path"]
    if not isinstance(path, str) or len(path) > 32767 or any(c in path for c in ("\x00", "\r", "\n")):
        raise ValueError("辅助遮罩路径无效。")


def key_color(layer):
    mode = layer.get("key_mode", "none")
    if mode in KEY_COLORS:
        return KEY_COLORS[mode]
    value = layer.get("key_color", DEFAULTS["key_color"])
    if not isinstance(value, str) or not COLOR_PATTERN.fullmatch(value):
        raise ValueError("抠像基准颜色须为 #RRGGBB。")
    return tuple(int(value[index:index+2], 16) for index in (1, 3, 5))


def despill_type(layer):
    """Only correct the matching dominant green/blue screen hue."""
    r, g, b = key_color(layer)
    if g > max(r, b) + 32:
        return "green"
    if b > max(r, g) + 32:
        return "blue"
    return ""


def despill_filter(layer):
    """Same conservative max-other-channel rule as the Qt poster.

    FFmpeg's despill default compares to a weighted mean, changing skin/ivory
    even when the key channel is not dominant. Its default correction channel
    also stays green when type=blue. Use an explicit native RGB expression;
    this runs in FFmpeg, not a Python per-video-frame loop.
    """
    spill = despill_type(layer) if layer.get("key_despill") else ""
    if not spill or layer.get("key_despill_mode", "basic") != "basic":
        return ""
    amount = layer.get('key_despill_amount', 100)/100
    if not amount: return ''
    channels = {name: f"{name}(X,Y)" for name in ("r", "g", "b")}
    channel, others = ("g", ("r", "b")) if spill == "green" else ("b", ("r", "g"))
    delta = f"max(0,{channel}(X,Y)-max({others[0]}(X,Y),{others[1]}(X,Y)))*{amount:.9f}"
    luma = (.587 if channel == 'g' else .114)*layer.get('key_despill_basic_luma', 0)/100
    if amount == 1 and luma == 0:
        channels[channel] = f"min({channel}(X,Y),max({others[0]}(X,Y),{others[1]}(X,Y)))"
    else:
        channels = {name: (value if not luma and name != channel else
                    f'clip({value}+({delta})*{luma-(name == channel):.9f},0,255)') for name, value in channels.items()}
    return "geq=" + ":".join(f"{key}='{value}'" for key, value in channels.items()) + ":a='alpha(X,Y)':interpolation=nearest"


def active_mask(layer):
    return (layer.get("key_mask_path", "") if layer.get("key_mode", "none") != "none"
            and layer.get("key_method", "normal") == "enhanced" else "")


def validate_mask(layer):
    value = active_mask(layer)
    if value:
        path = Path(value)
        if not path.is_file() or path.suffix.casefold() not in MASK_SUFFIXES:
            raise ValueError("辅助遮罩不存在或格式不支持：" + str(path))
    return value


def inside_protection(alpha):
    """Match padded FFmpeg floodfill: 4-connected background from ALL borders."""
    import cv2
    import numpy as np
    # Alpha is normalized using the same uint8 quantization as the native path.
    raw = np.floor(np.clip(alpha, 0, 1)*255).astype(np.uint8)
    candidate = (raw < 254).astype(np.uint8)
    padded = np.pad(candidate, 1, constant_values=1)
    cv2.floodFill(padded, None, (0, 0), 2, flags=4)
    return (padded[1:-1, 1:-1] == 1).astype(np.float32)


def trimap_channels(rgba):
    """Black removes, exact mid-gray 128 is neutral, white protects; honor alpha."""
    import numpy as np
    rgb = rgba[:, :, :3].astype(np.float32)
    # FFmpeg RGB->gray uses the same BT.601 luminance convention.
    gray = np.rint(rgb[:, :, 0]*.299 + rgb[:, :, 1]*.587 + rgb[:, :, 2]*.114)
    opacity = rgba[:, :, 3].astype(np.float32)/255
    return (np.clip((gray-128)/127, 0, 1)*opacity,
            np.clip((128-gray)/128, 0, 1)*opacity)


def enhanced_alpha(raw, layer, trimap=None):
    import numpy as np
    from .chroma_matte import remap
    protection = inside_protection(raw) if layer.get("key_protect_inside", True) else np.zeros_like(raw)
    def quantize(value):
        return np.floor(value*255)/255 if layer.get("key_cleanup", False) else value
    from .matte_geometry import shift
    alpha = np.maximum(quantize(shift(remap(raw, layer), layer.get('key_matte_grow', 0))), protection)
    if trimap is not None:
        keep, remove = trimap_channels(trimap)
        keep, remove = quantize(keep), quantize(remove)
        alpha = quantize(alpha*(1-remove))
        alpha = quantize(alpha + (1-alpha)*keep)
        protection = quantize(protection*(1-remove))
        protection = quantize(protection + (1-protection)*keep)
    return alpha, protection


def add_keyed_source(args, filters, layer, input_index, label, fps, is_animated, *, duration):
    """Native RGBA/trimap compositing, no Python video loop or intermediate file.

    Returns (output pad, consumed input count); caller already added source -i.
    Native source alpha and user opacity remain separate. Despill must not
    recolour protected green/blue decorations.
    """
    from .chroma_matte import key_filters, levels
    from .chroma_despill import enabled, add_native
    from .matte_geometry import add_native as add_shift
    edge = enabled(layer)
    cleanup = layer.get("key_cleanup", False)
    cleanup_protection = None
    enhanced = layer.get("key_method", "normal") == "enhanced"
    mask = validate_mask(layer)
    consumed = 1
    root = label + "_key"
    split = (f"split=3[{root}_original][{root}_native_alpha_source][{root}_input]" if enhanced else
             f"split=2[{root}_native_alpha_source][{root}_input]")
    filters.append(f"[{input_index}:v]setpts=PTS-STARTPTS,trim=duration={duration:.9f},fps={float(fps):.9f},format=rgba,"
                   + split)
    filters.append(f"[{root}_native_alpha_source]alphaextract,format=gray[{root}_native_mask]")
    if not enhanced:
        filters.append(f"[{root}_input]" + ",".join(key_filters(layer)) +
                       f",split=2[{root}_rgb][{root}_alpha_source]")
        filters.append(f"[{root}_alpha_source]alphaextract,format=gray[{root}_unshifted]")
        add_shift(filters, root+'_unshifted', root+'_final_alpha', layer.get('key_matte_grow', 0))
        color_pad = root + "_rgb"
    else:
        spill = (despill_type(layer) if layer.get("key_despill") and layer.get('key_despill_amount', 100) > 0 and
                 (edge or layer.get("key_despill_mode", "basic") == "basic") else "")
        raw_layer = dict(layer, key_alpha_low=0, key_alpha_high=100, key_despill=False)
        filters.append(f"[{root}_input]" + ",".join(key_filters(raw_layer)) +
                       f",alphaextract,format=gray,split=2[{root}_raw][{root}_regions]")
        low, high = levels(layer)
        filters.append(f"[{root}_raw]lut=y='clip((val-{low*2.55:.9f})*"
                       f"{100/(high-low):.9f},0,255)',format=gray[{root}_unshifted]")
        add_shift(filters, root+'_unshifted', root+'_base_alpha', layer.get('key_matte_grow', 0))
        if layer.get("key_protect_inside", True):
            # Pad connects every image border; unlike corner-only seeding this
            # works even if a foreground body touches one or several edges.
            filters.append(f"[{root}_regions]lut=y='if(lt(val,254),0,255)',"
                "pad=iw+2:ih+2:1:1:color=black,"
                "floodfill=x=0:y=0:s0=0:d0=128,crop=iw-2:ih-2:1:1,"
                f"lut=y='if(eq(val,0),255,0)',format=gray[{root}_auto]")
        else:
            filters.append(f"[{root}_regions]lut=y=0,format=gray[{root}_auto]")
        needs_protection = bool(spill) or cleanup
        if needs_protection:
            filters.append(f"[{root}_auto]split=2[{root}_protect0][{root}_keep0]")
        else:
            filters.append(f"[{root}_auto]null[{root}_keep0]")
        filters.append(f"[{root}_base_alpha][{root}_keep0]blend=all_mode=lighten,format=gray[{root}_alpha0]")
        original_pad = root + "_original"
        if mask:
            if is_animated(mask):
                args += ["-stream_loop", "-1", "-i", mask]
            else:
                args += ["-loop", "1", "-i", mask]
            consumed += 1
            filters.append(f"[{input_index+1}:v]setpts=PTS-STARTPTS,trim=duration={duration:.9f},fps={float(fps):.9f},"
                           f"format=rgba[{root}_trimap_input]")
            filters.append(f"[{root}_trimap_input][{original_pad}]scale2ref=w=iw:h=ih:"
                           f"flags=bilinear[{root}_trimap][{root}_source_rgb]")
            original_pad = root + "_source_rgb"
            filters.append(f"[{root}_trimap]format=rgba,split=2[{root}_trimap_rgb][{root}_trimap_alpha]")
            filters.append(f"[{root}_trimap_rgb]format=gray,split=2[{root}_white0][{root}_black0]")
            filters.append(f"[{root}_trimap_alpha]alphaextract,format=gray,split=2[{root}_white_opacity][{root}_black_opacity]")
            filters.append(f"[{root}_white0]lut=y='clip((val-128)*255/127,0,255)',format=gray[{root}_white1]")
            filters.append(f"[{root}_black0]lut=y='clip((128-val)*255/128,0,255)',format=gray[{root}_black1]")
            for part in ("white", "black"):
                branches = (f",split=2[{root}_{part}_alpha][{root}_{part}_protect]" if needs_protection else
                            f"[{root}_{part}_alpha]")
                filters.append(f"[{root}_{part}1][{root}_{part}_opacity]blend=all_mode=multiply,format=gray" + branches)
            targets = (("alpha", "alpha0"), ("protect", "protect0")) if needs_protection else (("alpha", "alpha0"),)
            for part, source in targets:
                filters.append(f"[{root}_{source}][{root}_black_{part}]blend="
                               f"all_expr='A*(1-B/255)',format=gray[{root}_{part}_minus]")
                filters.append(f"[{root}_{part}_minus][{root}_white_{part}]blend="
                               f"all_expr='A+(255-A)*B/255',format=gray[{root}_{part}]")
            alpha_pad, protection_pad = root + "_alpha", root + "_protect"
        else:
            alpha_pad, protection_pad = root + "_alpha0", root + "_protect0"
        filters.append(f"[{alpha_pad}]format=gray[{root}_final_alpha]")
        if cleanup:
            if spill:
                filters.append(f"[{protection_pad}]split=2[{root}_color_protect][{root}_cleanup_protect]")
                protection_pad, cleanup_protection = root + "_color_protect", root + "_cleanup_protect"
            else:
                cleanup_protection = protection_pad
        if spill and not edge:
            filters.append(f"[{original_pad}]split=2[{root}_clean_input][{root}_saved_rgb]")
            filters.append(f"[{root}_clean_input]{despill_filter(layer)},format=gbrp[{root}_clean_rgb]")
            filters.append(f"[{root}_saved_rgb]format=gbrp[{root}_saved]")
            filters.append(f"[{protection_pad}]format=gbrp[{root}_colour_protect]")
            filters.append(f"[{root}_clean_rgb][{root}_saved][{root}_colour_protect]"
                           f"maskedmerge=planes=7[{root}_rgb]")
            color_pad = root + "_rgb"
        else:
            color_pad = original_pad
    filters.append(f"[{root}_native_mask][{root}_final_alpha]blend=all_mode=multiply:shortest=1,format=gray[{root}_combined_mask]")
    combined_mask = root + "_combined_mask"
    if cleanup:
        from .chroma_cleanup import add_native as add_cleanup
        combined_mask = add_cleanup(filters, layer, combined_mask, cleanup_protection, root + "_cleanup")
    if edge:
        filters.append(f"[{combined_mask}]split=2[{root}_output_alpha][{root}_edge_alpha]")
        color_pad = add_native(filters, layer, color_pad, root + "_edge_alpha",
                               protection_pad if enhanced else None, root + "_despill")
        combined_mask = root + "_output_alpha"
    filters.append(f"[{color_pad}][{combined_mask}]alphamerge=shortest=1,format=rgba[{root}_source]")
    return root + "_source", consumed
