"""Job-private reusable drawing glyphs; no installed fonts or new dependencies."""
import hashlib
import base64
import json
import struct
from collections import OrderedDict
from pathlib import Path
from qt_compat import QtCore,QtGui
from model.AppDataStorage import data_file
from .caption_font_binary import font

FONT_FOLDER='caption-glyphs'
MAX_GLYPHS=4096


def path_key(primitive):
    encoded=QtCore.QByteArray();stream=QtCore.QDataStream(encoded,QtCore.QIODevice.OpenModeFlag.WriteOnly)
    stream<<primitive['native']
    return (bool(primitive.get('outline_path')),hashlib.sha256(bytes(encoded)).digest())


class DrawingCommandCache:
    def __init__(self):self.values=OrderedDict();self.bytes=0
    def get(self,primitive):
        key=path_key(primitive)
        if key in self.values:self.values.move_to_end(key);return self.values[key]
        from .text_outline import ass_commands,compact_commands
        from .captions import _path_commands
        result=ass_commands(primitive['native']) if primitive.get('outline_path') else compact_commands(_path_commands(primitive['native']))
        size=len(result)
        if size<=8*1024*1024:
            self.values[key]=result;self.bytes+=size
            while self.bytes>8*1024*1024:
                _key,old=self.values.popitem(last=False);self.bytes-=len(old)
        return result


def contours(path,precision):
    result=[];points=0
    for index in range(path.elementCount()):
        element=path.elementAt(index)
        if element.type not in (QtGui.QPainterPath.ElementType.MoveToElement,QtGui.QPainterPath.ElementType.LineToElement):return None
        point=tuple(round(float(f'{v:.3f}')*precision) for v in (element.x,element.y))
        if not -32768<=point[0]<=32767 or not -32768<=2048-point[1]<=32767:return None
        if element.type==QtGui.QPainterPath.ElementType.MoveToElement:result.append([])
        if not result:return None
        result[-1].append(point);points+=1
        if points>60000 or len(result)>30000:return None
    # The existing ASS drawing parser implicitly closes every contour.
    result=[value for value in result if len(value)>1]
    if not result:return None
    last=(0,0)
    for contour in result:
        for point in contour:
            converted=(point[0],2048-point[1])
            if any(not -32768<=b-a<=32767 for a,b in zip(last,converted)):return None
            last=converted
    return result


class DrawingResources:
    def __init__(self,path,primitives,enabled,check=None):
        self.folder=Path(path).parent/FONT_FOLDER;self.records={};self.styles=[];self.bytes=0;self.glyphs=0;self.files=[]
        if not enabled:return
        entries={};keys={}
        for index,primitive in enumerate(primitives):
            if check and index%128==0:check()
            if primitive.get('cursor_image') or not primitive.get('outline_path'):continue
            precision=8
            encoded=QtCore.QByteArray();stream=QtCore.QDataStream(encoded,QtCore.QIODevice.OpenModeFlag.WriteOnly)
            stream<<primitive['native']
            key=(precision,hashlib.sha256(bytes(encoded)).digest());keys[id(primitive)]=key
            if key not in entries:entries[key]=contours(primitive['native'],precision)
        for precision in (8,):
            valid=sorted(key for key,value in entries.items() if key[0]==precision and value)
            for start in range(0,len(valid),MAX_GLYPHS):
                if check:check()
                selected=valid[start:start+MAX_GLYPHS]
                digest=hashlib.sha256(bytes([precision])+b''.join(k[1] for k in selected)).hexdigest()[:24]
                family='BTVG'+digest;style='G'+digest;size=2048/precision
                try:binary=font([entries[k] for k in selected],family,2048)
                except (ValueError,OverflowError,struct.error):continue
                target=self.folder/(family+'.ttf');data_file(target).write_bytes(binary)
                self.files.append(target.name);self.bytes+=len(binary);self.glyphs+=len(selected);self.styles.append((style,family,size))
                for i,key in enumerate(selected):entries[key]=(style,chr(0xe000+i))
        for identifier,key in keys.items():
            value=entries[key]
            if isinstance(value,tuple):self.records[identifier]=value

    def get(self,primitive):return self.records.get(id(primitive))

    def style_lines(self):
        return ''.join(f'Style: {style},{family},{size:g},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n'
                       for style,family,size in self.styles)

    def embed(self,output):
        """ASS font attachments keep generated scripts self-contained."""
        if not self.files:return
        output.write('[Fonts]\n')
        alphabet=b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        mapping=bytes.maketrans(alphabet,bytes(range(33,97)))
        reserved=('[script info]','[v4+ styles]','[v4 styles]','[events]','[fonts]')
        for name in self.files:
            output.write('fontname: '+name+'\n')
            with data_file(self.folder/name).open('rb') as source:
                while chunk:=source.read(60):
                    line=base64.b64encode(chunk).rstrip(b'=').translate(mapping).decode('ascii')
                    if line.lower().startswith(reserved):output.write(line[:1]+'\n');line=line[1:]
                    output.write(line+'\n')


def ass_filter(path,*,alpha=False):
    from .captions import filter_path
    result='ass=filename='+filter_path(path)
    folder=Path(path).parent/FONT_FOLDER
    if folder.is_dir():result+=':fontsdir='+filter_path(folder)
    return result+(':alpha=1' if alpha else '')


def memory_for_directory(directory):
    amount=0
    for path in Path(directory).rglob('*.caption-data.json'):
        value=json.loads(data_file(path).read_text('utf8'))
        # Event text + parsed events + reusable outlines, per libass instance.
        amount+=(value['bytes']*3+value['rows']*320+value['resource_bytes']*2)*value.get('renderers',1)+value.get('cursor_memory',0)
    return amount
