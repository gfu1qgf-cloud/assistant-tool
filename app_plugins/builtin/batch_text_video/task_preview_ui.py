"""Read-only task preview with explicit, selective media replacement."""
import copy
from pathlib import Path
from qt_compat import QtCore,QtGui,QtWidgets
from model.QtThreadLifecycle import connect_thread_finished,thread_is_active
from .task_render_config import bound,media,replace


class TaskPreviewDialog(QtWidgets.QDialog):
    def __init__(self,job,state,directory,parent=None,*,services=None,submit=None,autostart=True):
        super().__init__(parent)
        self.job=copy.deepcopy(job);self.state=copy.deepcopy(state);self.directory=directory
        self.services,self.submit=services,submit
        self.frame_worker=self.overlay_worker=self.play_worker=None
        self.surface=self.cache_owner=None;self.pending=False;self.closing=False;self.generation=0
        self.setWindowTitle('任务配置预览 · '+job.get('name','任务'));self.resize(1050,760)
        layout=QtWidgets.QVBoxLayout(self)
        self.notice=QtWidgets.QLabel('已保存的任务配置' if job.get('render_config_id') else '旧任务无历史配置：当前设置＋已分配素材')
        layout.addWidget(self.notice)
        split=QtWidgets.QSplitter();layout.addWidget(split,1)
        left=QtWidgets.QWidget();column=QtWidgets.QVBoxLayout(left);column.setContentsMargins(0,0,0,0)
        from .preview import Preview
        self.preview=Preview();self.preview.setToolTip('任务配置快览');self.preview.show_bounds=False
        self.stack=QtWidgets.QStackedWidget();self.stack.addWidget(self.preview);column.addWidget(self.stack,1)
        from .preview_timeline import AnimationTimeline
        self.timeline=AnimationTimeline();column.addWidget(self.timeline)
        self.debounce=QtCore.QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(150)
        self.debounce.timeout.connect(self.refresh)
        self.timeline.timeChanged.connect(self.timeline_changed)
        split.addWidget(left)
        right=QtWidgets.QWidget();details=QtWidgets.QVBoxLayout(right);details.setContentsMargins(0,0,0,0)
        self.summary=QtWidgets.QLabel();self.summary.setWordWrap(True);details.addWidget(self.summary)
        self.items=QtWidgets.QTableWidget(0,2);self.items.setHorizontalHeaderLabels(['控件','素材'])
        self.items.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.items.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.items.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.items.horizontalHeader().setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.items.verticalHeader().hide();details.addWidget(self.items,1)
        row=QtWidgets.QHBoxLayout();self.change=QtWidgets.QPushButton('替换文件…');self.change.clicked.connect(self.choose_file)
        self.pool=QtWidgets.QPushButton('从素材池替换…');self.pool.clicked.connect(self.choose_pool)
        row.addWidget(self.change);row.addWidget(self.pool);details.addLayout(row)
        self.text=QtWidgets.QPlainTextEdit();self.text.setReadOnly(True);self.text.setMaximumHeight(140);details.addWidget(self.text)
        split.addWidget(right);split.setSizes([660,360])
        row=QtWidgets.QHBoxLayout();layout.addLayout(row)
        self.play=QtWidgets.QPushButton('播放预览');self.play.clicked.connect(self.play_preview);row.addWidget(self.play)
        self.design=QtWidgets.QPushButton('返回快览');self.design.clicked.connect(self.return_to_design);row.addWidget(self.design)
        self.seek=QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal);self.seek.setEnabled(False)
        self.seek.sliderReleased.connect(self.seek_playback);row.addWidget(self.seek,1)
        self.rerender=QtWidgets.QPushButton('重新渲染');self.rerender.clicked.connect(self.resubmit);row.addWidget(self.rerender)
        close=QtWidgets.QPushButton('关闭');close.clicked.connect(self.close);row.addWidget(close)
        self.status=QtWidgets.QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        self.populate()
        can_edit=submit is not None and bool(job.get('render_assignment'))
        self.change.setEnabled(can_edit);self.pool.setEnabled(can_edit);self.rerender.setEnabled(submit is not None)
        if not job.get('render_assignment'):
            self.rerender.setText('按当前配置渲染')
            self.change.setToolTip('尚无固定分配记录，不能保证只换这一项；请先完成一次正式导出。')
            self.pool.setToolTip(self.change.toolTip())
        if autostart:QtCore.QTimer.singleShot(0,self.refresh)

    def populate(self):
        self.entries=media(self.job,self.state)
        selected=self.items.currentRow();self.items.setRowCount(len(self.entries))
        for row,entry in enumerate(self.entries):
            for column,value in enumerate((entry['name'],Path(entry['path']).name if entry['path'] else '无')):
                item=QtWidgets.QTableWidgetItem(value);item.setToolTip(entry['path']);self.items.setItem(row,column,item)
        if self.entries:self.items.selectRow(max(0,min(selected,len(self.entries)-1)))
        job,state=bound(self.job,self.state)
        from .audio_sources import resolve_voice
        voice=resolve_voice(job,state)
        music=(job.get('render_assignment') or {}).get('music') or job.get('shared_music_assignment')
        song=Path(music['item']['path']).name if music else ('按当前音乐设置' if state['settings'].get('music_enabled') else '无')
        self.summary.setText('人声：'+(Path(voice).name if voice else '无')+'\n音乐：'+song)
        self.summary.setToolTip(voice or '')
        self.text.setPlainText('\n\n'.join(v for v in (job.get('title',''),job.get('body','')) if v))

    def chosen_id(self):
        row=self.items.currentRow()
        return self.entries[row]['id'] if 0<=row<len(self.entries) else None

    def choose_file(self):
        if self.chosen_id() is None:return
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,'替换此任务的素材','','图片／视频 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff *.mp4 *.mov *.mkv *.avi *.webm *.m4v);;所有文件 (*)')
        if path:self.replace_media(path)

    def choose_pool(self):
        from .layers import IMAGE_SUFFIXES
        from .engine import VIDEO_SUFFIXES
        choices=[]
        for name,items in [('默认图片池',self.state.get('image_pool',[]))]+[(p['name'],p['items']) for p in self.state.get('resource_pools',[]) if p['kind'] in {'image','video','visual'}]:
            for item in items:
                if Path(item['path']).suffix.casefold() in IMAGE_SUFFIXES|VIDEO_SUFFIXES:
                    choices.append((name+' · '+(item.get('name') or Path(item['path']).name),item['path']))
        choices.extend(('底层背景池 · '+Path(p).name,p) for p in self.state.get('backgrounds',[]))
        if not choices:QtWidgets.QMessageBox.information(self,'素材池','没有图片或视频素材。');return
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('选择替换素材');dialog.resize(540,460)
        layout=QtWidgets.QVBoxLayout(dialog);search=QtWidgets.QLineEdit();search.setPlaceholderText('搜索素材');layout.addWidget(search)
        listing=QtWidgets.QListWidget();layout.addWidget(listing)
        for label,path in choices:
            item=QtWidgets.QListWidgetItem(label);item.setData(QtCore.Qt.ItemDataRole.UserRole,path);item.setToolTip(path);listing.addItem(item)
        search.textChanged.connect(lambda value:[listing.item(i).setHidden(value.casefold() not in listing.item(i).text().casefold()) for i in range(listing.count())])
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        listing.itemDoubleClicked.connect(lambda _:dialog.accept())
        if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted and listing.currentItem():
            self.replace_media(listing.currentItem().data(QtCore.Qt.ItemDataRole.UserRole))

    def replace_media(self,path):
        try:
            self.job=replace(self.job,self.state,self.chosen_id(),path)
            self.populate();self.release_playback();self.refresh()
            self.notice.setText('待重新渲染：已替换所选素材')
        except Exception as error:QtWidgets.QMessageBox.warning(self,'未替换素材',str(error))

    def workers(self):return [v for v in (self.frame_worker,self.overlay_worker,self.play_worker) if v is not None]

    def timeline_changed(self):
        self.return_to_design();self.debounce.start()

    def start_worker(self,attribute,worker,ready):
        setattr(self,attribute,worker);worker.ready.connect(ready,QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(worker,self,attribute,lambda:self.finished_worker(attribute))
        worker.start()

    def finished_worker(self,attribute):
        setattr(self,attribute,None)
        if self.workers():return
        if self.closing:QtCore.QTimer.singleShot(0,self.close)
        elif self.pending:self.pending=False;QtCore.QTimer.singleShot(0,self.refresh)
        self.play.setText('暂停' if self.surface and self.surface.is_playing() else '播放预览')

    def refresh(self):
        if self.closing:return
        if self.workers():
            self.pending=True
            for worker in self.workers():worker.cancel.set()
            return
        self.generation+=1
        try:
            self.view_job,self.view_state=bound(self.job,self.state)
            from .audio_sources import resolve_voice
            from .ui import PreviewWorker
            settings=self.view_state['settings']
            self.status.setText('正在读取任务画面…')
            worker=PreviewWorker(self.view_job.get('path',''),settings['ffmpeg_path'],self,
                voice_path=resolve_voice(self.view_job,self.view_state),blur=settings.get('background_blur',0),
                frame_settings=settings,cache_directory=self.directory)
            self.start_worker('frame_worker',worker,self.frame_ready)
        except Exception as error:self.status.setText(str(error))

    def frame_ready(self,result):
        if self.closing or self.pending or result.get('cancelled'):return
        if result.get('error'):self.status.setText(result['error']);return
        settings=copy.deepcopy(self.view_state['settings']);job=self.view_job
        from .engine import output_size
        from .preview_timeline import editor_duration
        from .image_background import duration as image_duration
        media=result.get('media');width,height=output_size(settings,media)
        duration=editor_duration(settings,(media or {}).get('duration') or image_duration(settings),result.get('voice_duration'),has_voice=bool(result.get('voice_path')))
        self.timeline.set_duration(duration);settings['_editor_timing']=self.timeline.snapshot()
        settings['_preview_voice']=result.get('voice_path','')
        from .text_colors import job_color_scope
        from .palette_rules import palette_scope
        settings['_color_scope']=job_color_scope(job);settings['_palette_scope']=palette_scope(job)
        image=QtGui.QImage();image.loadFromData(result.get('image',b''))
        self.preview.background=image;self.preview.canvas_size=QtCore.QSize(width,height)
        self.preview.cover=settings['fit']=='cover'
        self.preview.update()
        from .ui import OverlayWorker
        from .layers import ImageCache
        from .design_preview import prepare_design_bundle
        try:bundle,registry=prepare_design_bundle(self.view_state,job,self.services)
        except Exception as error:self.status.setText(str(error));return
        worker=OverlayWorker((job.get('title',''),job.get('body',''),width,height,settings),ImageCache(),self,
            design_job=job,design_bundle=bundle,design_registry=registry,directory=self.directory)
        self.start_worker('overlay_worker',worker,self.overlay_ready)

    def overlay_ready(self,result):
        if self.closing or self.pending:return
        if result.get('error'):self.status.setText(result['error']);return
        self.preview.overlay=result['image'];self.preview.composite_planes=result.get('planes',[])
        self.preview.update();self.status.setText('；'.join(result.get('design_notices',[])) or '任务配置快览')

    def play_preview(self):
        if self.play_worker:self.play_worker.cancel.set();return
        if self.workers():self.status.setText('画面正在准备，请稍后播放。');return
        if self.surface:
            self.stack.setCurrentWidget(self.surface)
            if self.surface.is_playing():self.surface.pause()
            else:self.surface.play()
            self.play.setText('暂停' if self.surface.is_playing() else '播放预览')
            return
        try:
            job,state=bound(self.job,self.state)
            from .playback import PlaybackWorker,preview_key
            from .node_effects import prepare_preview_bundle
            from .cloud_alignment import prepare_binding
            from .export_validation import validate_caption_voice
            validate_caption_voice(state,[job]);binding=prepare_binding(state['settings'],self.services)
            bundle,registry=prepare_preview_bundle(state,job,self.services)
            worker=PlaybackWorker(job,state,self.directory,preview_key(job,state,bundle),self,node_bundle=bundle,node_registry=registry,elevenlabs_binding=binding)
            self.play.setText('取消准备');self.status.setText('正在准备完整播放预览…')
            self.start_worker('play_worker',worker,self.play_ready)
        except Exception as error:self.status.setText(str(error))

    def play_ready(self,result):
        owner=result.get('cache_owner')
        if self.closing or self.pending or result.get('cancelled') or result.get('error'):
            if owner:owner.close()
            if result.get('error'):self.status.setText(result['error'])
            return
        from model.MediaPlayback import FfplayReviewSurface
        self.cache_owner=owner;self.surface=FfplayReviewSurface(parent=self.stack,preload_enabled=False)
        self.surface.positionChanged.connect(lambda value:self.seek.setValue(round(value*1000)) if not self.seek.isSliderDown() else None)
        self.surface.errorOccurred.connect(self.status.setText);self.stack.addWidget(self.surface);self.stack.setCurrentWidget(self.surface)
        self.video_path,self.video_duration=result['path'],result['duration']
        self.seek.setRange(0,round(self.video_duration*1000));self.seek.setEnabled(True)
        self.surface.prepare(self.video_path,0,self.video_duration,0);self.surface.play()

    def seek_playback(self):
        if self.surface:
            self.surface.prepare(self.video_path,0,self.video_duration,self.seek.value()/1000);self.surface.play()

    def return_to_design(self):
        if self.surface:self.surface.pause()
        self.stack.setCurrentWidget(self.preview);self.play.setText('播放预览')

    def release_playback(self):
        if self.surface:
            self.surface.release();self.stack.removeWidget(self.surface);self.surface.deleteLater();self.surface=None
        if self.cache_owner:self.cache_owner.close();self.cache_owner=None
        self.seek.setEnabled(False)

    def resubmit(self):
        if not self.submit:return
        if self.workers():
            self.status.setText('请等预览完成，或关闭播放准备后重新渲染。');return
        try:
            self.submit(self.job);self.status.setText('已加入重新渲染队列。');self.rerender.setEnabled(False)
            self.change.setEnabled(False);self.pool.setEnabled(False)
        except Exception as error:QtWidgets.QMessageBox.warning(self,'未开始重新渲染',str(error))

    def closeEvent(self,event):
        self.closing=True;self.debounce.stop();self.release_playback()
        for worker in self.workers():worker.cancel.set()
        if any(thread_is_active(v) for v in self.workers()):event.ignore();self.hide();return
        super().closeEvent(event)


def open_task(panel,key,*,rerender=False):
    owner=panel.owner
    if not owner.is_batch_busy():
        owner.edit_timer.stop()
        if not owner.persist():raise ValueError(owner.status.text())
    state=owner.store.load()
    from .project_migration import profile_data
    from .store import DEFAULTS
    data=profile_data(state,key[0],DEFAULTS)
    private={**state,**data,'active_profile':key[0]}
    job=next(v for v in private['jobs'] if v['id']==key[1])
    profile=next(p for p in state['profiles'] if p['id']==key[0]);revision=profile.get('revision',0)
    # The preview owns one task, rather than copying all projects into each worker.
    private['jobs']=[job];private['profiles']=[{**profile,'data':None}];private['render_plans']=[]
    if job.get('render_config_id'):
        private['render_configs']={job['render_config_id']:private['render_configs'][job['render_config_id']]}
    def submit(edited):
        if panel.locked(key[0]):raise ValueError('此项目正在渲染，请结束本批后操作。')
        owner.state=owner.store.stage_task_rerender(key[0],key[1],edited,expected_profile_revision=revision)
        owner.refresh_tables()
        plan=next(p for p in reversed(owner.state['render_plans']) if p['profile_id']==key[0] and p['ids']==[key[1]] and p['status']=='staged')
        owner._launch_batch(plan['ids'],plan=plan)
    dialog=TaskPreviewDialog(job,private,owner.store.directory,owner,services=getattr(owner,'context',None),
        submit=None if panel.locked(key[0]) else submit)
    dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
    if rerender:dialog.rerender.setFocus()
    dialog.show()
    return dialog
