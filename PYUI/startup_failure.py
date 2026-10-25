"""Readable, redacted initialization failure dialog before the main window."""
from qt_compat import QtWidgets
from model.SensitiveData import redact_sensitive_text


def show_whisper_startup_failure(error, *, config_path, log_path=None, parent=None):
    message = QtWidgets.QMessageBox(parent)
    message.setIcon(QtWidgets.QMessageBox.Icon.Critical)
    message.setWindowTitle('程序初始化失败 · 字幕模型')
    message.setText('Whisper 字幕模型加载失败，程序将退出。')
    message.setInformativeText(
        '已有配置和本次向导创建的配置都会保留，不会删除或重置。\n\n'
        '请检查网络/代理、磁盘空间及本地模型是否完整；若提示 DLL 或依赖错误，请恢复完整程序包。'
        '修复后重新启动即可，无需重新填写配置。\n\n'
        f'配置位置：{config_path}\n日志位置：{log_path or "日志尚未建立"}')
    message.setDetailedText(redact_sensitive_text(f'{type(error).__name__}: {error}'))
    message.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Close)
    message.button(QtWidgets.QMessageBox.StandardButton.Close).setText('退出程序')
    for button in message.buttons():
        if message.buttonRole(button) == QtWidgets.QMessageBox.ButtonRole.ActionRole:
            button.setText('错误详情')
            button.clicked.connect(lambda checked=False, current=button: current.setText('错误详情'))
    message.exec()


def exit_after_whisper_failure(error, *, app, storage, config_path, log_path=None):
    """Called before MainDialog is constructed; no plugin workers exist yet."""
    try:
        show_whisper_startup_failure(error, config_path=config_path, log_path=log_path)
    finally:
        from model.AppLogger import shutdown_application_logging
        shutdown_application_logging(close=True)
        if storage is not None:
            storage.close()
        from PyQt6 import sip
        sip.delete(app)
    return 3
