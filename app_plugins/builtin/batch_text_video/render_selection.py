"""Checkbox render intent, independent of table selection and task contents."""
def checked(job, plans=(), profile_id=''):
    if 'render_checked' in job: return bool(job['render_checked'])
    pending=next((p for p in plans if p.get('profile_id')==profile_id
                  and p.get('status') in {'staged','submitted'}),None)
    if pending:
        if 'known_ids' in pending and job['id'] not in pending['known_ids']:
            return job.get('status')!='已完成'
        return job['id'] in pending['ids']
    return job.get('status')!='已完成'


def identifiers(state):
    return [job['id'] for job in state.get('jobs',[]) if checked(
        job,state.get('render_plans',()),state.get('active_profile',''))]
