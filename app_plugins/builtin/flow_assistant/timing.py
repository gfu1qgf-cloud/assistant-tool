"""One catalog for editable Flow timing, persisted values and wire settings."""
import math
from collections import namedtuple

CONFIG_KEY = 'flow_assistant_timing'

Spec = namedtuple('Spec', 'key group label default minimum maximum unit help')
SPECS = [Spec(*row) for row in (
    ('credit_validity_seconds', '积分与关闭', '已检测余额有效期', 21600, 0, 2592000, '小时', '0 表示不过期；重连保留最近余额，付费前仍核对实际账号。过期仅触发重新检测，不把余额清零。'),
    ('high_credit_confirmation_seconds', '积分与关闭', '千分账号使用确认时限', 5, 1, 60, '秒', '曾检测到至少 100 分的账号长期标记备用；小额账号用完或不可用后才询问。未明确选择“是”就跳过，不自动消耗。'),
    ('credit_cooldown_seconds', '积分与关闭', '低于 10 积分的冷却时间', 21600, 60, 2592000, '小时', '冷却期间不自动打开、检测或分配新任务；手动成功检测到至少 10 积分可提前解除。修改后按原冷却起点重算截止时间。'),
    ('credit_close_grace_seconds', '积分与关闭', '低积分浏览器关闭前等待', 30, 0, 86400, '秒', '检测完成后至少保留此时间。只有生成、下载和归档全部完成才核对关闭。'),
    ('credit_close_retry_seconds', '积分与关闭', '关闭浏览器失败重查间隔', 30, 1, 3600, '秒', '仍有任务、下载或未确认记录时保持浏览器打开，之后重新核对。'),
    ('credit_close_timeout_seconds', '积分与关闭', '关闭浏览器响应超时', 60, 5, 3600, '秒', '等待扩展完成关闭前核对的时间；超时保留原任务。'),
    ('credit_close_attempts', '积分与关闭', '关闭浏览器失败重试上限', 3, 1, 20, '次', '扩展明确忙碌不会消耗此次数；普通关闭失败达到上限后保持窗口。'),
    ('page_stale_seconds', '连接与启动', '页面未响应判定时间', 180, 10, 86400, '秒', '页面心跳超过此时间才标为失联。与积分有效期独立，页面失联不会删除余额。'),
    ('browser_startup_seconds', '连接与启动', '浏览器启动占位时间', 90, 10, 3600, '秒', '页面及首次积分尚未就绪时占用并行启动名额，避免连续打开整组浏览器。'),
    ('browser_launch_interval_seconds', '连接与启动', '连续启动浏览器最短间隔', 3, 1, 3600, '秒', '成功启动一个后才开始计时；仅连续启动下一个时等待，第一项及空闲后的新请求立即执行。'),
    ('browser_launch_jitter_seconds', '连接与启动', '连续启动间隔随机增量', 2, 0, 3600, '秒', '在最短间隔上随机增加 0～此秒数，默认合计 3～5 秒；0 使用固定最短间隔。不会在每次启动前重新强制等待。'),
    ('connection_repair_wait_seconds', '连接与启动', '连接无响应后开始修复', 60, 5, 3600, '秒', '仅对没有生成、下载及未决结果的原项目尝试恢复。'),
    ('connection_repair_delays', '连接与启动', '连接修复各次等待', (30, 60, 120), 1, 86400, '秒序列', '逗号分隔，依次使用；次数超过列表长度时使用最后一项。'),
    ('connection_repair_attempts', '连接与启动', '连接自动修复上限', 3, 1, 20, '次', '达到上限后显示具体原因，保留项目和任务。'),
    ('handshake_timeout_seconds', '连接与启动', '扩展首次握手超时', 5, 1, 300, '秒', '仅用于尚未认证的本机连接，不影响已认证任务。'),
    ('queue_tick_seconds', '连接与启动', '调度检查间隔', 1, .25, 10, '秒', '队列及超时检查频率；不会因此周期性打开积分菜单。'),
    ('status_publish_seconds', '连接与启动', '界面状态刷新间隔', 15, 1, 3600, '秒', '无其他状态变化时向面板更新状态的间隔。'),
    ('disk_check_seconds', '连接与启动', '下载磁盘状态缓存', 15, 1, 3600, '秒', '磁盘空间状态重新读取间隔。'),
    ('request_timeout_seconds', '超时与重试', '积分与普通请求响应超时', 60, 5, 3600, '秒', '等待扩展读取账号积分等普通请求。增大页面等待时，也应相应增大这里。'),
    ('prepare_timeout_seconds', '超时与重试', '参数准备响应超时', 110, 10, 7200, '秒', '应覆盖扩展等待消息通道、页面控件、参数和时长核对的总时间。'),
    ('prepare_snapshot_seconds', '超时与重试', '浏览器参数快照有效期', 21600, 0, 2592000, '小时', '首次完整核对后，同一页面、账号和参数复用快照，省略重复准备。0 关闭；提交时仍设置并确认真实参数及本条图片。'),
    ('upload_timeout_seconds', '超时与重试', '图片上传响应超时', 120, 10, 7200, '秒', '超时仅恢复图片传输，不重新提交生成。'),
    ('download_timeout_seconds', '超时与重试', '重试下载响应超时', 60, 5, 7200, '秒', '等待扩展确认原结果下载已开始。'),
    ('credit_retry_seconds', '超时与重试', '首次积分读取失败重试间隔', 5, 1, 3600, '秒', '只在页面可用且仍在重试次数内读取；不解除低积分冷却。'),
    ('credit_after_repair_seconds', '超时与重试', '页面修复后读取积分等待', 3, 0, 3600, '秒', '给原页面加载账号菜单的时间。'),
    ('credit_read_attempts', '超时与重试', '首次积分读取尝试上限', 3, 1, 20, '次', '失败仍展示上次成功余额及时间，不能把失败当作余额为零。'),
    ('generation_retry_attempts', '超时与重试', '明确生成失败后自动重试上限', 2, 0, 10, '次', '0 关闭。仅重试本次媒体已明确失败的任务；超时或结果不明不重新生成。批次预算和账号隔离继续生效。'),
    ('generation_retry_delays', '超时与重试', '生成失败各次重试等待', (120, 300), 30, 86400, '秒序列', '给网站退款和恢复留出时间，默认先等 2 分钟、再等 5 分钟。每次重新读取实际积分，不预先增加退款。'),
    ('generation_retry_credit_probe_seconds', '超时与重试', '生成重试前积分重查间隔', 60, 5, 3600, '秒', '只核对可用且空闲的账号；每个浏览器最多使用积分读取尝试上限，不解除低积分冷却。'),
    ('prepare_retry_delays', '超时与重试', '参数准备各次重试等待', (2, 5, 15), .1, 86400, '秒序列', '按浏览器计数；未提交生成时才重试准备。'),
    ('prepare_attempts', '超时与重试', '参数准备自动重试上限', 3, 1, 20, '次', '不适用于结果不明的付费提交。'),
    ('upload_retry_delays', '超时与重试', '图片上传各次重试等待', (5, 15, 30, 60, 120), 1, 86400, '秒序列', '只重试原图片上传；次数仍由连接与设置中的上传重试上限控制。'),
    ('download_retry_delays', '超时与重试', '视频下载各次重试等待', (5, 15, 30, 60, 120), 1, 86400, '秒序列', '只下载已绑定的原结果，不重新生成。'),
    ('transfer_wait_seconds', '超时与重试', '原浏览器离线或忙碌重查间隔', 5, 1, 3600, '秒', '等待原浏览器重连／空闲，继续原传输。'),
    ('upload_reassign_wait_seconds', '超时与重试', '提交前连接卡住后重新调度等待', 60, 5, 3600, '秒', '仅准备／上传且没有扣费或生成证据时，超过此时间解除原浏览器绑定，让其他可用浏览器接单。'),
    ('upload_reassign_attempts', '超时与重试', '提交前自动重新调度上限', 3, 0, 20, '次', '达到上限后保留记录，可手动完全重置；不会对结果不明的付费提交自动重新生成。0 关闭自动换浏览器。'),
    ('download_probe_seconds', '超时与重试', '下载绑定只读核对间隔', 15, 1, 3600, '秒', '等待原媒体绑定或签名地址时的快照检查间隔。'),
    ('download_probe_attempts', '超时与重试', '下载结果只读核对上限', 10, 1, 50, '次', '达到上限后等待原结果通知；准确结果迟到仍自动恢复下载，不重置实际下载重试次数。'),
    ('download_stall_seconds', '超时与重试', '下载无进展判定时间', 120, 10, 86400, '秒', '进入原视频下载恢复流程，独立于生成无进展时间。'),
    ('stall_seconds', '超时与重试', '生成无进展后自动核对', 1200, 120, 86400, '分钟', '兼容原来的无进展设置。只读核对原任务，始终不自动重新生成。'),
    ('generation_probe_seconds', '超时与重试', '生成无进展只读核对间隔', 60, 1, 3600, '秒', '无进展达到判定时间后，隔多久再检查原任务。'),
    ('generation_probe_attempts', '超时与重试', '生成无进展核对上限', 3, 1, 20, '次', '达到上限后保留原结果待核对。'),
    ('binding_probe_seconds', '超时与重试', '媒体归属缺失只读核对间隔', 15, 1, 3600, '秒', '只查询原提交的媒体身份，不刷新或重新生成。'),
    ('binding_probe_attempts', '超时与重试', '媒体归属只读核对上限', 3, 1, 20, '次', '达到上限仍接收原结果的后续通知。'),
    ('bridge_heartbeat_seconds', '扩展与页面', '扩展连接心跳间隔', 20, 1, 300, '秒', '扩展向主程序发送连接心跳；与页面快照分别计时。'),
    ('bridge_handshake_timeout_seconds', '扩展与页面', '扩展等待主程序握手超时', 15, 1, 300, '秒', '连接停在建立中或主程序未确认时，重建本机连接；不刷新项目或重新提交生成。'),
    ('bridge_heartbeat_timeout_seconds', '扩展与页面', '主程序心跳无响应超时', 90, 5, 3600, '秒', '扩展已连接但长期收不到主程序消息时自动重连；原任务及下载记录保留。'),
    ('bridge_reconnect_initial_seconds', '扩展与页面', '断线重连首次等待', 1, .1, 300, '秒', '失败后逐次加倍等待，最多等到下方上限。'),
    ('bridge_reconnect_max_seconds', '扩展与页面', '断线重连最大等待', 20, 1, 3600, '秒', '配置更新也不会清除最近一次有效余额。'),
    ('bridge_reconnect_alarm_seconds', '扩展与页面', '扩展后台唤醒检查间隔', 30, 30, 3600, '秒', 'Chrome 后台闹钟至少 30 秒，只检查连接，不启动生成。'),
    ('bridge_config_reload_seconds', '扩展与页面', '配置变化后重连等待', .05, .01, 10, '秒', '等待当前配置写入完成后重新建立本机连接。'),
    ('bridge_forward_wait_seconds', '扩展与页面', '等待页面消息通道', 45, 1, 3600, '秒', '项目脚本尚未就绪时允许等待多久；不另开标签页。'),
    ('bridge_forward_retry_seconds', '扩展与页面', '页面消息通道重查间隔', 1, .1, 60, '秒', '等待通道期间每次检查之间的时间。'),
    ('page_controls_wait_seconds', '扩展与页面', '等待页面控件就绪', 45, 1, 3600, '秒', '开始参数准备、上传或提交前等待真实页面控件。'),
    ('page_reference_reuse_wait_seconds', '扩展与页面', '已上传参考图重新挂载等待', 5, 1, 300, '秒', '复用素材无法在此时间内准确挂载时，停止等待并补传；主程序上传超时自动覆盖整个操作上限。'),
    ('page_initial_settle_seconds', '扩展与页面', '页面脚本启动稳定等待', 2, .1, 60, '秒', '脚本刚加载后等待状态恢复和页面挂载。'),
    ('page_dom_stable_seconds', '扩展与页面', '页面控件稳定等待', 1.5, .1, 60, '秒', '页面变动结束后等待控件稳定。'),
    ('page_settings_wait_seconds', '扩展与页面', '参数选中状态核对等待', 5, 1, 300, '秒', '等待实际选中模型、比例、分辨率和数量稳定，不能代替参数核对。'),
    ('page_duration_wait_seconds', '扩展与页面', '时长面板操作超时', 8, 1, 300, '秒', '等待打开时长面板并确认真实选中秒数。'),
    ('page_duration_reopen_seconds', '扩展与页面', '时长面板重新打开间隔', 2, .1, 60, '秒', '面板重建后重新定位打开的间隔。'),
    ('page_duration_options_stable_seconds', '扩展与页面', '可用时长选项稳定等待', 2, .1, 60, '秒', '只有稳定的选项才能证明不支持某时长。'),
    ('page_proof_valid_seconds', '扩展与页面', '参数核对结果有效期', 5, .1, 300, '秒', '超过有效期重新核对参数；不改变 Google 积分。'),
    ('page_snapshot_seconds', '扩展与页面', '网页状态心跳间隔', 15, 1, 300, '秒', '无变化时仍回报网页状态，建议小于页面未响应判定时间。'),
    ('page_poll_seconds', '扩展与页面', '网页状态检查间隔', 2, .1, 60, '秒', '扩展在本页检查状态的间隔，不打开积分菜单。'),
    ('page_control_poll_seconds', '扩展与页面', '网页控件等待检查间隔', .25, .05, 5, '秒', '等待控件、参数或时长时的检查间隔。'),
    ('page_health_wait_seconds', '扩展与页面', '网页健康检查响应等待', .3, .05, 10, '秒', '允许网页返回最新快照再判断是否空闲。'),
    ('page_health_valid_seconds', '扩展与页面', '网页健康快照有效期', 1.5, .1, 60, '秒', '关闭或修复页面前只使用新近返回的状态。'),
    ('page_repair_gap_seconds', '扩展与页面', '同一项目两次修复最短间隔', 30, 1, 3600, '秒', '防止项目刚刷新后再次刷新；任务忙碌时始终不刷新。'),
    ('setup_ticket_seconds', '配对与连接向导', '一次连接票据有效期', 120, 30, 3600, '秒', '仅本次明确连接请求可领取，票据仍为单次使用。'),
    ('setup_pair_wait_seconds', '配对与连接向导', '连接确认等待', 30, 5, 3600, '秒', '向导等待扩展领取配置及网页确认的时间。'),
    ('setup_update_wait_seconds', '配对与连接向导', '扩展更新后连接等待', 60, 5, 3600, '秒', '等待空闲配置加载扩展后重新连上。'),
    ('setup_retry_delays', '配对与连接向导', '向导各次重试确认等待', (60, 120, 120), 1, 3600, '秒序列', '重试次数超过列表时使用最后一项。'),
    ('setup_capture_seconds', '配对与连接向导', '识别项目地址等待', 300, 30, 3600, '秒', '只在当前向导所选配置的等待期内识别剪贴板项目地址。'),
    ('setup_clipboard_seconds', '配对与连接向导', '手动配对信息剪贴板保留', 120, 5, 3600, '秒', '仅清理由本工具复制且尚未被替换的内容。'),
    ('setup_claim_timeout_seconds', '配对与连接向导', '扩展领取配置超时', 10, 1, 300, '秒', '扩展设置页或项目页等待本机软件返回连接信息的时间。'),
    ('setup_status_wait_seconds', '配对与连接向导', '扩展设置页确认连接等待', 10, 1, 300, '秒', '配置已保存后检查主程序是否确实接受连接。'),
    ('setup_status_poll_seconds', '配对与连接向导', '扩展设置页连接检查间隔', .5, .1, 10, '秒', '等待确认连接时每次状态检查之间的时间。'),
    ('setup_discovery_timeout_seconds', '配对与连接向导', '查找待连接入口超时', 3, .5, 60, '秒', '仅查找当前配置自己的连接票据。'),
    ('setup_refresh_debounce_seconds', '配对与连接向导', '连接入口变化合并等待', .1, .01, 10, '秒', '项目地址变化时合并重复连接事件，不额外创建标签页。'),
    ('draft_save_seconds', '连接与启动', '手动提示词草稿保存等待', .45, .1, 10, '秒', '停止输入后保存本条草稿的等待时间。'),
    ('setup_auto_attempts', '配对与连接向导', '向导自动重试上限', 3, 1, 20, '次', '切换配置、关闭或连接成功会停止自动重试。'),
)]
CATALOG = {spec.key: spec for spec in SPECS}


