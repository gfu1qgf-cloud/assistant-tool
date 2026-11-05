"""Text/image chat through the fixed official Responses endpoint, without tools."""


def is_chat_model(model):
    return (isinstance(model, str) and model.startswith(('gpt-', 'o1', 'o3', 'o4', 'chatgpt-'))
            and not any(part in model.lower() for part in (
                'codex', 'audio', 'realtime', 'transcribe', 'tts', 'image',
                'search', 'deep-research', 'moderation')))


def request_payload(model, messages, system, maximum, streaming):
    history = []
    for item in messages:
        role = item['role']
        if role not in {'user', 'assistant'}:
            raise ValueError('聊天记录只能包含用户与助手消息。')
        content = []
        if item.get('text'):
            content.append({'type': 'output_text' if role == 'assistant' else 'input_text',
                            'text': item['text']})
        for image in item.get('images', []):
            if role != 'user':
                raise ValueError('图片必须属于用户消息。')
            content.append({'type': 'input_image',
                            'image_url': 'data:' + image['mime'] + ';base64,' + image['data']})
        history.append({'role': role, 'content': content})
    payload = {'model': model, 'input': history, 'max_output_tokens': maximum,
               'stream': streaming, 'store': False}
    if system:
        payload['instructions'] = system
    return payload


def normalize_response(data):
    status = data.get('status')
    if status not in {'completed', 'incomplete'}:
        raise ValueError('OpenAI 回答未完成；未自动重发，避免重复计费。')
    parts = []
    for item in data.get('output') or []:
        if item.get('type') != 'message':
            continue
        for part in item.get('content') or []:
            if part.get('type') == 'output_text':
                parts.append(str(part.get('text') or ''))
            elif part.get('type') == 'refusal':
                raise ValueError('OpenAI 拒绝了本次请求；没有自动重试。')
    usage = data.get('usage') or {}
    reason = (data.get('incomplete_details') or {}).get('reason') if status == 'incomplete' else 'stop'
    if status == 'incomplete' and reason != 'max_output_tokens':
        raise ValueError('OpenAI 回答未完成（' + str(reason or '未知原因') + '）；未自动重发。')
    return {'model': data.get('model'), 'reported_model': data.get('model') or '', 'choices': [{'message': {'content': ''.join(parts)},
            'finish_reason': 'length' if status == 'incomplete' else 'stop'}],
            'usage': {'prompt_tokens': usage.get('input_tokens', 0),
                      'completion_tokens': usage.get('output_tokens', 0)}}
