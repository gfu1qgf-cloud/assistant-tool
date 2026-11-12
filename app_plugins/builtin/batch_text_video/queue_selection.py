"""Atomic checkbox edits across projects; running render snapshots stay immutable."""
import copy
import uuid
from .render_selection import checked

ACTIVE = {'submitted', 'queued', 'running', 'canceling'}


def remove_jobs(state, selections, expected=None):
    """Remove queue records atomically; never touch media files or pool entries."""
    result = copy.deepcopy(state)
    profiles = {p['id']: p for p in result['profiles']}
    changed = False
    for identifier, values in selections.items():
        ids = set(values)
        if not ids: continue
        profile = profiles.get(identifier)
        if profile is None: raise ValueError('项目已不存在，请刷新总列表。')
        if expected is not None and profile.get('revision', 0) != expected.get(identifier):
            raise ValueError('项目已被其他窗口修改，请刷新后再删除。')
        if any(p['profile_id'] == identifier and p['status'] in ACTIVE for p in result['render_plans']):
            raise ValueError('正在渲染的项目不能删除任务，请先停止该项目。')
        data = result if result['active_profile'] == identifier else profile.get('data') or {}
        from .pools import migrate_resource_fields
        migrate_resource_fields(data)
        if not ids <= {j['id'] for j in data.get('jobs', [])}:
            raise ValueError('任务记录已变化，请刷新后再删除。')
        data['jobs'] = [j for j in data['jobs'] if j['id'] not in ids]
        plans = []
        for plan in result['render_plans']:
            if plan['profile_id'] == identifier:
                plan['ids'] = [value for value in plan['ids'] if value not in ids]
                plan['known_ids'] = [value for value in plan.get('known_ids', []) if value not in ids]
                if not plan['ids']: continue
            plans.append(plan)
        result['render_plans'] = plans
        from .pools import snapshot
        profile['data'] = snapshot(data)
        profile['revision'] = profile.get('revision', 0)+1
        changed = True
    if changed: result['revision'] += 1
    return result, changed


def apply_checks(state, changes, expected=None):
    result = copy.deepcopy(state)
    profiles = {p['id']: p for p in result['profiles']}
    changed = False
    for identifier, values in changes.items():
        profile = profiles.get(identifier)
        if profile is None: raise ValueError('项目已不存在，请刷新总列表。')
        if expected is not None and profile.get('revision', 0) != expected.get(identifier):
            raise ValueError('项目已被其他窗口修改，请刷新后再勾选。')
        if any(p['profile_id'] == identifier and p['status'] in ACTIVE for p in result['render_plans']):
            raise ValueError('正在渲染的项目暂时不能更改勾选；其他项目可以继续添加。')
        data = result if result['active_profile'] == identifier else profile.get('data') or {}
        from .pools import migrate_resource_fields
        migrate_resource_fields(data)
        jobs = data.get('jobs', [])
        by_id = {j['id']: j for j in jobs}
        if not isinstance(values, dict) or not set(values) <= set(by_id) or any(type(v) is not bool for v in values.values()):
            raise ValueError('任务勾选记录已变化，请刷新总列表。')
        edited = False
        for job_id, value in values.items():
            if by_id[job_id].get('render_checked') is not value:
                by_id[job_id]['render_checked'] = value; edited = True
        ids = [j['id'] for j in jobs if checked(j, result['render_plans'], identifier)]
        plan = next((p for p in result['render_plans'] if p['profile_id'] == identifier and p['status'] == 'staged'), None)
        known = [j['id'] for j in jobs]
        if not ids:
            if plan: result['render_plans'].remove(plan); changed = True
        elif plan:
            if plan['ids'] != ids or plan.get('known_ids') != known or plan['name'] != profile['name']:
                plan.update(ids=ids, known_ids=known, name=profile['name'], selection_managed=True, error=''); changed = True
        else:
            result['render_plans'].append(dict(id=uuid.uuid4().hex, profile_id=identifier, name=profile['name'],
                ids=ids, known_ids=known, status='staged', task_id='', error='', selection_managed=True)); changed = True
        if edited:
            from .pools import snapshot
            profile['data'] = snapshot(data)
            profile['revision'] = profile.get('revision', 0)+1
            changed = True
    if changed: result['revision'] += 1
    return result, changed


def stage_failed_retries(state,selections,expected=None):
    changes={}
    for pid,ids in selections.items():
        profile=next((p for p in state['profiles'] if p['id']==pid),None)
        data=state if state['active_profile']==pid else (profile or {}).get('data') or {}
        failed={j['id'] for j in data.get('jobs',[]) if j.get('status')=='需处理'}
        if not ids or len(ids)!=len(set(ids)) or not set(ids)<=failed:
            raise ValueError('失败任务已变化，请刷新后重试。')
        changes[pid]=dict.fromkeys(ids,True)
    result,changed=apply_checks(state,changes,expected)
    edited=False
    for pid,ids in selections.items():
        plan=next(p for p in result['render_plans'] if p['profile_id']==pid and p['status']=='staged')
        if plan['ids']!=list(ids) or not plan.get('retry_only'):
            plan.update(ids=list(ids),retry_only=True);edited=True
    if edited and not changed:result['revision']+=1
    return result,changed or edited
