"""Reusable local colour sets. Render rules carry a portable selection snapshot."""
import copy
import threading
import uuid
from pathlib import Path
from qt_compat import QtGui
from model.AppDataStorage import data_file, data_directory

BUILTINS = (
    ('warm','暖橙与珊瑚',['#ffb347','#ff7f50','#ffd166','#e76f51','#f4a261']),
    ('blue','清新蓝绿',['#00b4d8','#48cae4','#2a9d8f','#90e0ef','#0077b6']),
    ('pastel','柔和马卡龙',['#f7cad0','#cdb4db','#bde0fe','#b9fbc0','#fbf8cc']),
    ('pink','粉紫花瓣',['#ff8fab','#c77dff','#e0aaff','#ffafcc','#b5179e']),
    ('earth','大地与奶咖',['#a98467','#6c584c','#adc178','#dde5b6','#f0ead2']),
    ('gold','黑白与暖金',['#ffffff','#ffd700','#f4d58d','#e8e8e8','#d4af37']),
    ('jewel','浓郁宝石',['#9b2226','#005f73','#0a9396','#ee9b00','#6a4c93']),
    ('neutral','中性灰阶',['#ffffff','#e5e5e5','#a3a3a3','#525252','#171717']),
)
_LOCK=threading.RLock()

def canonical(value, alpha=True):
    if not isinstance(value,str) or len(value)>64:raise ValueError('颜色格式无效。')
    color=QtGui.QColor(value.strip())
    if not color.isValid():raise ValueError('请输入有效颜色，例如 #ffcc66。')
    return color.name(color.NameFormat.HexArgb if alpha and color.alpha()!=255 else color.NameFormat.HexRgb)

def defaults():
    return [dict(id='builtin:'+key,name=name,colors=list(colors)) for key,name,colors in BUILTINS]

def validate(items):
    if not isinstance(items,list) or len(items)>64:raise ValueError('最多保存64个颜色套餐。')
    seen=set();result=[]
    for item in items:
        if not isinstance(item,dict):raise ValueError('颜色套餐格式无效。')
        identifier=item.get('id');name=item.get('name');colors=item.get('colors')
        if not isinstance(identifier,str) or not identifier or len(identifier)>96 or identifier in seen:raise ValueError('颜色套餐编号无效或重复。')
        if not isinstance(name,str) or not name.strip() or len(name)>60:raise ValueError('套餐名称须为1–60个字。')
        if not isinstance(colors,list) or not 1<=len(colors)<=32:raise ValueError('每个套餐需要1–32种颜色。')
        values=list(dict.fromkeys(canonical(c) for c in colors));seen.add(identifier)
        result.append(dict(id=identifier,name=name.strip(),colors=values))
    return result

class PaletteLibrary:
    def __init__(self,path=None):
        if path is None:
            from app_paths import DATA_ROOT
            path=DATA_ROOT/'BatchTextVideo'/'color-palettes.json'
        self.path=Path(path)

    def load(self):
        with _LOCK:
            file=data_file(self.path)
            if not file.exists():return dict(version=1,revision=0,palettes=defaults())
            if self.path.stat().st_size>256*1024:raise ValueError('颜色套餐库过大，未覆盖原文件。')
            value=file.read_json()
            if not isinstance(value,dict) or value.get('version')!=1 or type(value.get('revision')) is not int:raise ValueError('颜色套餐库格式无效，未重置原记录。')
            return dict(version=1,revision=value['revision'],palettes=validate(value.get('palettes')))

    def save(self,items,revision):
        with _LOCK:
            current=self.load()
            if current['revision']!=revision:raise ValueError('颜色套餐已在其他窗口修改，请重新打开后保存。')
            result=dict(version=1,revision=revision+1,palettes=validate(copy.deepcopy(items)))
            data_directory(self.path.parent)
            data_file(self.path).write_json(result,ensure_ascii=False,indent=2)
            return result

def new_palette(name,colors):return validate([dict(id=uuid.uuid4().hex,name=name,colors=colors)])[0]
