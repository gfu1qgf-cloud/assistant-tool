"""Small vision inputs from frozen reference copies, never live source paths."""
import base64
import hashlib
import io
from pathlib import Path
import re
from PIL import Image
from model.AppDataStorage import data_directory


def planning_images(root, job):
    if not re.fullmatch(r'[a-f0-9]{32}',job['id']):raise ValueError('参考图任务ID无效。')
    output=[]
    for ref in job['references']:
        name=ref.get('file','')
        if not re.fullmatch(r'[a-f0-9]{64}\.(?:png|jpg|jpeg|webp)',name):raise ValueError('参考图快照名称无效。')
        file=data_directory(Path(root)/job['id']/'references').file(name)
        raw=file.read_bytes()
        if len(raw)>3*1024**2 or hashlib.sha256(raw).hexdigest()!=Path(name).stem:
            raise ValueError('参考图快照内容已改变，未提交文字请求。')
        with Image.open(io.BytesIO(raw)) as original:
            if original.width*original.height>40000000:raise ValueError('参考图尺寸异常。')
            image=original.convert('RGBA')
            image.thumbnail((768,768),Image.Resampling.LANCZOS)
            canvas=Image.new('RGB',image.size,'white');canvas.paste(image,mask=image.getchannel('A'))
            for side,quality in ((768,80),(768,65),(512,65),(384,60)):
                canvas.thumbnail((side,side),Image.Resampling.LANCZOS)
                stream=io.BytesIO();canvas.save(stream,format='JPEG',quality=quality)
                encoded=stream.getvalue()
                if len(encoded)<=192*1024:break
            if len(encoded)>192*1024:raise ValueError('参考图分析预览仍过大，请缩小后重试。')
        output.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(encoded).decode(),
                       'detail':'high'})
    return output


def recent_reference_items(jobs, root, limit=20):
    """Reuse existing snapshots; add no cache copies and execute no file paths."""
    rows=[];seen=set()
    for job in jobs:
        if not re.fullmatch(r'[a-f0-9]{32}',str(job.get('id',''))):continue
        original=job.get('spec',{}).get('references',[])
        for index,ref in enumerate(job.get('references',[])):
            if not isinstance(ref,dict):continue
            name=ref.get('file','')
            if not isinstance(name,str) or not re.fullmatch(r'[a-f0-9]{64}\.(?:png|jpg|jpeg|webp)',name) or name in seen:continue
            path=Path(root)/job['id']/'references'/name
            try:
                if not path.is_file():continue
            except OSError:continue
            title=Path(original[index]).name if index<len(original) and isinstance(original[index],str) else name[:12]
            rows.append({'path':str(path),'name':title,'created':job.get('created',0)})
            seen.add(name)
            if len(rows)>=limit:return rows
    return rows
