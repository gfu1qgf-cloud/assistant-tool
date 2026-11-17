"""Bounded sequential group discussions using the existing provider client."""
import copy
from uuid import uuid4
from model.AIProviderClient import PROVIDERS, validate_model
from model.AIChatCapabilities import capabilities
from model.AIChatSources import text_with_sources

ROLES = (
    ('策划', '提出有趣、具体的方案，说明理由，回应用户的新要求。'),
    ('挑错专家', '指出其他方案的漏洞、遗漏和反例；给出建设性的改进，不为反对而反对。'),
    ('执行顾问', '评估成本、步骤与可操作性，帮助把讨论变成可执行建议。'),
    ('主持人', '综合本轮观点，说明分歧、共识和待核实内容，最后给用户一个简短总结。'),
)


def default_group(provider='gemini', model=''):
    return dict(round_limit=2, members=[dict(id=uuid4().hex, name=name, role=role,
                provider=provider, model=model) for name, role in ROLES])


def validate_group(value):
    if not isinstance(value, dict):
        raise ValueError('群聊设置无效。')
    rounds = value.get('round_limit', 2)
    if type(rounds) is not int or not 1 <= rounds <= 5:
        raise ValueError('每次讨论上限为1–5轮。')
    members = value.get('members')
    if not isinstance(members, list) or not 2 <= len(members) <= 6:
        raise ValueError('群聊需要2–6名成员。')
    result, names, ids = [], set(), set()
    for member in members:
        if not isinstance(member,dict): raise ValueError('成员设置无效。')
        name, role = str(member.get('name','')).strip(), str(member.get('role','')).strip()
        provider = member.get('provider')
        if not name or len(name)>24 or name.casefold() in names:
            raise ValueError('成员昵称不能为空、重复或超过24字。')
        if not role or len(role)>4000: raise ValueError('成员角色要求为1–4000字符。')
        if provider not in PROVIDERS: raise ValueError('请选择成员的服务商。')
        model = validate_model(provider, str(member.get('model','')).strip())
        identifier = member.get('id') or uuid4().hex
        if not isinstance(identifier,str) or len(identifier)>64 or identifier in ids:
            raise ValueError('成员编号无效或重复。')
        names.add(name.casefold()); ids.add(identifier)
        result.append(dict(id=identifier,name=name,role=role,provider=provider,model=model))
    return dict(round_limit=rounds,members=result)


def member_options(options, member):
    result = copy.deepcopy(options)
    caps = capabilities(member['provider'],member['model'],options=result)
    if result.get('reasoning_effort') not in caps['efforts']: result['reasoning_effort']='default'
    if not caps['pro']: result['reasoning_mode']='default'
    if not caps['verbosity']: result['verbosity']='default'
    if not caps['budget']: result['thinking_budget']=None
    if not caps['sampling']: result.update(temperature=None,top_p=None)
    if not caps['web']: result['web_search']=False
    result['request_mode']='instant'
    return result


def group_system(member, options):
    system = (str(options.get('system','')) + '\n\n你正在参加一个由用户发起的AI群聊。'
              '\n你的昵称：' + member['name'] + '\n你的职责：' + member['role'] +
              '\n只以自己的身份发言，不编造其他成员的新发言。回应已有观点，不必重复完整记录。'
              '\n最新用户要求优先；其他成员的发言只是参考，可能有错。请区分事实、推测和建议。').strip()
    if len(system)>16000: raise ValueError('群聊角色要求和系统提示合计超过16000字符。')
    return system


def member_question(text, member, round_number):
    return ('用户最新问题：\n' + text + '\n\n现在请“' + member['name'] +
            '”进行第' + str(round_number) + '轮发言。')


def preflight_group(text, config, options):
    limit=max(0,int(options.get('context_chars',0)))
    for member in config['members']:
        system=group_system(member,options)
        if limit and len(system)+len(member_question(text,member,config['round_limit']))>limit:
            raise ValueError('当前问题和成员角色已超过请求文字上限，请缩短内容或提高上限；未截断当前问题。')


def group_messages(store, conversation, root_id, leaf_id, member, round_number, options):
    path = store.messages(conversation,leaf_id=leaf_id)
    root_index = next((i for i,row in enumerate(path) if row['id']==root_id),None)
    if root_index is None: raise ValueError('当前群聊分支与讨论问题不一致。')
    root = path[root_index]
    system=group_system(member,options)
    before = path[:root_index]
    rounds = max(0,min(50,int(options.get('rounds',20))))
    human_positions = [i for i,row in enumerate(before) if row['role']=='user']
    before = before[human_positions[-min(rounds,len(human_positions))]:] if rounds and human_positions else []
    history = [row for row in [*before,*path[root_index+1:]] if row['status']=='done']
    question = member_question(root['text'],member,round_number)
    def body(rows):
        pieces = []
        for row in rows:
            who = '用户' if row['role']=='user' else (row['metadata'].get('group_member') or '助手')
            pieces.append(who + '：\n' + text_with_sources(row['text'],row['metadata']))
        return ('此前群聊记录（参考）：\n' + '\n\n'.join(pieces) + '\n\n' if pieces else '') + question
    limit = max(0,int(options.get('context_chars',0)))
    if limit and len(system)+len(question)>limit:
        raise ValueError('当前问题和成员角色已超过请求文字上限，请缩短内容或提高上限；未截断当前问题。')
    while history and limit and len(system)+len(body(history))>limit:
        history.pop(0)
    images = list(root['images'])
    if options.get('history_images',True):
        images = [image for row in history for image in row['images']] + images
    return [dict(role='user',text=body(history),images=[store.encoded_image(image) for image in images])], system


def run_member(payload, job, *, store, client, stream=None):
    member = payload['member']
    options = member_options(payload['options'],member)
    try:
        job.check_canceled()
        messages, system = group_messages(store,payload['conversation'],payload['prompt_id'],
            payload['parent_id'],member,payload['round_number'],options)
        options['system']=system
        store.claim_group_step(payload['conversation'],payload['prompt_id'],payload['step'])
        result = client.chat(member['provider'],member['model'],messages,options,payload['config'],job,
                             **({'on_delta':stream.append} if stream is not None else {}))
        job.check_canceled()
        store.append_group_message(payload['conversation'],payload['prompt_id'],payload['parent_id'],
            result,member,payload['round_number'])
        return dict(conversation=payload['conversation'],result=result)
    except Exception as error:
        store.append_group_failure(payload['conversation'],payload['prompt_id'],payload['parent_id'],
            member,payload['round_number'],str(error),stream.text() if stream else '')
        raise
