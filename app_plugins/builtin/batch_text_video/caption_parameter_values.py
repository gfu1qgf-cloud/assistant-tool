"""Semantic, read-only value presentation for the subtitle parameter overview."""
import json
from qt_compat import QtCore, QtGui, QtWidgets

VALUE_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
SEARCH_ROLE = VALUE_ROLE + 1
FILTERS = (('全部类型','all'),('颜色与渐变','color'),('数值','number'),
           ('开关','bool'),('选项','choice'),('文字','text'))


def unit_for(key, label, control):
    # Stored geometry is always percentage/centre data, even when its editor
    # currently displays pixel distances from an anchored edge.
    if key in {'x','y','width','height','caption_focus_x','caption_focus_y','caption_clip_feather'}: return '%'
    suffix = control.suffix().strip() if isinstance(control, QtWidgets.QAbstractSpinBox) and hasattr(control,'suffix') else ''
    if suffix: return suffix
    if key.endswith('_ms'): return 'ms'
    if key.endswith(('_duration','_period')) or '秒数' in label: return '秒'
    if key == 'metronome_bpm': return 'BPM'
    if key in {'opacity_bg','opacity_outline','opacity_shadow','opacity_high_bg','speed_trail_opacity'}: return '/ 255'
    if key in {'global_mask_opacity','ambient_glow_opacity','ambient_dark_opacity','scrolling_opacity_context'}: return '/ 1'
    if '%' in label or '不透明度' in label: return '%'
    if '角度' in label or key == 'rotation': return '°'
    if '倍数' in label or 'scale' in key or key in {'pop_start','pop_peak'}: return '×'
    if key.endswith(('_seed','_version')) or '种子' in label: return ''
    if '层数' in label: return '层'
    if '词数' in label or '最多词' in label: return '词'
    if '行数' in label: return '行'
    if key in {'line_spacing','caption_size_contrast'}: return '×'
    if any(word in label for word in ('px','字号','振幅','高度','半径','外扩','柔化','间隔','字距','词距','偏移','模糊')): return 'px'
    return ''


def color_name(color):
    hue,saturation,value,_alpha = color.getHsvF()
    if value < .16: return '黑色'
    if saturation < .12: return '白色' if value > .88 else '灰色'
    hue *= 360
    return next(name for bound,name in ((15,'红色'),(45,'橙色'),(70,'黄色'),(165,'绿色'),
                                       (200,'青色'),(255,'蓝色'),(290,'紫色'),(345,'粉色'),(361,'红色')) if hue < bound)


def describe(control, value, key='', label=''):
    info = {'raw':value,'kind':'text','text':str(value),'colors':[],'gradient':False}
    if type(value) is bool:
        info.update(kind='bool',text='开启' if value else '关闭')
        return info
    is_color = ('color' in key or 'palette' in key or key in {'color','caption_group_palette'})
    if is_color:
        choices = value if isinstance(value,list) else str(value).split(',')
        if choices and all(QtGui.QColor(str(v).strip()).isValid() for v in choices):
            palette = [QtGui.QColor(str(v).strip()) for v in choices]
            gradient = len(palette)>1 and key not in {'caption_group_palette','stroke_expand_colors','color_palette'}
            if len(palette)==1:
                color=palette[0]
                text=color_name(color)+' · '+color.name().upper()
                if color.alpha()<255: text+=' · '+format(color.alphaF()*100,'.3g')+'%'
            else:
                text=('渐变' if gradient else '调色板')+' · '+str(len(palette))+' 色'
            info.update(kind='color',text=text,colors=[v.name(QtGui.QColor.NameFormat.HexArgb) for v in palette],gradient=gradient)
            return info
    if isinstance(control,QtWidgets.QComboBox):
        index=control.findData(value)
        info.update(kind='choice',text=control.itemText(index) if index>=0 else str(value))
        return info
    if type(value) in (int,float):
        unit=unit_for(key,label,control)
        info.update(kind='number',text=format(value,'.8g')+(' '+unit if unit else ''),unit=unit)
        return info
    if isinstance(value,list): info['text']='，'.join(map(str,value))
    if value in ('',None): info['text']='未设置'
    return info


def swatch(info, palette, width=84, height=20):
    pixmap=QtGui.QPixmap(width,height)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter=QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect=QtCore.QRectF(1,1,width-2,height-2)
        if info['kind']=='bool':
            color=palette.color(QtGui.QPalette.ColorRole.Highlight) if info['raw'] else palette.color(QtGui.QPalette.ColorRole.Mid)
            painter.setPen(QtCore.Qt.PenStyle.NoPen);painter.setBrush(color)
            painter.drawEllipse(QtCore.QRectF(2,2,height-4,height-4))
            pen=QtGui.QPen(palette.color(QtGui.QPalette.ColorRole.HighlightedText),2)
            painter.setPen(pen)
            if info['raw']:
                painter.drawLine(QtCore.QPointF(6,height*.5),QtCore.QPointF(9,height*.65))
                painter.drawLine(QtCore.QPointF(9,height*.65),QtCore.QPointF(14,height*.34))
            else:painter.drawLine(QtCore.QPointF(6,height*.5),QtCore.QPointF(14,height*.5))
            return pixmap
        path=QtGui.QPainterPath();path.addRoundedRect(rect,4,4);painter.setClipPath(path)
        for y in range(0,height,5):
            for x in range(0,width,5):
                painter.fillRect(x,y,5,5,QtGui.QColor('#cccccc' if (x//5+y//5)%2 else '#ffffff'))
        colors=[QtGui.QColor(v) for v in info['colors']]
        if info['gradient']:
            gradient=QtGui.QLinearGradient(rect.topLeft(),rect.topRight())
            for i,color in enumerate(colors):gradient.setColorAt(i/(len(colors)-1),color)
            painter.fillRect(rect,gradient)
        else:
            for i,color in enumerate(colors):
                painter.fillRect(QtCore.QRectF(rect.left()+i*rect.width()/len(colors),rect.top(),rect.width()/len(colors),rect.height()),color)
        painter.setClipping(False);painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(palette.color(QtGui.QPalette.ColorRole.Mid));painter.drawRoundedRect(rect,4,4)
    finally:painter.end()
    return pixmap


def decorate(item, column, info, palette, changed=False):
    item.setData(column,VALUE_ROLE,info)
    if info['kind'] in {'color','bool'}:
        item.setIcon(column,QtGui.QIcon(swatch(info,palette,width=84 if info['kind']=='color' else 34)))
    if changed and column==1:
        item.setForeground(column,palette.brush(QtGui.QPalette.ColorRole.Link))
        font=item.font(column);font.setBold(True);item.setFont(column,font)
    text=info['text']
    if info['colors']: text+='\n'+'  '.join(info['colors'])
    item.setToolTip(column,text)


def searchable(info):
    return info['text']+' '+json.dumps(info['raw'],ensure_ascii=False)+' '+' '.join(info['colors'])
