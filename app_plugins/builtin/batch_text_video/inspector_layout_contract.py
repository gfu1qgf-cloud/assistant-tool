"""Fail before editing if a live process still owns the old inspector API."""


def require_flat_layout(panel):
    if not getattr(panel,'flat_sources',False) or not isinstance(getattr(panel,'targets',None),dict):
        panel.deleteLater()
        raise RuntimeError('控件属性布局已更新，当前进程仍使用旧界面；下次正常重启后生效，当前参数未修改。')