def _checked(spec, raw):
    if spec.unit == '秒序列':
        if isinstance(raw, str):
            raw = raw.replace('，', ',').replace('、', ',').split(',')
            try: raw = [float(item.strip()) for item in raw]
            except ValueError: raise ValueError(f'{spec.label}：请输入逗号分隔的秒数') from None
        if not isinstance(raw, (tuple, list)) or not 1 <= len(raw) <= 20:
            raise ValueError(f'{spec.label}：需要 1～20 个等待时间')
        values = list(raw)
    else:
        values = [raw]
    if any(type(value) not in (int, float) or not math.isfinite(value) or
           not spec.minimum <= value <= spec.maximum for value in values):
        raise ValueError(f'{spec.label}：允许范围为 {spec.minimum:g}～{spec.maximum:g} 秒' if spec.unit != '次'
                         else f'{spec.label}：允许范围为 {spec.minimum:g}～{spec.maximum:g} 次')
    if spec.unit == '次' and int(raw) != raw: raise ValueError(f'{spec.label}：次数必须是整数')
    return values if spec.unit == '秒序列' else int(raw) if spec.unit == '次' else raw


def value(preferences, key):
    spec = CATALOG[key]
    settings = (preferences or {}).get('timing', {})
    if not isinstance(settings, dict): settings = {}
    raw = settings.get(key,
        (preferences or {}).get('stall_seconds', spec.default) if key == 'stall_seconds' else spec.default)
    try: return _checked(spec, raw)
    except (ValueError, TypeError, OverflowError):
        return list(spec.default) if spec.unit == '秒序列' else spec.default


