"""Stateless AI compute node. Transport, keys and encrypted caching are host-owned."""
import base64
import math
from pathlib import Path
import re

from app_plugins.api import NodeDefinition, NodeParameter, NodePort
from model.AIImages import normalize_image
from model.AIProviderClient import PROVIDERS, validate_model
from model.node_workflow.contracts import FileList, ResourceRef
from model.node_workflow.python_values import PythonValue, parse_json

KIND = 'ai_model_client.request'
YES_NO = (('no', '关闭'), ('yes', '允许（可能消耗额度）'))


def validate_params(params):
    validate_model(params['provider'], params['model'])
    for key, low, high in (('max_tokens', 128, 32768), ('request_limit', 1, 100),
                            ('retries', 0, 2), ('cache_revision', 1, 1000000)):
        if type(params[key]) is not int or not low <= params[key] <= high:
            raise ValueError(key + ' 超出允许范围。')
    if type(params['temperature']) not in (int, float) or not math.isfinite(params['temperature']) or not 0 <= params['temperature'] <= 2:
        raise ValueError('温度必须为 0–2 的有限数值。')
    for key, choices in (('output_format', {'text', 'json'}), ('cache', {'reuse', 'off'}),
                          ('preview_network', {'no', 'yes'}), ('free_only', {'no', 'yes'}),
                          ('foreign_only', {'no', 'yes'}), ('temperature_mode', {'default', 'custom'})):
        if params[key] not in choices:
            raise ValueError(key + ' 选项无效。')
    if not isinstance(params['prompt'], str) or not isinstance(params['system'], str):
        raise ValueError('提示词与系统指令必须是文本。')
    if len(params['prompt']) > 64000 or len(params['system']) > 16000:
        raise ValueError('提示词最多64000字符，系统指令最多16000字符；未静默截断。')


def prepare(_task, params, services):
    validate_params(params)
    return services.prepare_ai_workflow(params['provider'])


def image_inputs(inputs, context):
    refs = []
    files = inputs.get('images')
    if files is not None:
        if not isinstance(files, FileList):
            raise ValueError('图片列表必须来自显式文件列表节点。')
        if len(files.files) > 3:
            raise ValueError('每次最多3张图片，请先筛选列表；不会只取前三张。')
        files.verify(context.check_canceled)
        refs.extend(files.files)
    path = inputs.get('image_path')
    if path:
        if not isinstance(path, str) or re.match(r'^[a-z][a-z0-9+.-]*://', path, re.I):
            raise ValueError('图片路径只接受你明确连接的本地文件，不下载网址或读取聊天附件。')
        refs.append(ResourceRef.capture(Path(path), 'image'))
    if len(refs) > 3:
        raise ValueError('图片列表与单张路径合计最多3张。')
    images = []
    for ref in refs:
        context.check_canceled()
        ref.verify()
        raw, _suffix, mime, _width, _height = normalize_image(ref.path)
        ref.verify()
        images.append({'mime': mime, 'data': base64.b64encode(raw).decode('ascii')})
    return images, refs


