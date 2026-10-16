"""Local, app-scoped font resources and honest weight matching feedback."""
from pathlib import Path
from functools import lru_cache
import struct
from qt_compat import QtCore, QtGui


POPPINS_FILES = tuple(
    "Poppins-" + style + ".ttf"
    for style in (
        "Thin", "ThinItalic", "ExtraLight", "ExtraLightItalic", "Light", "LightItalic",
        "Regular", "Italic", "Medium", "MediumItalic", "SemiBold", "SemiBoldItalic",
        "Bold", "BoldItalic", "ExtraBold", "ExtraBoldItalic", "Black", "BlackItalic",
    )
)
_loaded_application = None
OPEN_SANS_FILES = ('OpenSans[wdth,wght].ttf', 'OpenSans-Italic[wdth,wght].ttf')
BUNDLED_FONTS = {'poppins': POPPINS_FILES, 'opensans': OPEN_SANS_FILES}
POPPINS_STYLES = dict(zip(range(100, 1000, 100),
    ("Thin", "ExtraLight", "Light", "Regular", "Medium", "SemiBold", "Bold", "ExtraBold", "Black")))


def _read_weight_axis(table):
    """Read bounded OpenType fvar metadata; never scan files or install fonts.

    Qt supplies the selected face's actual table, including TTC collections and
    Windows per-user fonts. Only small axis records are needed, not instances.
    """
    if not table:
        return None
    if len(table) < 16 or len(table) > 2*1024*1024:
        raise ValueError("可变字体轴表长度无效")
    major, minor, offset, _reserved, count, size, _instances, _instance_size = struct.unpack_from('>8H', table)
    if major != 1 or minor != 0 or not 1 <= count <= 64 or size < 20 or offset < 16:
        raise ValueError("可变字体轴表格式暂不支持")
    if offset+count*size > len(table):
        raise ValueError("可变字体轴表不完整")
    tags, weight_axis = set(), None
    for index in range(count):
        tag, minimum, default, maximum, _flags, _name = struct.unpack_from('>4siiiHH', table, offset+index*size)
        if tag in tags or not minimum <= default <= maximum:
            raise ValueError("可变字体轴定义无效")
        tags.add(tag)
        if tag == b'wght':
            weight_axis = (minimum/65536, default/65536, maximum/65536)
    return weight_axis


def _axis_backend_works(font, axis):
    """Probe actual glyph outlines once; GDI can ignore a successfully set axis."""
    if not hasattr(QtGui.QFont, 'setVariableAxis'):
        return False
    if axis[0] == axis[2]:
        return True
    paths = []
    for value in (axis[0], axis[2]):
        candidate = QtGui.QFont(font)
        candidate.setVariableAxis(QtGui.QFont.Tag('wght'), value)
        raw = QtGui.QRawFont.fromFont(candidate)
        if not raw.isValid():
            return None
        glyphs = [glyph for glyph in raw.glyphIndexesForString('Aa0中مЖक') if glyph]
        paths.append([raw.pathForGlyph(glyph) for glyph in glyphs])
    if not paths[0] or not paths[1]:
        return None
    return paths[0] != paths[1]


@lru_cache(maxsize=256)
def font_capabilities(family, italic=False):
    """Cache metadata only, never share QRawFont/QPainter objects across threads."""
    font = QtGui.QFont(family)
    font.setStyleName('')
    font.setPixelSize(48)
    font.setWeight(QtGui.QFont.Weight.Normal)
    font.setItalic(italic)
    raw = QtGui.QRawFont.fromFont(font)
    if not raw.isValid():
        return {'axis': None, 'backend': None, 'error': '本机无法读取该字体元数据'}
    try:
        axis = _read_weight_axis(bytes(raw.fontTable('fvar')))
    except (ValueError, struct.error) as error:
        return {'axis': None, 'backend': None, 'error': str(error)}
    return {'axis': axis, 'backend': _axis_backend_works(font, axis) if axis else None, 'error': ''}


def _clear_font_cache():
    font_capabilities.cache_clear()


def weight_font(family, weight, italic=False):
    font = QtGui.QFont(family)
    font.setStyleName("")
    font.setWeight(QtGui.QFont.Weight(int(weight)))
    font.setItalic(italic)
    capability = font_capabilities(family, italic)
    axis = capability['axis']
    if axis and capability['backend'] is True:
        font.setVariableAxis(QtGui.QFont.Tag('wght'), min(axis[2], max(axis[0], weight)))
    elif family.casefold() == "poppins":
        # Some Poppins Thin/ExtraLight files expose 250/275 to Windows instead
        # of Google Fonts' 100/200 catalog weights. Match their real faces by
        # name so these two UI presets do not both become Thin.
        closest = min(POPPINS_STYLES, key=lambda value: (abs(value-weight), value))
        style = POPPINS_STYLES[closest]
        if italic:
            style = "Italic" if closest == 400 else style + " Italic"
        if style in QtGui.QFontDatabase.styles(family):
            font.setStyleName(style)
    return font


