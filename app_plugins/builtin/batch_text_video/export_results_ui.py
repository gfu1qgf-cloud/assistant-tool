"""One non-blocking result warning per batch, not one modal per failed video."""
from qt_compat import QtCore, QtWidgets


def voice_guidance(parent,message):
    if '动态字幕需要人声' not in message:return ''
    return '启用人声并选择来源，或关闭对应动态字幕。'


def translate_buttons(box):
    button=box.button(QtWidgets.QMessageBox.StandardButton.Ok)
    if button is not None:button.setText('知道了')
    for button in box.buttons():
        if 'details' in button.text().lower():button.setText('详细信息…')


def locate_voice(parent):
    parent.right_tabs.setCurrentIndex(1)
    parent.project_sections.sections['audio'].set_expanded(True)
    card=parent.audio_cards['voice']
    parent.right_tabs.widget(1).ensureWidgetVisible(card)
    parent.controls['use_voice'].setFocus()


def show_action_error(parent,action,error,*,details='',profile=''):
    previous=getattr(parent,'_action_warning',None)
    if previous is not None:
        previous.close();previous.deleteLater()
    box=QtWidgets.QMessageBox(parent)
    box.setWindowTitle(action)
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    text=str(error).strip() or '操作未完成。'
    box.setText('动态字幕需要人声，当前没有可用音频。' if '动态字幕需要人声' in text else text.splitlines()[0][:500])
    hint=voice_guidance(parent,text)
    information='\n'.join(part for part in (('项目：'+profile if profile else ''),hint,
        '\n'.join(text.splitlines()[1:])[:700]) if part)
    if information:box.setInformativeText(information)
    if details or len(text)>500:box.setDetailedText((text+'\n\n'+details)[:30000])
    box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
    if hint and hasattr(parent,'audio_cards') and not profile:
        button=box.addButton('人声设置',QtWidgets.QMessageBox.ButtonRole.ActionRole)
        button.clicked.connect(lambda:locate_voice(parent))
    box.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    translate_buttons(box)
    parent._action_warning=box;box.show();box.raise_()
    return box


def show_export_warning(parent, result, *, profile=''):
    previous = getattr(parent,'_batch_warning',None)
    if previous is not None:
        previous.close(); previous.deleteLater()
    message = QtWidgets.QMessageBox(parent)
    message.setWindowTitle('文案视频导出：有项目需要处理')
    message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    message.setText('本批导出未完成。' if result.get('error') and not result.get('failed') else
                    f"完成 {result.get('completed',0)} 个，需处理 {result.get('failed',0)} 个。")
    failures = result.get('failures',[]) or [result.get('error','请查看后台任务详情。')]
    message.setInformativeText('\n'.join(str(value) for value in failures[:3])[:1500]+
        ('\n其余项目请展开“详细信息”查看。' if len(failures)>3 else '')+
        ('\n项目：'+profile if profile else '')+
        ('\n'+voice_guidance(parent,'\n'.join(map(str,failures))) if voice_guidance(parent,'\n'.join(map(str,failures))) else ''))
    message.setDetailedText('\n'.join(str(v) for v in failures)[:30000])
    message.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
    message.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    translate_buttons(message)
    parent._batch_warning = message  # The stable dialog owns the native widget.
    message.show()
    return message
