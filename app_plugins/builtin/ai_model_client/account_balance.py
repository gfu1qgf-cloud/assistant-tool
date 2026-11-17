"""Read-only OpenRouter account snapshots; no model generation or disk cache."""
from decimal import Decimal, InvalidOperation
from model.AIProviderClient import AIProviderError
from model.OfflineMode import require_online
from model.SensitiveData import register_sensitive_values

LINKS = {
    'openai_balance': 'https://platform.openai.com/settings/organization/billing/overview',
    'openai_usage': 'https://platform.openai.com/usage',
    'openrouter_balance': 'https://openrouter.ai/settings/credits',
    'openrouter_activity': 'https://openrouter.ai/activity',
    'openrouter_management': 'https://openrouter.ai/settings/management-keys',
}


def validate_token(value):
    value=str(value).strip()
    if not value or len(value)>4096 or any(c.isspace() for c in value):
        raise ValueError('请填写有效的OpenRouter管理Key（不能包含空白）。')
    register_sensitive_values([value])
    return value


def amount(data, name, *, required=False):
    value=data.get(name)
    if value is None and not required:return None
    if isinstance(value,bool) or not isinstance(value,(str,int,float,Decimal)):
        raise AIProviderError('余额接口未提供有效金额：'+name)
    try:result=Decimal(str(value))
    except InvalidOperation:raise AIProviderError('余额接口返回了无效金额：'+name) from None
    if not result.is_finite() or abs(result)>Decimal('1e18'):
        raise AIProviderError('余额接口返回了无效金额：'+name)
    return result


def dollars(value):
    if value is None:return '接口未提供'
    text=format(value,',.6f').rstrip('0').rstrip('.')
    integer,_,fraction=text.partition('.')
    return '$'+integer+'.'+fraction.ljust(2,'0')


def query_openrouter(payload, job, *, client):
    require_online('查询OpenRouter余额与用量')
    job.check_canceled()
    kind=payload['kind']
    if kind not in {'key','account'}:raise ValueError('未知余额查询类型。')
    token=validate_token(payload['token'])
    path='key' if kind=='key' else 'credits'
    try:
        response=client._request('openrouter',token,'','https://openrouter.ai/api/v1/'+path,job)
    except AIProviderError as error:
        if kind=='account' and error.status==403:
            raise AIProviderError('账户总余额需要OpenRouter管理Key；普通聊天Key的剩余消费上限请用上方查询。',status=403) from None
        raise
    job.check_canceled()
    data=response.get('data') if isinstance(response,dict) else None
    if not isinstance(data,dict):raise AIProviderError('余额接口未返回有效数据；未将缺失值显示为零。')
    if kind=='account':
        total,used=amount(data,'total_credits',required=True),amount(data,'total_usage',required=True)
        if total<0 or used<0:raise AIProviderError('账户额度或累计用量无效。')
        return dict(kind=kind,total=total,used=used,remaining=total-used)
    fields={name:amount(data,name) for name in ('limit','limit_remaining','usage','usage_daily','usage_weekly','usage_monthly')}
    if not any(value is not None for value in fields.values()):
        raise AIProviderError('接口未提供Key用量；未将缺失值显示为零。')
    if any(value is not None and value<0 for name,value in fields.items() if name!='limit_remaining'):
        raise AIProviderError('Key额度或用量无效。')
    return dict(kind=kind,**fields,unlimited='limit' in data and data['limit'] is None,
        reset=data.get('limit_reset') if data.get('limit_reset') in ('daily','weekly','monthly') else None)
