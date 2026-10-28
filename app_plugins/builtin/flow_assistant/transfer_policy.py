"""Transfer-only retry policy. No generation, network or Qt dependencies."""
import math
import time

DELAYS = (5, 15, 30, 60, 120)
LABELS = {'upload': '图片上传', 'download': '视频下载'}


def retry_limit(preferences, stage):
    value = preferences.get(stage + '_retry_limit', 5)
    return max(0, min(10, value)) if type(value) is int else 5


def retry_info(row, stage):
    value = row.get('remote', {}).get('transfer_retries', {}).get(stage, {})
    return value if value.get('attempt', row.get('attempt')) == row.get('attempt') else {}


def ready_bindings(remote):
    return [binding for binding in remote.get('bindings', []) if isinstance(binding, dict)
            and binding.get('schema') == 2 and binding.get('submissionId') and any(
                media in binding.get('mediaIds', []) and isinstance(binding.get('urls', {}).get(media), str)
                and binding['urls'][media].startswith('https:') for media in binding.get('readyIds', []))]


def retry_detail(row, preferences, now=None):
    if row['remote'].get('image_upload_abandoned'):return row.get('error','图片上传失败5次，已放弃所有同图未提交任务。')
    stage = 'upload' if row['status'] == 'uploading' or row['status'] == 'failed' and retry_info(row, 'upload').get('stopped') else 'download' if row['status'] in {'download_failed', 'downloading'} or row['status']=='download_parked' and row['remote'].get('download_backlog') else None
    if not stage: return ''
    info = retry_info(row, stage)
    if not info: return ''
    count, limit = info.get('count', 0), retry_limit(preferences, stage)
    label = LABELS[stage]
    cycle=row['remote'].get('upload_cycle',{})
    if stage=='upload' and cycle and not cycle.get('confirmed') and not info.get('permanent'):
        if str(info.get('waiting','')).startswith('原图3分钟'):return info['waiting']
        if not info.get('stopped') and count>=limit:
            remaining=max(0,math.ceil(cycle['at']+180-(time.time() if now is None else now)))
            return f'原图尚未确认，{remaining} 秒后重新拖拽上传（距上次上传满3分钟）'
    if info.get('stopped'):
        if not limit: return f'{label}自动重试已关闭：' + info.get('last_error', '')
        return f'{label}自动重试已停止（{count}/{limit}）：' + info.get('last_error', '')
    if info.get('waiting'):
        if stage == 'download' and info.get('probes') and not info.get('active'):
            return f'视频结果核对 {info["probes"]}/{info.get("probe_limit", 10)} · ' + info['waiting']
        return f'{label}重试 {count}/{limit} · ' + info['waiting']
    if info.get('next_at'):
        seconds = max(0, math.ceil(info['next_at'] - (time.time() if now is None else now)))
        return f'{seconds} 秒后自动重试{label}（第 {count + 1}/{limit} 次）'
    if count:
        if not limit: return f'正在手动重试{label}'
        return f'正在重试{label}（第 {count}/{limit} 次）'
    return ''