def ensure_bundled_fonts():
    """Register once on the GUI thread, before any preview/export workers start.

    No system installation, network fetch or user configuration changes.
    The module-relative path also matches the PyInstaller data destination.
    """
    global _loaded_application
    app = QtGui.QGuiApplication.instance()
    if app is None or _loaded_application is app:
        return
    if QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError("请先在主线程加载视频字体，再启动预览或导出。")
    directory = Path(__file__).with_name("fonts")
    for family, names in BUNDLED_FONTS.items():
        for name in names:
            path = directory / family / name
            if not path.is_file() or QtGui.QFontDatabase.addApplicationFont(str(path)) < 0:
                raise RuntimeError("视频字体资源缺失或无法加载：" + str(path))
    _clear_font_cache()
    app.fontDatabaseChanged.connect(_clear_font_cache)
    _loaded_application = app


def font_weight_status(family, weight, italic=False):
    """Describe native styles and Qt's actual match, not merely the saved value."""
    font = weight_font(family, weight, italic)
    font.setPixelSize(48)
    info = QtGui.QFontInfo(font)
    capability = font_capabilities(family, italic)
    axis = capability['axis']
    actual = int(info.weight())
    styles = QtGui.QFontDatabase.styles(info.family())
    installed = sorted({int(QtGui.QFontDatabase.weight(info.family(), style))
                        for style in styles if QtGui.QFontDatabase.italic(info.family(), style) == italic})
    if info.family().casefold() == "poppins":
        available = {value: ("Italic" if value == 400 else style+" Italic") if italic else style
                     for value, style in POPPINS_STYLES.items()}
        installed = sorted(value for value, style in available.items() if style in styles)
        actual = next((value for value, style in available.items() if style == info.styleName()), actual)
    family_found = any(name.casefold() == family.casefold() for name in QtGui.QFontDatabase.families())
    if not family_found:
        summary = "字体未找到，实际使用：" + info.family()
    elif axis and capability['backend'] is True:
        applied = min(axis[2], max(axis[0], weight))
        span = f'{axis[0]:g}–{axis[2]:g}'
        summary = (f'可变字重 {applied:g}（范围 {span}）' if applied == weight else
                   f'请求 {weight} → 应用 {applied:g}（可变范围 {span}）')
    elif axis:
        summary = f'字体含可变字重；本机支持待确认，当前Qt匹配 {actual}' if capability['backend'] is None else (
            f'字体含可变字重，但当前Qt后端未应用；匹配 {actual}')
    elif capability['error']:
        summary = f'可变字重检测待确认；当前Qt匹配 {actual}'
    elif actual not in installed:
        summary = f"请求 {weight} → Qt匹配 {actual}（未枚举该档，可能为模拟）"
    elif actual != weight and len(installed) == 1:
        summary = f"请求 {weight} → 实际 {actual}（本机只加载了 {installed[0]} 一档字形）"
    elif actual != weight:
        summary = f"请求 {weight} → 实际 {actual}（当前加载字形中没有该档）"
    else:
        summary = f"实际字重 {actual} · {info.styleName()}"
    details = (f"字体：{info.family()}；{'斜体' if italic else '正体'}\n"
               f"当前Qt枚举的预设字重：{' / '.join(map(str, installed)) or '未枚举到'}\n")
    if axis:
        details += f'OpenType可变字重范围：{axis[0]:g}–{axis[2]:g}；默认 {axis[1]:g}\n'
        if capability['backend'] is True:
            details += f'已验证本机字形轮廓随权重轴改变；请求 {weight} → 应用 {min(axis[2], max(axis[0], weight)):g}\n'
        else:
            details += '当前后端未确认轴生效，使用原有Qt匹配；未冒充已实现连续调节。\n'
    elif capability['error']:
        details += capability['error']+'；不能据此认定字体没有可变字重。\n'
    else:
        details += f'所选字形无wght可变轴；请求 {weight} → Qt匹配 {actual} · {info.styleName()}\n'
        if len(installed) == 1:
            details += '当前字体资源只有一个字形档位，不代表这个字体家族没有其他字重。\n'
        if family.casefold() == 'open sans extrabold':
            details += '这是固定ExtraBold字形；请选择“Open Sans”使用程序内置完整300–800连续字重。\n'
    details += '数值原样保存；预设列表不等于字体全部能力，不会用描边伪造字重。'
    return summary, details