def normalize(raw):
    if not isinstance(raw, dict): raise ValueError('时间设置格式无效')
    unknown = set(raw) - CATALOG.keys()
    if unknown: raise ValueError('未知时间设置：' + ', '.join(sorted(unknown)))
    return {key: _checked(CATALOG[key], item) for key, item in raw.items()}


def snapshot(preferences):
    return {spec.key: value(preferences, spec.key) for spec in SPECS}


def delay(preferences, key, index):
    values = value(preferences, key)
    return values[max(0, min(index, len(values) - 1))]


def request_timeout(preferences, action, client=None):
    key = {'prepare': 'prepare_timeout_seconds', 'upload_images': 'upload_timeout_seconds',
           'retry_download': 'download_timeout_seconds', 'close_exhausted': 'credit_close_timeout_seconds'}.get(action, 'request_timeout_seconds')
    seconds = value(preferences, key)
    if action == 'upload_images':
        controls = value(preferences, 'page_controls_wait_seconds')
        reuse = controls if (client or {}).get('extension_version') == '1.1.25' else value(preferences, 'page_reference_reuse_wait_seconds')
        floor = value(preferences, 'bridge_forward_wait_seconds') + 3 * controls + reuse + 20 * value(preferences, 'page_control_poll_seconds') + 5
        seconds = max(seconds, floor)
    if (client and client.get('extension_version') and client['extension_version'] not in {'1.1.21', '1.1.22', '1.1.23', '1.1.24', '1.1.25', '1.1.26'}
            and 'timing_settings_v1' not in client.get('capabilities', [])):
        seconds = max(seconds, CATALOG[key].default)
    return seconds


