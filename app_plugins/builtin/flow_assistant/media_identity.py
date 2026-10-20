"""A directory or nonempty ID is not proof that a download belongs to a job."""


def generated_bindings(remote):
    bindings = remote.get('bindings', [])
    if not isinstance(bindings, list): return []
    return [b for b in bindings if isinstance(b, dict) and b.get('schema') == 2
            and b.get('source') == 'generation_response'
            and isinstance(b.get('submissionId'), str) and b['submissionId']
            and isinstance(b.get('taskId'), str) and b['taskId']
            and isinstance(b.get('mediaIds'), list) and isinstance(b.get('readyIds'), list)
            and all(isinstance(value, str) and value for value in b['mediaIds'] + b['readyIds'])]


def merge_snapshot_bindings(remote, bindings):
    """Retain durable ownership/readiness when the same page state is partial.

    The caller rejects conflicting task/submission/media identities first.
    A new signed URL may replace its old URL; missing readiness cannot erase it.
    """
    previous = generated_bindings(remote)
    incoming = generated_bindings({'bindings': bindings})
    if not previous:
        return bindings
    if not incoming:
        return remote['bindings']
    identity = lambda b: (b['taskId'], b['submissionId'])
    saved = {identity(binding): binding for binding in previous}
    merged = []
    for binding in bindings:
        if binding not in incoming or identity(binding) not in saved:
            merged.append(binding)
            continue
        old = saved[identity(binding)]
        media_ids = binding['mediaIds'] or old['mediaIds']
        urls = dict(old.get('urls') or {})
        urls.update({media: url for media, url in (binding.get('urls') or {}).items()
                     if media in media_ids and isinstance(url, str) and url.startswith('https:')})
        merged.append({**old, **binding, 'mediaIds': media_ids, 'urls': urls,
                       'readyIds': list(dict.fromkeys(old['readyIds'] + binding['readyIds']))})
    return merged


def owns_download(job, download):
    if not isinstance(download, dict) or not isinstance(download.get('mediaId'), str) or not download['mediaId']:
        return False
    for binding in generated_bindings(job['remote']):
        if (binding['submissionId'] == download.get('submissionId')
                and binding['taskId'] == download.get('taskId')
                and binding['taskId'] == job['remote'].get('upstream_task')
                and download.get('mediaId') in binding.get('mediaIds', [])
                and download.get('mediaId') not in binding.get('failedIds', [])
                and download.get('mediaId') in binding.get('readyIds', [])):
            return True
    return False
