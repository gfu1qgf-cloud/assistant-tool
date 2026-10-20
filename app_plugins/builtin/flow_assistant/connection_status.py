"""One truthful, side-effect-free status model for the guide and queue UI."""
import time

from .setup_support import project_identity, project_block
from .credit_policy import balance_ready


def browser_keys(client):
    return {prefix + str(client[key]) for prefix, key in
            (('profile:', 'profile'), ('browser:', 'id')) if client.get(key)}


def configured_profiles(preferences):
    projects = preferences.get('profile_projects', {})
    return [profile for profile in dict.fromkeys(preferences.get('profiles') or [])
            if project_identity(projects.get(profile))]


def browser_status(client, preferences, jobs=(), now=None):
    now = time.time() if now is None else now
    online = bool(client.get('id'))
    saved = preferences.get('profile_projects', {}).get(client.get('profile')) or client.get('saved_project')
    project = '页面已响应' if online and client.get('page_ready') else ('地址已保存' if project_identity(saved) else '未设置地址')
    if not online:
        return {'connection': '未连接', 'project': project, 'state': '待连接' if project_identity(saved) else '待配置',
                'reason': '等待打开此配置并连接扩展' if project_identity(saved) else '请先添加这个配置自己的 Flow 项目地址', 'color': '#986100'}
    blocks = preferences.get('browser_blocks', {})
    stored_block = next((blocks[key] for key in sorted(browser_keys(client)) if blocks.get(key)), '')
    environment_block = client.get('environment_block')
    # publish() includes the setup block in its aggregate environment field.
    # That alias is not a second independent failure of an auto-confirming page.
    if environment_block == client.get('setup_blocked'):
        environment_block = ''
    project_error = project_block(client, preferences, include_pending=False) if client.get('setup_pending') else ''
    block = project_error or environment_block or client.get('setup_blocked') or client.get('blocked') or stored_block
    if (client.get('setup_pending') and not project_error and not environment_block
            and not client.get('blocked') and not stored_block):
        return {'connection': '已连接', 'project': project, 'state': '自动确认中',
                'reason': str(client.get('setup_blocked') or '正在后台确认本配置，无需手动操作'), 'color': '#986100'}
    if block:
        return {'connection': '已连接', 'project': project, 'state': '需处理', 'reason': str(block), 'color': '#b03728'}
    active = next((row for row in jobs if row.get('status') in
        {'preparing', 'uploading', 'submitting', 'generating', 'downloading', 'uncertain', 'download_failed'}
        and (row.get('browser') == client['id'] or client.get('account') and
             row.get('remote', {}).get('account') == client['account'])), None)
    if active:
        return {'connection': '已连接', 'project': project, 'state': '执行中',
                'reason': active.get('wait_reason') or active.get('error') or '正在处理已分配任务', 'color': '#206bb0'}
    if (client.get('credit_check') or {}).get('state') == 'checking':
        return {'connection': '已连接', 'project': project, 'state': '积分检测中',
                'reason': '正在读取此浏览器的账号和积分，完成后自动恢复接单', 'color': '#986100'}
    if not client.get('page_ready'):
        reason = client.get('ready_note') or client.get('page_error') or '扩展在线，正在自动等待 Flow 页面控件加载稳定'
    elif not client.get('account'):
        reason = client.get('note') or '页面已连接，正在首次读取 Flow 登录账号信息'
    elif not balance_ready(client, now):
        reason = '等待本次连接首次读取积分；之后按提交费用本地计算'
    elif client['credits'] <= 0:
        reason = '积分不足，等待补充积分或备用账号'
    else:
        return {'connection': '已连接', 'project': project, 'state': '可接单',
                'reason': '已确认项目和账号；积分按已提交费用本地计算，不定时打开账号菜单', 'color': '#187747'}
    return {'connection': '已连接', 'project': project, 'state': '未就绪', 'reason': reason, 'color': '#986100'}
