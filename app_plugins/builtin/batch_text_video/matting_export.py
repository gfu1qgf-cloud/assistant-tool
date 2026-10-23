"""Optional explicit exports; managed results and source files stay immutable."""
from pathlib import Path
from model.AppDataStorage import data_file
from .matting_assets import identity, verify_file


def export_copy(catalogue, record, destination):
    source = verify_file(record['result'], '已保存的抠像结果')
    destination = Path(destination).resolve()
    if not destination.suffix: destination = destination.with_suffix(source.suffix)
    if destination.suffix.lower() != source.suffix.lower(): raise ValueError('请选择与已存结果相同的PNG／MOV格式。')
    protected = set()
    for entry in catalogue.load()['assets'].values():
        for field in ('source', 'result'):
            if field in entry: protected.add(identity(entry[field]['path']))
    if identity(destination) in protected: raise ValueError('另存不能覆盖源素材或工作台管理的抠像结果。')
    try: destination.relative_to(catalogue.root.resolve())
    except ValueError: pass
    else: raise ValueError('请另存到工作台管理目录以外的位置。')
    with data_file(source).open('rb') as input_stream, data_file(destination).atomic_writer(binary=True) as output:
        while True:
            chunk = input_stream.read(1024*1024)
            if not chunk: break
            output.write(chunk)
    return destination


def export_dynamic(catalogue, record, destination, *, models=None, ffmpeg='', cancel=None, report=None, progress=None):
    """Only an explicit 'save as' creates a transparent media file."""
    from model.AppDataStorage import data_io, data_directory
    from .matting_assets import verify_record, record_files
    from .matting_render import _video, infer, refined
    from .matting_models import check_cancel
    from .visual_media import is_animated
    verify_record(record)
    source = record['source']['path']; p = record['parameters']
    suffix = '.mov' if is_animated(source) else '.png'
    target = Path(destination).resolve()
    if not target.suffix: target = target.with_suffix(suffix)
    if target.suffix.lower() != suffix: raise ValueError('请选择透明PNG／MOV格式。')
    protected = {identity(path) for entry in catalogue.load()['assets'].values() for path in record_files(entry)}
    protected.add(identity(source))
    if identity(target) in protected: raise ValueError('另存不能覆盖源素材或工作台管理的抠像结果。')
    try: target.relative_to(catalogue.root.resolve())
    except ValueError: pass
    else: raise ValueError('请另存到工作台管理目录以外的位置。')
    data_directory(target.parent).ensure()
    with data_io.TemporaryDirectory(prefix='.matting-export-', dir=target.parent) as folder:
        temporary = Path(folder)/('transparent'+suffix)
        if suffix == '.mov': _video(source, temporary, p, models, ffmpeg, cancel, report, progress)
        else:
            from PIL import Image
            base = infer(source, p, models=models, cancel=cancel, report=report, ffmpeg=ffmpeg)
            with data_file(temporary).open('wb') as stream: Image.fromarray(refined(base, p), 'RGBA').save(stream, 'PNG')
        check_cancel(cancel); verify_record(record)
        data_io.replace(temporary, target)
    return target
