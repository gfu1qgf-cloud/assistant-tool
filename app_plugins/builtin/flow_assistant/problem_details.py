"""Explain a blocked account and the exact next action without hiding the error."""
import re


def temporary_code(message):
    text = str(message or '').strip()
    if (re.fullmatch(r'(?:error:\s*)?no current window\.?', text, re.I) or
            text == '浏览器响应超时，请检查页面后恢复；未发送新的生成请求。' or
            re.fullmatch(r'页面或浏览器窗口连续未就绪，自动等待已达\d+次；未提交生成。', text) or
            re.fullmatch(r'找不到视频时长选项，无法确认 (?:4|6|8|10) 秒；请检查当前模型的设置面板', text)):
        return 'FLOW_PAGE_NOT_READY'
    return ''


def offline_retry(client, preferences, message):
    if client.get('id') or not temporary_code(message): return False
    keys = [scope + ':' + str(client[field]) for scope, field in
            (('profile', 'profile'), ('browser', 'id')) if client.get(field)]
    plans = preferences.get('environment_recoveries', {})
    return not any(plans.get(key, {}).get('state') == 'stopped' for key in keys)


def manual_action(message):
    text = str(message or '')
    lower = text.casefold()
    if '用户手动标记账号不可用' in text:
        return '如需重新使用，在浏览器表右键选择“重置选中账号状态并重新检测”'
    if 'runtime_controls_v1' in text or '尚未加载运行时' in text:
        return '在此配置的扩展管理页重新加载 Flow 桥接扩展，再刷新原项目页'
    if any(word in lower for word in ('captcha', '验证码', '安全限制', 'unusual activity', '人机验证')):
        return '打开此配置的原项目页，完成网站验证或按网站提示解除限制，再恢复此账号'
    if any(word in lower for word in ('需要登录', '未登录', 'sign in', 'log in', '登录失效')):
        return '打开此配置并登录正确的 Flow 账号，再恢复此账号'
    if any(word in text for word in ('扩展', '版本', '安装路径')):
        return '在此配置的扩展管理页检查启用状态、安装路径和版本，再点“修正后恢复选中浏览器”'
    if any(word in text for word in ('手动', '用户已', '主动关闭')):
        return '如需重新使用，在浏览器列表中选中此配置并恢复；程序会保留你的暂停选择'
    if any(word in text for word in ('时长', '模型', '分辨率', '参数', '设置面板')):
        return '检查原项目是否提供所需模型、时长和分辨率；修正设置或任务参数后恢复此账号'
    if '项目' in text or '地址' in text or '权限' in text:
        return '检查此配置保存的项目地址及当前账号访问权限，再恢复此账号'
    if '窗口' in text or 'window' in lower:
        return '检查此配置的原项目窗口是否存在、页面是否能正常打开，再恢复此账号'
    return '打开此配置的原项目页，核对登录、页面加载和扩展连接，再点“修正后恢复选中浏览器”'


def explain(message):
    if '需要手动处理：' in str(message): return '原因：' + str(message)
    action = manual_action(message)
    if '手动重置账号状态' in str(message) and '重置选中账号状态并重新检测' not in action:
        action += '；处理后在浏览器表右键选择“重置选中账号状态并重新检测”'
    return '原因：' + str(message) + '；需要手动处理：' + action


def recovery_reason(entry, preferences, now, *, offline=False):
    from .timing import value
    error = str(entry.get('error') or '原页面尚未就绪')
    count = entry.get('count', 0)
    limit = value(preferences, 'connection_repair_attempts')
    if entry.get('state') == 'stopped':
        stop = entry.get('stop_reason') or f'原页面自动恢复已达上限（{count}/{limit}）'
        action = entry.get('manual_action') or manual_action(error)
        return f'原因：{error}；自动恢复已停止：{stop}；已跳过此账号，其他账号继续；需要手动处理：{action}'
    if offline:
        return f'上次问题：{error}；自动处理：队列需要时重新连接此配置，随后重试或刷新原项目页；无需手动解除旧错误'
    wait = entry.get('wait_reason') or ''
    remaining = max(0, int(entry.get('next_at', 0) - now + .999))
    step = '等待刷新后的原项目重新响应' if entry.get('state') == 'refreshing' else '将刷新原项目并重新核对'
    if remaining: step += f'，约 {remaining} 秒后再次检查'
    return f'原因：{error}；自动处理：{step}（{count}/{limit}）' + ('；当前等待：' + wait if wait else '') + '；其他账号继续接单，无需手动处理'
