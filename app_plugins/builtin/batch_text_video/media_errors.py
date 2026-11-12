"""Keep allocation failures actionable even when MemoryError has no message."""


class MediaMemoryError(RuntimeError):
    pass


def memory_failure(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,(MemoryError,MediaMemoryError)):
            return True
        if isinstance(error,OSError) and (getattr(error,'errno',None)==12 or getattr(error,'winerror',None) in {8,14,1455}):
            return True
        text=str(error).casefold()
        if any(value in text for value in ('cannot allocate memory','not enough memory','paging file is too small','系统可提交内存不足','页面文件太小')):
            return True
        error=getattr(error,'__cause__',None) or getattr(error,'__context__',None)
    return False


def describe(error):
    if memory_failure(error):
        return '系统可提交内存不足，视频处理失败。请降低渲染并发（例如从8降到2），或关闭占用大量内存的程序后重试。'
    return str(error).strip() or type(error).__name__