def response_data(text, output_format):
    if output_format == 'text':
        return PythonValue.capture(text)
    value = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n([\s\S]*?)\n```', value, flags=re.I)
    if fenced:
        value = fenced.group(1)
    try:
        return PythonValue.capture(parse_json(value))
    except (ValueError, TypeError) as error:
        raise ValueError('模型没有返回严格有效的 JSON（拒绝重复键、NaN、杂文和超限数据）。'
                         '未将它记为成功缓存；请调整提示词后重试，不会执行回答中的代码。') from error


def build(service, inputs, params, context):
    validate_params(params)
    prompt = inputs.get('prompt') if inputs.get('prompt') is not None else params['prompt']
    system = inputs.get('system') if inputs.get('system') is not None else params['system']
    text = inputs.get('text') or ''
    if not all(isinstance(item, str) for item in (prompt, system, text)):
        raise ValueError('AI 的提示词、内容和系统指令必须是文本。')
    if len(prompt) + len(text) > 64000 or len(system) > 16000:
        raise ValueError('AI 输入过长：提示词＋内容最多64000字符，系统指令最多16000字符；未截断。')
    images, refs = image_inputs(inputs, context)
    user_text = '\n\n'.join(value for value in (prompt, text) if value.strip())
    if not user_text.strip() and not images:
        raise ValueError('AI 节点没有提示词、内容或图片，未发送请求。')
    if params['output_format'] == 'json':
        system += '\n请只返回一个严格合法的 JSON 值，不附带说明、Markdown 或代码。'
    if len(system) > 16000:
        raise ValueError('JSON 输出指令加入后，系统指令超过16000字符。')
    options = {'system': system, 'max_tokens': params['max_tokens'],
        'temperature': params['temperature'] if params['temperature_mode'] == 'custom' else None,
        'stream': False, 'free_only': params['free_only'] == 'yes',
        'cf_free_only': params['free_only'] == 'yes', 'foreign_only': params['foreign_only'] == 'yes'}
    result, cached = service.execute(getattr(context, 'node_prepared', {}).get('binding', ''),
        params['provider'], validate_model(params['provider'], params['model']),
        [{'role': 'user', 'text': user_text, 'images': images}], options, context,
        cache=params['cache'] == 'reuse', revision=params['cache_revision'],
        allow_preview=params['preview_network'] == 'yes', request_limit=params['request_limit'],
        retries=params['retries'], output_format=params['output_format'],
        validate_result=lambda answer: response_data(answer['text'], params['output_format']))
    for ref in refs:
        ref.verify()
    return {'text': result['text'], 'data': response_data(result['text'], params['output_format']),
        'metadata': {key: result[key] for key in ('provider', 'model', 'usage', 'elapsed', 'warning')},
        'cached': cached, 'model': result['model']}


def register_nodes(context):
    # Resolve the host service at registration on the GUI thread. The worker
    # closure captures only this plain service, never PluginContext/a widget.
    service = context.ai_workflows
    context.register_node(NodeDefinition(KIND, 'AI 调用',
        inputs=(NodePort('prompt', '提示词 · 覆盖参数', 'text', False),
                NodePort('text', '待处理文字', 'text', False),
                NodePort('system', '系统指令 · 覆盖参数', 'text', False),
                NodePort('images', '图片文件列表 · 最多3张', 'files', False),
                NodePort('image_path', '单张本地图片路径', 'text', False)),
        outputs=(NodePort('text', '回答文本', 'text'), NodePort('data', '回答数据 · 文本或JSON', 'data'),
                 NodePort('metadata', '模型／用量报告', 'mapping'), NodePort('cached', '复用缓存', 'boolean'),
                 NodePort('model', '实际模型 ID', 'text')),
        parameters=(
            NodeParameter('provider', '服务商', 'choice', 'gemini', choices=tuple(PROVIDERS.items())),
            NodeParameter('model', '具体模型 ID · 与试用台相同', default='', required=True),
            NodeParameter('prompt', '提示词 · 接线后由输入覆盖', 'multiline', '请处理以下内容，直接返回结果。', required=False),
            NodeParameter('system', '系统指令', 'multiline', '', required=False),
            NodeParameter('output_format', '输出格式', 'choice', 'text', choices=(('text', '文本'), ('json', '严格 JSON'))),
            NodeParameter('max_tokens', '最大输出 Token', 'integer', 1024, 128, 32768),
            NodeParameter('temperature_mode', '温度设置', 'choice', 'default', choices=(('default', '使用模型默认值'), ('custom', '自定义'))),
            NodeParameter('temperature', '温度', 'number', .2, 0, 2),
            NodeParameter('cache', '回答缓存', 'choice', 'reuse', choices=(('reuse', '复用加密缓存'), ('off', '不缓存 · 每次执行都请求'))),
            NodeParameter('cache_revision', '缓存版本号 · 改动可重新生成', 'integer', 1, 1, 1000000),
            NodeParameter('preview_network', '允许预览联网', 'choice', 'no', choices=YES_NO),
            NodeParameter('free_only', '仅免费／每日额度模型 · OR和CF', 'choice', 'yes', choices=(('yes', '开启 · 不保证账户零计费'), ('no', '关闭 · 允许付费模型'))),
            NodeParameter('foreign_only', '仅允许已知国外厂商模型', 'choice', 'yes', choices=(('yes', '开启'), ('no', '关闭'))),
            NodeParameter('request_limit', '本次执行生成调用上限 · 含重试', 'integer', 20, 1, 100),
            NodeParameter('retries', '临时服务错误额外重试次数', 'integer', 0, 0, 2)),
        build=lambda inputs, params, job: build(service, inputs, params, job), prepare=prepare,
        validate_parameters=validate_params, execution='compute', category='AI',
        keywords=('AI', 'Gemini', 'Mistral', 'OpenRouter', 'Cloudflare', 'Groq', 'JSON', '图片'),
        description='复用“AI 模型试用台→连接设置”的密钥；模型ID需明确填写，不随机路由。'
                    '只发送接入的文字与图片，无聊天历史、无整行任务隐式上传。'
                    '文字接入→AI调用→预览（回答文本）或Python数据输出；支持Line/Detail及插件工作流。'
                    '编辑画布不会请求，预览默认只读缓存；需明确允许预览联网才会发送。'
                    '每家服务最多2个并发，缓存只存加密回答，默认不重试；401/403/余额不足不重试。'
                    '请求上限按单次工作流上下文计数，Line每行独立；模型可能产生费用，Token不等于价格。'
                    'JSON是数据而非可执行代码；不自动接入或执行Python代码。'))
