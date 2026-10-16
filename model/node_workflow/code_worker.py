"""Isolated process protocol. No Qt, app startup, secrets broker or private log."""
import contextlib
import io
import json
from pathlib import Path
import sys
import traceback

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class BoundedLog(io.StringIO):
    def write(self, text):
        if self.tell() + len(text) > 32768:
            raise ValueError('Python日志超过32KB，请减少打印。')
        return super().write(text)


def main():
    from model.node_workflow.code_node import decode, encode, public_classes, MAX_WIRE
    from model.node_workflow.code_stdio import binary_channel
    reader, own_reader = binary_channel('stdin')
    writer, own_writer = binary_channel('stdout')
    log = BoundedLog()
    try:
        raw = reader.read(MAX_WIRE + 1)
        if len(raw) > MAX_WIRE:
            raise ValueError('Python节点请求过大。')
        request = json.loads(raw.decode('utf-8'))
        if request.get('offline') is True:
            from model.OfflineMode import enable_offline
            enable_offline()
        namespace = {'__name__': '__node_code__', 'inputs': decode(request['inputs']), 'outputs': {}, **public_classes()}
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            exec(compile(request['code'], '<节点代码>', 'exec'), namespace, namespace)
        reply = {'outputs': encode(namespace['outputs']), 'log': log.getvalue()}
    except BaseException:
        reply = {'error': traceback.format_exc()[-12000:], 'log': log.getvalue()}
    result = json.dumps(reply, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(result) > MAX_WIRE:
        result = json.dumps({'error': 'Python节点输出超过16MB，未截断。'}, ensure_ascii=False).encode('utf-8')
    try:
        writer.write(result)
        writer.flush()
    finally:
        if own_reader: reader.close()
        if own_writer: writer.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
