"""Unhinted, app-private vector resources in a minimal TrueType container.
OpenType tables follow https://learn.microsoft.com/typography/opentype/spec/ .
No source font data or text shaping tables are copied; these are final drawing paths.
"""
import struct


def pack(fmt,*values):return struct.pack('>'+fmt,*values)
def padded(value):return value+bytes((-len(value))%4)
def checksum(value):return sum(struct.unpack('>'+str(len(padded(value))//4)+'I',padded(value)))&0xffffffff


def font(paths,family,units=1024):
    glyphs=[pack('5hH',0,0,0,0,0,0)];boxes=[(0,0,0,0)];counts=[(0,0)]
    for contours in paths:
        points=[point for contour in contours for point in contour]
        x=[p[0] for p in points];y=[units-p[1] for p in points]
        box=(min(x),min(y),max(x),max(y));boxes.append(box);counts.append((len(points),len(contours)))
        ends=[];n=0
        for contour in contours:n+=len(contour);ends.append(n-1)
        dx=[x[0]]+[b-a for a,b in zip(x,x[1:])];dy=[y[0]]+[b-a for a,b in zip(y,y[1:])]
        glyphs.append(pack('h4h',len(contours),*box)+pack(str(len(ends))+'H',*ends)+pack('H',0)+bytes([1])*len(points)
            +pack(str(len(dx))+'h',*dx)+pack(str(len(dy))+'h',*dy))
    n=len(glyphs);offsets=[0];parts=[]
    for g in glyphs:
        parts.append(padded(g));offsets.append(offsets[-1]+len(parts[-1]))
    glyf=b''.join(parts)
    bounds=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
    tables={b'glyf':glyf,b'loca':pack(str(n+1)+'I',*offsets),
        b'hmtx':b''.join(pack('Hh',units,b[0]) for b in boxes),
        b'head':pack('4I2H2Q4h3h2h',0x10000,0x10000,0,0x5f0f3cf5,3,units,0,0,*bounds,0,8,2,1,0),
        b'hhea':pack('I3hH11hH',0x10000,units,0,0,units,min(b[0] for b in boxes),min(units-b[2] for b in boxes),
            max(b[2] for b in boxes),1,0,0,0,0,0,0,0,n),
        b'maxp':pack('I14H',0x10000,n,max(v[0] for v in counts),max(v[1] for v in counts),0,0,1,0,0,0,0,0,0,0,0),
        b'post':pack('Iihh5I',0x30000,0,0,0,0,0,0,0,0)}
    os2=pack('Hh3H11h',0,units,400,5,0,*([0]*11))+bytes(10)+pack('4I4s3H3h2H',0,0,0,0,b'BTVG',0x40,0xe000,0xe000+n-2,units,0,0,units,0)
    assert len(os2)==78,len(os2)
    tables[b'OS/2']=os2
    cmap=pack('7H',4,32,0,4,4,1,0)+pack('2HH2H2H2H',0xe000+n-2,0xffff,0,0xe000,0xffff,(1-0xe000)&65535,1,0,0)
    tables[b'cmap']=pack('2H2HI',0,1,3,1,12)+cmap
    values={1:family,2:'Regular',3:family,4:family,6:family};records=[];strings=b''
    for name,value in values.items():
        encoded=value.encode('utf-16-be');records.append(pack('6H',3,1,0x409,name,len(encoded),len(strings)));strings+=encoded
    tables[b'name']=pack('3H',0,len(records),6+12*len(records))+b''.join(records)+strings
    tags=sorted(tables);power=2**(len(tags).bit_length()-1);header=pack('I4H',0x10000,len(tags),power*16,power.bit_length()-1,len(tags)*16-power*16)
    directory=b'';data=b'';offset=12+16*len(tags);head_at=0
    for tag in tags:
        value=tables[tag];directory+=pack('4s3I',tag,checksum(value),offset,len(value))
        if tag==b'head':head_at=offset
        data+=padded(value);offset+=len(padded(value))
    output=bytearray(header+directory+data);struct.pack_into('>I',output,head_at+8,(0xb1b0afba-checksum(output))&0xffffffff)
    return bytes(output)
