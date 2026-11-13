"""One right-click replacement; affected queue rows become checked pending jobs."""
from qt_compat import QtWidgets
from .asset_replace import libraries,selected


def add_actions(editor,menu,key):
    if key not in {name for name,_items in libraries(editor.state)}:return
    ids=editor.table_for(key).identifiers()
    action=menu.addAction('替换素材…',lambda:edit(editor,key))
    action.setEnabled(len(ids)==1)
    action.setToolTip('替换当前项目中此原文件的视觉引用；受影响任务自动勾选并标为待重新渲染。')


def edit(editor,key):
    if editor.is_batch_busy():return False
    ids=editor.table_for(key).identifiers()
    if len(ids)!=1:return False
    saved=False
    try:
        old=selected(editor.state,key,ids[0])
        from .visual_media import VISUAL_FILTER
        path,_=QtWidgets.QFileDialog.getOpenFileName(editor,'替换素材',old,VISUAL_FILTER)
        if not path:return False
        editor.edit_timer.stop()
        if not editor.persist():raise ValueError(editor.status.text())
        pid=editor.state['active_profile']
        profile=next(p for p in editor.state['profiles'] if p['id']==pid)
        state,info=editor.store.replace_visual_asset(pid,key,ids[0],path,
            expected_profile_revision=profile.get('revision',0),expected_source=old)
        editor.state=state
        saved=True
        from .thumbnails import service
        service().invalidate((old,path))
        from .store import file_identity
        if editor.preview_video and file_identity(editor.preview_video)==file_identity(old):editor.preview_video=info['new']
        loading=editor._loading;editor._loading=True
        try:
            editor.layer_panel.set_pool_state(state)
            editor.layer_panel.set_layers(state['settings']['layers'])
        finally:editor._loading=loading
        for values in (editor._render_progress,editor._render_stages):
            for identifier in info['affected_ids']:values.get(pid,{}).pop(identifier,None)
        editor.invalidate_playback();editor.reset_editor_history();editor.refresh_tables()
        flat=editor.render_queue_panel.flat
        if info['affected_ids']:
            flat.filter.setCurrentIndex(flat.filter.findData('dirty'));flat.search.clear()
        editor._preview_path=None;editor.refresh_preview()
        message=f"已替换素材；{len(info['affected_ids'])}条任务待重新渲染，已勾选。"
        if info['legacy_ids']:message+=f"其中{len(info['legacy_ids'])}条旧任务按当前设置补全配置。"
        editor.status.setText(message)
        return True
    except Exception as error:
        title='替换已保存，界面刷新失败' if saved else '未替换素材'
        editor.status.setText(title+'：'+str(error))
        from .export_results_ui import show_action_error
        show_action_error(editor,title,error)
        return False
