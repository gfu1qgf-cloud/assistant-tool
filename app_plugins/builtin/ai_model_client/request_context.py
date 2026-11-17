"""Prepare a bounded display/request copy without changing saved messages."""
from model.AIChatSources import text_with_sources


def select_context(history, prompt, *, rounds=20, text_limit=0,
                   include_history_images=True, system=''):
    rounds = max(0, min(50, int(rounds)))
    text_limit = max(0, int(text_limit))
    current = dict(role='user', text=text_with_sources(prompt['text'], prompt.get('metadata') or {}),
                   images=list(prompt.get('images') or []))
    base_chars = len(system) + len(current['text'])
    if len(prompt['text']) > 128000:
        raise ValueError('单条消息超过128000字符，请分段或缩短；原文没有被截断。')
    if len(system) > 16000:
        raise ValueError('系统提示超过16000字符，请缩短；原文没有被截断。')
    if text_limit and base_chars > text_limit:
        raise ValueError(f'当前消息和系统提示共{base_chars}字符，超过本次文字上限{text_limit}；请提高上限或缩短输入，不会截断你的消息。')
    successful = [row for row in history if row['status'] == 'done']
    recent = successful[-2 * rounds:] if rounds else []
    messages = [dict(role=row['role'], text=text_with_sources(row['text'], row.get('metadata') or {}),
                     images=list(row.get('images') or []) if include_history_images else []) for row in recent]
    total = base_chars + sum(len(row['text']) for row in messages)
    while messages and text_limit and total > text_limit:
        count = 2 if (len(messages) > 1 and messages[0]['role'] == 'user'
                      and messages[1]['role'] == 'assistant') else 1
        total -= sum(len(row['text']) for row in messages[:count])
        del messages[:count]
    return [*messages, current], dict(
        history_messages=len(messages), history_rounds=sum(row['role'] == 'user' for row in messages),
        omitted_messages=len(successful) - len(messages), text_chars=total,
        images=sum(len(row['images']) for row in [*messages, current]))
