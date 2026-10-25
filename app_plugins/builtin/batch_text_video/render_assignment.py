"""Persist compact native media assignments before encoding, then publish freely."""
import copy


def capture(job, state, layers, music, voice):
    from .components import walk_components, CONTENT_FIELDS
    sources = {}
    for layer in walk_components(layers):
        assigned_source = layer.get('source') in {'sequence', 'pool'} or layer['kind'] == 'image' or layer['kind'] == 'caption' and layer.get('source') == 'file'
        if assigned_source and layer.get('enabled') and layer.get('opacity', 100):
            field = CONTENT_FIELDS.get(layer['kind'])
            if field:
                value = layer.get('_matting_binding', {}).get('source', {}).get('path', layer.get(field, '')) if field == 'path' else layer.get(field, '')
                sources[layer['id']] = {'field': field, 'value': value}
    selection = None
    if music:
        from .custom_pools import get_pool
        items = get_pool(state, music['pool_id'], 'music')['items'] if music.get('pool_id') else state['music']
        item = copy.deepcopy(next(value for value in items if value['id'] == music['id']))
        item.update(cursor=music['start'], audio_key=music['audio_key'])
        selection = {'item': item, 'settings': {key: state['settings'][key]
            for key in ('skip_silence', 'silence_db', 'silence_seconds')}}
    return dict(version=1, sources=sources, music=selection, background=job.get('path', ''), voice=voice)


def apply(job, state):
    assignment = job.get('render_assignment')
    if not assignment:
        return job, state
    if assignment.get('version') != 1:
        raise ValueError('任务素材分配版本不支持，请重新分配所选任务素材。')
    assignment = copy.deepcopy(assignment)
    from model.PrivateDataPaths import remap_path_fields
    remap_path_fields(assignment, {'background', 'voice', 'path'})
    for source in assignment['sources'].values():
        if source['field'] in {'path', 'caption_file'}:
            portable = {'path': source['value']}; remap_path_fields(portable, {'path'})
            source['value'] = portable['path']
    state, job = copy.deepcopy(state), copy.deepcopy(job)
    settings = state['settings']
    from .components import walk_components
    by_id = {layer['id']: layer for layer in walk_components(settings['layers'])}
    for identifier, value in assignment['sources'].items():
        if identifier not in by_id or by_id[identifier]['kind'] == 'node_effect':
            raise ValueError('已分配的控件被删除或替换，请先重新分配此任务的素材。')
        by_id[identifier].update(source='fixed', sequence_items=[], sequence_cursor=0,
                                 **{value['field']: value['value']})
    job.update(path=assignment['background'], voice_path=assignment['voice'], voice_override=True)
    settings.update(background_pool_id='', use_voice=bool(assignment['voice']))
    music = assignment.get('music')
    settings.update(music_enabled=bool(music), music_pool_id='')
    state['music'] = [copy.deepcopy(music['item'])] if music else []
    state['next_music_id'] = music['item']['id'] if music else ''
    if music:
        settings.update(music['settings'])
        state['_assigned_music_start'] = float(music['item']['cursor'])
    return job, state


def reserve(store, state, job_id, prepared):
    assignment = getattr(prepared, 'assignment', None)
    node_rotation = any(step.get('node_source') for step in prepared.sequence_plan)
    if node_rotation and next(job for job in state['jobs'] if job['id'] == job_id).get('render_assignment'):
        raise ValueError('工作流新增了节点资源轮换，请先重新分配此任务素材。')
    if not assignment or node_rotation:
        return state, False
    current = next(job for job in state['jobs'] if job['id'] == job_id)
    if not current.get('render_assignment'):
        reserved = store.completion_state(state, job_id, prepared.output, prepared.music, prepared.sequence_plan)
        updated = copy.deepcopy(state)
        updated['settings']['layers'] = reserved['settings']['layers']
        for key in ('music', 'next_music_id', 'resource_pools'):
            updated[key] = reserved[key]
        next(job for job in updated['jobs'] if job['id'] == job_id)['render_assignment'] = copy.deepcopy(assignment)
        state = store.save(updated, state['revision'])
    prepared.music, prepared.sequence_plan = None, []
    return state, True


def clear_selected(owner):
    if owner.is_batch_busy(): return
    selected = set(owner.selected_ids())
    old = {job['id']: copy.deepcopy(job['render_assignment']) for job in owner.state['jobs']
           if job['id'] in selected and job.get('render_assignment')}
    for job in owner.state['jobs']:
        if job['id'] in old: job.pop('render_assignment', None)
    if not owner.persist():
        for job in owner.state['jobs']:
            if job['id'] in old: job['render_assignment'] = old[job['id']]
        return
    owner.status.setText(f'已清除{len(old)}条任务的固定分配，下次导出重新分配；池位置不回退，原成品保留。')
