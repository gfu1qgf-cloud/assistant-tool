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
        for field in ('source', 'result'): protected.add(identity(entry[field]['path']))
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
