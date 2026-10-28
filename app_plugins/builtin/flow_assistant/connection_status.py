"""One truthful, side-effect-free status model for the guide and queue UI."""
import time

from .setup_support import project_identity, project_block
from .credit_policy import balance_ready
from .account_policy import hold_reason
from .credit_cooldown import credit_reason, MIN_CREDITS
from .timing import value as timing_value
from .problem_details import explain, offline_retry, recovery_reason


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
    cooldown = credit_reason(client, preferences, now)
    from .high_credit import reason as high_credit_reason
    high_reason = high_credit_reason(client, preferences)
    blocks = preferences.get('browser_blocks', {})
    stored_block = next((blocks[key] for key in sorted(browser_keys(client)) if blocks.get(key)), '')
    closing=preferences.get('browser_lifecycle',{}).get(client.get('profile'),{})
    if online and not stored_block and not hold_reason(client,preferences) and closing.get('state')=='close_failed' and closing.get('last_error'):
        return {'connection':'已连接','project':project,'state':'自动关闭受阻',
            'reason':f'关闭尝试 {closing.get("close_attempts",0)}/3：'+closing['last_error']+
                '；已保留窗口，其他账号继续。','color':'#986100'}
    if online and not stored_block and not cooldown and not hold_reason(client,preferences):
        from .bridge_recovery import needed, EXTENSION_VERSION
        update=preferences.get('bridge_recoveries',{}).get(client.get('profile'),{})
        if update.get('target')==EXTENSION_VERSION and update.get('state')!='done' and needed(client,preferences):
            labels={'requesting':'等待UI权限打开更新入口','opening':'正在打开更新入口','awaiting':'入口已打开，等待新版扩展重连',
                    'waiting':'等待下一次自动重载','restarting':'已尝试安全重开，等待窗口退出确认','stopped':'自动恢复已停止'}
            return {'connection':'已连接','project':project,'state':'扩展自动恢复',
                'reason':labels.get(update.get('state'),'正在核对版本')+f'（重载 {update.get("count",0)}/2）：'+
                    str(update.get('error') or client.get('runtime_note') or '其他账号继续调度'),
                'color':'#b03728' if update.get('state')=='stopped' else '#986100'}
    repair = next((preferences.get('environment_recoveries', {}).get(key, {}) for key in sorted(browser_keys(client))
        if preferences.get('environment_recoveries', {}).get(key, {}).get('error') == stored_block), {})
    if not online:
        from .runtime_close import PAUSED
        if any(preferences.get('browser_blocks', {}).get(key) == PAUSED for key in browser_keys(client)):
            return {'connection': '未连接', 'project': project, 'state': '手动关闭／暂停', 'reason': PAUSED, 'color': '#986100'}
        failure = preferences.get('browser_launch_failures', {}).get(client.get('profile'), {})
        slot = preferences.get('browser_slots', {}).get(client.get('profile'), {})
        isolated = hold_reason(client, preferences)
        if isolated:
            return {'connection': '未连接', 'project': project, 'state': '账号已隔离', 'reason': explain(isolated), 'color': '#b03728'}
        if cooldown:
            return {'connection': '未连接', 'project': project, 'state': '积分冷却', 'reason': cooldown, 'color': '#986100'}
        from .exhausted_browser import reason as exhausted_reason
        exhausted = exhausted_reason(client, preferences, now)
        if exhausted:
            return {'connection':'未连接', 'project':project, 'state':'低积分已断开／已跳过', 'reason':exhausted, 'color':'#986100'}
        lifecycle = preferences.get('browser_lifecycle', {}).get(client.get('profile'), {})
        from .browser_lifecycle import AUTO_CLOSING
        if stored_block == AUTO_CLOSING and lifecycle.get('state') in {'closing','cooldown'}:
            if lifecycle['state'] == 'cooldown':
                seconds = max(0, round(lifecycle.get('reopen_at', now) - now))
                reason = ('已因积分低于10自动关闭，等待积分刷新／冷却结束后重新检测' if lifecycle.get('kind')=='low_credit'
                    else f'空闲备用窗口已收回；约 {seconds} 秒后可在有任务、名额时重新打开' if lifecycle.get('kind')=='idle_capacity'
                    else f'已确认浏览器安全关闭；约 {seconds} 秒后重新候选，有任务和名额时重试')
            else:
                reason = '自动关闭尚未收到窗口退出确认；仅保留此配置，其他账号继续调度，不重复启动浏览器'
            return {'connection':'未连接','project':project,'state':'自动关闭／冷却','reason':reason,'color':'#986100'}
        if stored_block and not failure:
            retrying = offline_retry(client, preferences, stored_block)
            detail = recovery_reason(repair or {'error': stored_block}, preferences, now, offline=True) if retrying or repair else explain(stored_block)
            return {'connection': '未连接', 'project': project,
                'state': '千分账号备用' if high_reason and retrying else '等待自动重试' if retrying else '需处理',
                'reason': detail + ('；' + high_reason if high_reason else ''), 'color': '#986100' if retrying else '#b03728'}
        if high_reason:
            return {'connection': '未连接', 'project': project, 'state': '千分账号备用', 'reason': high_reason, 'color': '#986100'}
        if failure or slot:
            if not failure and slot.get('state')=='opening' and now-slot.get('opened_at',slot.get('at',0))>=timing_value(preferences,'browser_startup_seconds'):
                return {'connection':'未连接','project':project,'state':'启动等待超时／已跳过',
                    'reason':'启动后未在时限内确认页面连接，已释放并行启动名额；保留原配置记录以防重复开窗，其他账号可继续。',
                    'color':'#b03728'}
            retry = preferences.get('browser_probe_retries', {}).get(client.get('profile'), {})
            rechecking = failure.get('retryable') and retry.get('count', 0) < timing_value(preferences, 'browser_probe_attempts')
            return {'connection': '未连接', 'project': project, 'state': '启动待处理' if failure else '等待原连接',
                    'reason': (('启动检查等待自动复查' if rechecking else '启动检查自动复查已达上限，可修正后恢复') +
                        f'（{retry.get("count",0)}/{timing_value(preferences,"browser_probe_attempts")}）；'
                        if failure.get('retryable') else '') +
                        (explain(failure.get('message') or '浏览器未成功启动') if failure and not rechecking else failure.get('message') or
                         '保留原配置连接记录，等待扩展重连；不因空闲窗口阻止其他账号') +
                        ('；上次页面问题：' + stored_block if stored_block else ''), 'color': '#b03728' if failure and not rechecking else '#986100'}
        return {'connection': '未连接', 'project': project, 'state': '待连接' if project_identity(saved) else '待配置',
                'reason': '等待打开此配置并连接扩展' if project_identity(saved) else '请先添加这个配置自己的 Flow 项目地址', 'color': '#986100'}
    environment_block = client.get('environment_block')
    # publish() includes the setup block in its aggregate environment field.
    # That alias is not a second independent failure of an auto-confirming page.
    if environment_block == client.get('setup_blocked') or environment_block == cooldown:
        environment_block = ''
    project_error = project_block(client, preferences, include_pending=False) if client.get('setup_pending') else ''
    account_error = hold_reason(client, preferences)
    recovery = client.get('connection_recovery') or {}
    blocked = '' if cooldown and client.get('blocked') == '积分不足' else client.get('blocked')
    block = account_error or project_error or environment_block or client.get('setup_blocked') or blocked or stored_block
    if (client.get('setup_pending') and not project_error and not environment_block
            and not client.get('blocked') and not stored_block):
        return {'connection': '已连接', 'project': project, 'state': '自动确认中',
                'reason': str(recovery.get('message') or client.get('setup_blocked') or '正在后台确认本配置，无需手动操作'), 'color': '#986100'}
    if block:
        if repair and not account_error and block == stored_block:
            stopped = repair.get('state') == 'stopped'
            return {'connection': '已连接', 'project': project,
                'state': '恢复失败／已跳过' if stopped else '自动恢复中',
                'reason': recovery_reason({**repair, 'wait_reason': client.get('environment_wait') or repair.get('wait_reason', '')}, preferences, now),
                'color': '#b03728' if stopped else '#986100'}
        return {'connection': '已连接', 'project': project, 'state': '账号已隔离' if account_error else '需处理', 'reason': explain(block), 'color': '#b03728'}
    active = next((row for row in jobs if row.get('status') in
        {'preparing', 'uploading', 'submitting', 'generating', 'downloading', 'uncertain', 'download_failed'}
        and (row.get('browser') == client['id'] or client.get('account') and
             row.get('remote', {}).get('account') == client['account'])), None)
    if active:
        if active.get('status') == 'uncertain':
            return {'connection': '已连接', 'project': project, 'state': '原结果待核对',
                'reason': (active.get('error') or '原任务是否成功提交尚未确认') +
                    '；此账号保留原结果核对，其他账号继续接单；需核对原项目是否已生成，确认未提交后可使用“完全重置并重新调度”，不要直接重复生成',
                'color': '#b03728'}
        if active.get('status') == 'download_failed':
            return {'connection': '已连接', 'project': project, 'state': '原结果待下载',
                'reason': (active.get('wait_reason') or active.get('error') or '正在核对并恢复原视频下载') +
                    '；仅保留原账号及视频绑定，其他账号继续接单', 'color': '#986100'}
        return {'connection': '已连接', 'project': project, 'state': '执行中',
                'reason': active.get('wait_reason') or active.get('error') or '正在处理已分配任务', 'color': '#206bb0'}
    if client.get('batch_running'):
        return {'connection': '已连接', 'project': project, 'state': '网页队列收尾中',
                'reason': '上一批网页队列仍在收尾，结束后自动继续下一条，无需手动停止', 'color': '#986100'}
    if client.get('reset_wait_until', 0) > now:
        return {'connection': '已连接', 'project': project, 'state': '等待旧操作结束',
                'reason': '任务已解除绑定；此浏览器暂等原操作结束，其他可用账号继续接单', 'color': '#986100'}
    from .setup_support import required_bridge_update
    upgrade = required_bridge_update(client, preferences)
    if upgrade:
        return {'connection':'已连接','project':project,'state':'扩展需更新',
            'reason':upgrade,'color':'#b03728'}
    reuse=client.get('reuse_recovery') or {}
    if reuse.get('state')=='stopped':
        return {'connection':'已连接','project':project,'state':'准备失败／已跳过',
            'reason':str(reuse.get('reason') or '账号准备未能完成')+'；已释放准备占位，其他账号继续。可用运行时检测积分或恢复账号重新核对。',
            'color':'#b03728'}
    if (client.get('credit_check') or {}).get('state') == 'checking':
        return {'connection': '已连接', 'project': project, 'state': '积分检测中',
                'reason': '正在读取此浏览器的账号和积分，完成后自动恢复接单', 'color': '#986100'}
    if cooldown:
        closing = (client.get('credit_close') or {}).get('message')
        return {'connection': '已连接', 'project': project, 'state': '积分冷却',
                'reason': cooldown + ('；' + closing if closing else ''), 'color': '#986100'}
    if high_reason:
        note = (client.get('credit_close') or {}).get('message') or client.get('backup_rotation_note')
        return {'connection': '已连接', 'project': project, 'state': '千分账号备用',
                'reason': high_reason + ('；' + note if note else ''), 'color': '#986100'}
    if (client.get('prepare_wait') or {}).get('next_at', 0) > now:
        return {'connection': '已连接', 'project': project, 'state': '自动等待中',
                'reason': '此浏览器的页面控件尚未稳定，短暂等待后自动重试；其他账号继续执行', 'color': '#986100'}
    if not client.get('page_ready'):
        reason = recovery.get('message') or client.get('ready_note') or client.get('page_error') or '扩展在线，正在自动等待 Flow 页面控件加载稳定'
    elif not client.get('account'):
        reason = client.get('note') or '页面已连接，正在首次读取 Flow 登录账号信息'
    elif not balance_ready(client, now, preferences):
        recheck = preferences.get('credit_balances', {}).get(str(client.get('account') or '').strip().casefold(), {}).get('recheck_after', 0)
        reason = ('已释放失败任务占用，重新读取实际积分后继续接单' if recheck > client.get('credits_at', 0) else
                  '本次账号尚未确认，已保留上次余额，等待重新读取账号' if client.get('identity_unconfirmed') else
                  '余额已过设置的有效期，保留上次值；有排队任务时空闲后重新检测')
    elif client['credits'] < MIN_CREDITS:
        reason = '积分低于 10，等待补充积分或备用账号'
    else:
        head = next((row.get('payload', {}) for row in jobs if row.get('status') == 'queued'), None)
        if head:
            from .policy import estimated_cost
            cost = estimated_cost(head, {})
            if cost is not None and client['credits'] < cost:
                return {'connection': '已连接', 'project': project, 'state': '积分不足当前任务',
                    'reason': f'当前队首 {head.get("duration")} 秒任务需要 {cost:g} 分，本账号剩余 {client["credits"]:g} 分；本条跳过此账号，继续寻找其他账号；后续任务保持提交顺序',
                    'color': '#986100'}
            from .image_affinity import allows_image
            if not allows_image(client,head,jobs,preferences):
                return {'connection':'已连接','project':project,'state':'其他图片独占',
                    'reason':'此账号保留原参考图的剩余任务，不接当前队首图片；调度继续寻找其他账号或启动备用浏览器',
                    'color':'#986100'}
        limits = [entry for key in browser_keys(client) for entry in preferences.get('parameter_blocks', {}).get(key, [])]
        note = ('；部分参数不可用：' + '、'.join(dict.fromkeys(str(e.get('duration')) + ' 秒' for e in limits)) +
                '，其他参数继续接单；修正后可恢复选中浏览器重新检测') if limits else ''
        return {'connection': '已连接', 'project': project, 'state': '可接单',
                'reason': '已确认项目和账号；积分按已提交费用本地计算' + note, 'color': '#187747'}
    return {'connection': '已连接', 'project': project, 'state': '未就绪', 'reason': reason, 'color': '#986100'}
