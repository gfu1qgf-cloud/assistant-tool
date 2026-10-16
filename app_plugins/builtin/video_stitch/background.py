from model.BackgroundTasks import BackgroundTaskCanceled, TaskCancellationToken
from .engine import Canceled, export_pair


def run_stitch_job(payload, context):
    try:
        result = export_pair(payload["left"], payload["right"], payload["output"],
            basis=payload["basis"], audio=payload["audio"], compatible=payload["compatible"],
            overwrite=payload.get("overwrite", False), cancel=TaskCancellationToken(context),
            progress=lambda value: context.report("正在拼接／验证成品", value))
    except Canceled:
        raise BackgroundTaskCanceled("视频拼接已取消，源视频未修改。") from None
    return {**result, "paths": [result["output"]]}
