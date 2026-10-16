"""Headless host jobs; UI and worker model instances never cross threads."""
from model.image_retrieval.encoder import ChineseImageEncoder
from model.video_retrieval.frames import resolve_tools, Canceled
from model.video_retrieval.index import VideoSearchIndex, discover_videos
from model.BackgroundTasks import BackgroundTaskCanceled, BackgroundTaskResultError


def run_video_index_job(payload, context):
    settings = payload['settings']
    encoder = ChineseImageEncoder(settings['model'])
    index = VideoSearchIndex(payload['index_root'], settings['model'])
    def progress(done, total, message):
        context.report(message, min(99, int(done / total * 100)) if total else None)
    try:
        context.report('正在扫描视频库…')
        paths = discover_videos(payload['roots'], context.is_canceled)
        if not paths:
            raise ValueError('这些目录没有支持的视频文件；请检查视频库目录，旧索引保留。')
        ffmpeg, ffprobe = resolve_tools(settings['ffmpeg_path'])
        result = index.sync(paths, encoder, settings, ffmpeg, ffprobe, progress, context.is_canceled)
        if result['canceled']:
            raise BackgroundTaskCanceled('已取消，已经完成的视频索引保留。')
        if result['errors']:
            raise BackgroundTaskResultError(
                f"视频索引：更新 {result['updated']}，未变 {result['unchanged']}，失败 {len(result['errors'])}；可查看详情。", result)
        context.report('视频索引已保存', 100)
        return result
    except Canceled as error:
        raise BackgroundTaskCanceled(str(error)) from error
    finally:
        encoder.release()
        index.release()
