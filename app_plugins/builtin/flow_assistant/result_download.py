"""Recover only the unique media identity from the original generation receipt."""
import copy
import re
from .media_identity import generated_bindings
from .transfer_policy import retry_info


def unique_result(remote):
    matches = [(b,media) for b in generated_bindings(remote) if b['taskId']==remote.get('upstream_task')
               for media in b['mediaIds'] if media not in b.get('failedIds', []) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}',media)]
    return matches[0] if len(matches)==1 else None


def completed_receipt(row, data):
    target = row['remote'].get('download_recovery_target') or {}
    download = retry_info(row, 'download').get('download_id')
    if (data.get('state') != 'complete' or type(download) is not int or data.get('downloadId') != download or
            target.get('attempt') != row['attempt'] or data.get('taskId') != target.get('task_id') or
            data.get('submissionId') != target.get('submission_id') or data.get('mediaId') != target.get('media_id')):
        return None
    remote = copy.deepcopy(row['remote'])
    binding = next((b for b in generated_bindings(remote) if b['taskId']==target['task_id']==remote.get('upstream_task') and
                    b['submissionId']==target['submission_id'] and target['media_id'] in b['mediaIds'] and
                    target['media_id'] not in b.get('failedIds', [])), None)
    if not binding:return None
    binding['readyIds'] = list(dict.fromkeys(binding['readyIds'] + [target['media_id']]))
    remote['cloud_finished'] = True
    return remote
