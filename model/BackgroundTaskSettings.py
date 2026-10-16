"""Host scheduling admission limits; never overrides locks or running jobs."""
from copy import deepcopy

CONFIG_KEY='background_tasks'
MAX_PARALLEL=8
DEFAULTS={'max_parallel':3,'kind_limits':{'media':2,'network':2}}
VIDEO_PRIORITY={'max_parallel':6,'kind_limits':{'media':4,'network':2}}
FIXED_LIMITS={'cpu':1,'model':1}


def normalize_settings(value=None):
    if value is None:value={}
    if not isinstance(value,dict):raise ValueError('后台并发设置必须是对象。')
    kinds=value.get('kind_limits',{})
    if not isinstance(kinds,dict):raise ValueError('后台任务分类并发设置无效。')
    result=deepcopy(DEFAULTS)
    for name,label,source,target in (
        ('max_parallel','后台总并发',value,result),
        ('media','视频／媒体并发',kinds,result['kind_limits']),
        ('network','联网任务并发',kinds,result['kind_limits']),
    ):
        number=source.get(name,target[name])
        if type(number) is not int or not 1 <= number <= MAX_PARALLEL:
            raise ValueError(f'{label}须为1–{MAX_PARALLEL}的整数。')
        target[name]=number
    return result


def manager_options(value=None):
    settings=normalize_settings(value)
    return {'max_parallel':settings['max_parallel'],
            'kind_limits':{**settings['kind_limits'],**FIXED_LIMITS}}
