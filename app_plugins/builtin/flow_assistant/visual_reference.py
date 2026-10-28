"""Small local comparison template; never sends the original file off-machine."""
import base64
from io import BytesIO
from pathlib import Path

from .store import fingerprint


def template(reference):
    from PIL import Image, ImageOps
    path = Path(reference['path'])
    if fingerprint(path)['sha256'] != reference['sha256']:
        raise ValueError('截图备用比对的参考图内容已经变化。')
    with Image.open(path) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert('RGBA')
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        background = Image.new('RGB', image.size, 'white')
        background.paste(image, mask=image.getchannel('A'))
        output = BytesIO()
        background.save(output, format='PNG')
    return {'upload_id':reference['upload_id'],'sha256':reference['sha256'],
            'thumbnail':'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')}
