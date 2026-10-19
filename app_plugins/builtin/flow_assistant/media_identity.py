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


def owns_download(job, download):
    if not isinstance(download, dict) or not isinstance(download.get('mediaId'), str) or not download['mediaId']:
        return False
    for binding in generated_bindings(job['remote']):
        if (binding['submissionId'] == download.get('submissionId')
                and binding['taskId'] == download.get('taskId')
                and binding['taskId'] == job['remote'].get('upstream_task')
                and download.get('mediaId') in binding.get('mediaIds', [])
                and download.get('mediaId') in binding.get('readyIds', [])):
            return True
    return False
