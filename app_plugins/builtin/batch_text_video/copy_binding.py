"""Keep task-paired copy together; independent pools remain an explicit choice."""
import copy


def imported_task(job):
    """Import provenance is independent of whether the title is required."""
    return bool(job and (job.get('requires_title') or
        job.get('text_source') in {'task_audio_text', 'task_name'}))


def bind_task_copy(layer, job, *, source_profile=''):
    """Return a frozen text source, or None when ordinary rotation applies.

    Queue rows already own a title/body snapshot. Looking up the same text by
    a mutable library cursor (or a re-imported item ID) can select another task.
    Only the default copy pool gets this behaviour; custom pools, sequences,
    fixed text and explicitly independent rotation keep their original meaning.
    """
    if (not job or layer.get('kind') not in {'title', 'body', 'text'}
            or layer.get('source') != 'pool' or layer.get('pool_id')
            or layer.get('copy_binding', 'task') == 'rotate'):
        return None
    if not (job.get('copy_id') or imported_task(job)):
        return None
    current_profile = job.get('copy_profile_id', '')
    if source_profile and current_profile and source_profile != current_profile:
        return None  # An explicitly different project/profile is not this copy.
    field = layer.get('pool_field', 'title' if layer['kind'] == 'title'
                      else 'body' if layer['kind'] == 'body' else 'full')
    if field in {'title', 'body'}:
        text = str(job.get(field, ''))
    else:
        text = '\n'.join(str(job.get(key, '')) for key in ('title', 'body') if job.get(key))
    result = copy.deepcopy(layer)
    result.update(source='fixed', source_file='', text=text,
                  sequence_items=[], sequence_cursor=0)
    return result
