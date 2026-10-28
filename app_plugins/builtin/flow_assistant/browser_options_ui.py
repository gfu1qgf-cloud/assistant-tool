"""Compact grouped browser settings; explanations belong to tooltips."""
from qt_compat import QtWidgets
from .browser_options import CONFIG_KEY, normalize, snapshot


class BrowserOptionsSettings(QtWidgets.QWidget):
    def __init__(self, parent=None, *, preferences=None):
        super().__init__(parent)
        self.widget = self
        self.preferences = preferences or {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel('鼠标悬停在选项上查看说明；点击“确定”保存。', self))
        def group(title):
            box = QtWidgets.QGroupBox(title, self)
            layout.addWidget(box)
            return QtWidgets.QVBoxLayout(box)
        def option(box, key, title, help_text):
            edit = QtWidgets.QCheckBox(title, self)
            edit.setObjectName('flow_' + key)
            edit.setToolTip(help_text)
            edit.setAccessibleDescription(help_text)
            box.addWidget(edit)
            setattr(self, key, edit)
        window = group('窗口操作')
        option(window, 'focus_before_action', '自动激活浏览器窗口',
            '在参数设置、上传、提交、积分读取和视频预览恢复前，切到对应浏览器。关闭后，UI互斥仍可使用，'
            '程序不再主动切换前台；手动“激活窗口”仍可用。FLOW新开浏览器会请求后台最小化启动，'
            'Chrome复用已有进程时是否切换窗口仍可能受Chrome自身行为影响。此选项不影响Chrome页的手动启动。')
        option(window, 'ui_mutex', '串行执行网页操作（UI互斥，实验）',
            'FLOW的开窗、积分读取、参数设置、传图和提交共用互斥锁；视频生成和后台下载等待不占锁。'
            '此开关与自动激活独立，关闭自动激活也可继续串行执行。Chrome页的手动操作不加锁。默认关闭。')
        tasks = group('任务与参考图')
        option(tasks, 'credit_packs', '同图积分分包、连续提交',
            '已打开的同档空闲账号做有限联合规划，减少碎包；每包最多100分，不足100按实际余额，千分账号分轮使用。'
            '搜索有25毫秒及次数上限，超限保留可执行方案；逐条确认后随机等待1～3秒继续提交。'
            '生成和下载独立进行；每轮提交锁默认120秒，准备、传图和排队不计入。默认开启。')
        option(tasks, 'visual_upload_fallback', '图片识别失败时使用截图比对',
            '先按准确文件名和素材编号识别，再在本机内存中截图核对参考图。排除视频和不明确匹配，'
            '仍须核对唯一素材编号及首帧；截图不保存或上传。默认开启。')
        recovery = group('自动恢复')
        option(recovery, 'auto_bridge_recovery', '自动重载旧扩展并重连',
            '仅处理FLOW调度组内的空闲账号。两次重载未恢复时，尝试一次受保护的浏览器重开。'
            '个人标签页、正在生成或下载、未确认结果均阻止关闭；失败显示原因并继续其他账号。默认开启。')
        option(recovery, 'auto_close_low_credit', '积分低于10时，空闲后关闭浏览器',
            '有个人标签页或未完成操作时保留窗口；确认安全关闭后等待积分刷新或冷却结束。默认开启。')
        option(recovery, 'auto_reopen_page_faults', '暂时故障时，安全关闭并冷却重试',
            '积分读取或页面恢复连续失败后，空闲且仅有本项目标签页才关闭。确认退出后依次等待60秒、180秒、30分钟再候选。'
            '通常保留并行数对应的浏览器，等待和恢复时最多扩展到两倍，且总数不超过8个；空闲备用窗口自动收回。'
            '登录、验证码、网站限制和手动暂停不自动重开。默认开启。')
        layout.addStretch(1)
        self.load_config({})

    def load_config(self, config):
        options = normalize(config[CONFIG_KEY]) if CONFIG_KEY in config else snapshot(self.preferences)
        for key, value in options.items():
            getattr(self, key).setChecked(value)

    def update_config(self, config):
        config[CONFIG_KEY] = {key: getattr(self, key).isChecked() for key in snapshot(self.preferences)}
        return config

    def validate(self):
        normalize({key: getattr(self, key).isChecked() for key in snapshot(self.preferences)})
