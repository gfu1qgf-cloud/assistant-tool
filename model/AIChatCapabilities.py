"""Verified chat parameter families; unknown special features stay unavailable.

Keep capability policy independent of Qt and transport. Catalog metadata may
confirm OpenRouter parameters; a global API enum never proves model support.
"""
import math
import re


def capabilities(provider, model, row=None, options=None):
    name = str(model).lower().removeprefix('models/')
    options = options or {}
    result = {'efforts': (), 'pro': False, 'verbosity': False, 'budget': None,
              'web': False, 'sampling': True, 'note': '未确认的专属能力不发送，其他参数保留模型默认。'}
    if provider == 'openai':
        if name.startswith('gpt-6'):
            required = name.startswith(('gpt-6-astra', 'gpt-6.1-sol'))
            result.update(efforts=(() if required else ('none',)) + ('low','medium','high','xhigh','max'),
                          pro=True, verbosity=True, web=True)
        elif name.startswith('gpt-5'):
            if '-pro' in name:
                efforts = ('high',)
            elif re.match(r'gpt-5\.(?:[2-9]|[1-9][0-9])', name):
                efforts = ('none','low','medium','high','xhigh')
            elif name.startswith('gpt-5.1'):
                efforts = ('none','low','medium','high')
            else:
                efforts = ('minimal','low','medium','high')
            result.update(efforts=efforts, verbosity=True, web='nano' not in name,
                          pro=name.startswith('gpt-5.6') and '-pro' not in name)
        elif name.startswith(('o1','o3','o4')):
            result.update(efforts=('high',) if '-pro' in name else ('low','medium','high'),
                          web=name.startswith('o3') and '-pro' not in name)
        elif name.startswith(('gpt-4.1','gpt-4o')):
            result['web'] = 'nano' not in name
        if result['efforts']:
            result['sampling'] = 'none' in result['efforts'] and options.get('reasoning_effort') == 'none'
            if options.get('reasoning_effort') == 'minimal' and name.startswith('gpt-5'):
                result['web'] = False
    elif provider == 'gemini':
        if any(part in name for part in ('image','audio','live','tts')):
            pass
        elif name.startswith('gemini-2.5'):
            result['budget'] = (128,32768,False) if 'pro' in name else (512 if 'lite' in name else 1,24576,True)
        elif name.startswith('gemini-3'):
            if 'pro' in name:
                result['efforts'] = ('low','high')
            elif 'flash' in name:
                result['efforts'] = ('low','medium','high') if name.startswith(('gemini-3.7','gemini-3.8')) else ('minimal','low','medium','high')
        result['note'] = 'Gemini思考按代际适配。Google搜索所需的专用搜索建议展现尚未接入，当前禁用该服务联网，不冒充已支持。'
    elif provider == 'groq':
        if name in {'openai/gpt-oss-20b','openai/gpt-oss-120b'}:
            result['efforts'] = ('low','medium','high')
        elif name == 'qwen/qwen3.8-27b':
            result['efforts'] = ('none','low','medium','high')
        elif 'muse-spark' in name:
            result['efforts'] = ('low','medium','high','xhigh')
    elif provider == 'mistral':
        if name.startswith(('mistral-large-4','mistral-large-latest')):
            result['efforts'] = ('low','medium','high')
        elif name.startswith(('mistral-small-2603','mistral-small-latest','mistral-medium-3-5')):
            result['efforts'] = ('none','high')
        elif name.startswith(('zai-glm-5-3',)):
            result['efforts'] = ('low','high','max')
    elif provider == 'openrouter':
        supported = set((row or {}).get('supported_parameters') or ())
        if supported.intersection({'reasoning','reasoning_effort'}):
            result['efforts'] = ('none','minimal','low','medium','high','xhigh')
        result['verbosity'] = 'verbosity' in supported
        result['web'] = not options.get('free_only', True)
        result['note'] = '专属参数依据账号模型目录；路由平台可能映射思考等级。联网插件可另外收费，仅免费模式下禁用联网。'
    if not result['sampling']:
        result['note'] = '当前推理模型／等级不支持采样参数；温度与 Top P 不发送，原值保留。'
    return result


def apply_options(provider, model, payload, options, row=None):
    caps = capabilities(provider, model, row, options)
    effort = options.get('reasoning_effort') or 'default'
    if effort != 'default':
        if effort not in caps['efforts']:
            raise ValueError('当前模型不支持所选思考等级，请使用模型默认或更换等级。')
        if provider == 'openai': payload.setdefault('reasoning', {})['effort'] = effort
        elif provider == 'gemini': payload['generationConfig'].setdefault('thinkingConfig', {})['thinkingLevel'] = effort.upper()
        elif provider == 'openrouter': payload['reasoning'] = {'effort': effort}
        else: payload['reasoning_effort'] = effort
    mode = options.get('reasoning_mode') or 'default'
    if mode not in {'default','standard','pro'}: raise ValueError('推理模式无效。')
    if mode != 'default':
        if not caps['pro']: raise ValueError('当前模型未确认支持推理模式设置。')
        payload.setdefault('reasoning', {})['mode'] = mode
    budget = options.get('thinking_budget')
    if budget is not None:
        limits = caps['budget']
        if (not limits or type(budget) is not int or not
            (budget == -1 or budget == 0 and limits[2] or limits[0] <= budget <= limits[1])):
            raise ValueError('此模型的思考预算无效；请使用动态预算或允许的范围。')
        payload['generationConfig'].setdefault('thinkingConfig', {})['thinkingBudget'] = budget
    verbosity = options.get('verbosity') or 'default'
    if verbosity != 'default':
        if not caps['verbosity'] or verbosity not in {'low','medium','high'}:
            raise ValueError('当前模型不支持所选回答详略。')
        if provider == 'openai': payload.setdefault('text', {})['verbosity'] = verbosity
        else: payload['verbosity'] = verbosity
    target = payload['generationConfig'] if provider == 'gemini' else payload
    for option, key, low, high in (('temperature','temperature',0,2), ('top_p','topP' if provider == 'gemini' else 'top_p',0.01,1)):
        value = options.get(option)
        if value is not None:
            if not caps['sampling']: raise ValueError('当前推理设置不支持温度／Top P，请使用模型默认。')
            if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(option + ' 数值无效。')
            target[key] = value
    if options.get('web_search'):
        if not caps['web']: raise ValueError('当前模型／接口或免费模式未开放联网搜索。')
        if provider == 'openai':
            payload['tools'] = [{'type':'web_search'}]
            mode=options.get('search_mode','auto')
            if mode not in {'auto','required'}: raise ValueError('联网搜索模式无效。')
            payload['tool_choice'] = mode
            limit=options.get('search_limit',3)
            if type(limit) is not int or not 1<=limit<=20: raise ValueError('联网工具调用上限必须为1–20。')
            payload['max_tool_calls']=limit
        elif provider == 'gemini': payload['tools'] = [{'google_search':{}}]
        elif provider == 'openrouter': payload['plugins'] = [{'id':'web','max_results':3}]
    return caps
