"""One non-blocking result warning per batch, not one modal per failed video."""
from qt_compat import QtCore, QtWidgets


def show_export_warning(parent, result):
    previous = getattr(parent,'_batch_warning',None)
    if previous is not None:
        previous.close(); previous.deleteLater()
    message = QtWidgets.QMessageBox(parent)
    message.setWindowTitle('文案视频导出：有项目需要处理')
    message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    message.setText(f"完成 {result.get('completed',0)} 个，需处理 {result.get('failed',0)} 个。")
    failures = result.get('failures',[]) or [result.get('error','请查看后台任务详情。')]
    message.setInformativeText('\n'.join(str(value) for value in failures[:3])[:1500]+
        ('\n其余项目请展开“详细信息”查看。' if len(failures)>3 else '')+
        '\n原音频和 SRT 未被改写；可以调整后仅重试需处理的项目。')
    message.setDetailedText('\n'.join(str(v) for v in failures)[:30000])
    message.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
    message.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    parent._batch_warning = message  # The stable dialog owns the native widget.
    message.show()
    return message