def validate(preferences):
    values = snapshot(preferences)
    if values['bridge_reconnect_max_seconds'] < values['bridge_reconnect_initial_seconds']:
        raise ValueError('断线重连最大等待不能小于首次等待')
    if values['bridge_heartbeat_timeout_seconds'] <= values['bridge_heartbeat_seconds']:
        raise ValueError('主程序心跳无响应超时应大于扩展连接心跳间隔')
    if values['page_stale_seconds'] <= values['page_snapshot_seconds'] + values['page_poll_seconds']:
        raise ValueError('页面未响应判定时间应大于网页状态心跳间隔与检查间隔之和')
    if values['page_health_valid_seconds'] <= values['page_health_wait_seconds']:
        raise ValueError('网页健康快照有效期应大于健康检查响应等待')
    prepare = sum(values[key] for key in ('bridge_forward_wait_seconds', 'page_controls_wait_seconds',
                   'page_settings_wait_seconds', 'page_duration_wait_seconds')) + 5
    if values['prepare_timeout_seconds'] < prepare:
        raise ValueError(f'参数准备响应超时应至少为 {prepare:g} 秒，以覆盖页面通道、控件、参数和时长核对')
    if values['upload_timeout_seconds'] < values['bridge_forward_wait_seconds'] + values['page_controls_wait_seconds'] + 5:
        raise ValueError('图片上传响应超时应覆盖页面通道和控件等待，并至少再留 5 秒')
    if values['request_timeout_seconds'] < values['bridge_forward_wait_seconds'] + 5:
        raise ValueError('普通请求响应超时应比页面消息通道等待至少多 5 秒')
    return values


def duration_label(seconds):
    unit, divisor = ('小时', 3600) if seconds >= 3600 else ('分钟', 60) if seconds >= 60 else ('秒', 1)
    return f'{seconds / divisor:g} {unit}'


def timing_changes(preferences, raw):
    """Prepare one validated transaction, including existing cooldown deadlines."""
    previous = preferences.get('timing', {})
    timing = {**(previous if isinstance(previous, dict) else {}), **normalize(raw)}
    merged = {**preferences, 'timing': timing}
    validate(merged)
    changes = {'timing': timing, 'stall_seconds': value(merged, 'stall_seconds')}
    if 'credit_cooldown_seconds' in timing:
        changes['credit_cooldowns'] = {account: {**entry,
            'until': entry['at'] + value(merged, 'credit_cooldown_seconds')}
            for account, entry in preferences.get('credit_cooldowns', {}).items()}
    return changes
