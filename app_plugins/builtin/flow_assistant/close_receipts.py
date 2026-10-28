"""Release download-only leftovers for closure, retaining all cloud uncertainty."""
import re
from .media_identity import generated_bindings


def parked(service, client):
    result=[]
    for row in service.store.rows():
        remote=row['remote'];attempt=row['attempt']
        if (row['browser']!=client['id'] or row['status']!='download_parked' or
                remote.get('cloud_finished') is not True or remote.get('download_skipped') is not True or
                not re.fullmatch(r'[a-f0-9]{32}',attempt or '')):continue
        from .video_catalog import manual_binding
        if manual_binding(row) or any(b['taskId']==remote.get('upstream_task') and
               set(b['readyIds']) & (set(b['mediaIds'])-set(b.get('failedIds',[])))
               for b in generated_bindings(remote)):
            result.append(attempt)
    return result


def released(service, client):
    return list(dict.fromkeys(parked(service,client)+service.store.released_attempts(client['id'])))[:512]
