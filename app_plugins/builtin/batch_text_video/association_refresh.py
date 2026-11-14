"""Explicit project-wide association refresh; no polling and no media decoding."""
import copy
import threading
from pathlib import Path
from qt_compat import QtCore,QtWidgets
from model.QtThreadLifecycle import connect_thread_finished
from .task_associations import for_state,apply,explicit_voice


def scan(state,cancel,progress=lambda *_:None):
    records=[];folders={};issues=[];session={}
    total=sum(len(state.get(key,[])) for key in ('copy_pool','jobs'))
    count=0
    for key in ('copy_pool','jobs'):
        for job in state.get(key,[]):
            if cancel.is_set():return dict(cancelled=True)
            try:
                data=for_state(job,state,cancel=cancel,session=session)
                records.append(dict(key=key,id=job['id'],data=data))
                value=data['job'];folder=value.get('task_dir') or (str(Path(value['task_voice_path']).parent) if value.get('task_voice_path') else '')
                if folder:
                    folders[folder]=folders.get(folder,False) or bool(value.get('task_voice_path') and Path(value['task_voice_path']).is_file())
                issues.extend((job.get('name') or job['id'])+'：'+v for v in data['issues'])
            except Exception as error:
                if cancel.is_set():return dict(cancelled=True)
                issues.append((job.get('name') or job['id'])+'：'+str(error))
            count+=1
            if count%25==0 or count==total:progress(count,total)
    return dict(profile=state['active_profile'],records=records,total=count,
        voice_folders=sum(folders.values()),missing_folders=sum(not v for v in folders.values()),
        issues=list(dict.fromkeys(issues)))


def apply_results(state,result):
    if result.get('cancelled'):return state,False
    if state['active_profile']!=result['profile']:raise ValueError('当前项目已切换，请重新刷新。')
    updated=copy.deepcopy(state);changed=False
    by_key={key:{job['id']:job for job in updated.get(key,[])} for key in ('copy_pool','jobs')}
    for record in result['records']:
        job=by_key[record['key']].get(record['id'])
        if job is None:raise ValueError('任务列表已变化，请重新刷新。')
        before=copy.deepcopy(job);data=record['data']
        if not apply(job,data['proposal']):continue
        changed=True
        operational=(any(k in data['proposal']['fields'] for k in ('path','voice_path'))
            or updated['settings'].get('use_voice') and not explicit_voice(job)
                and 'task_subtitle_path' in data['proposal']['fields']
            or bool(data['proposal']['assignment']))
        dependency_changed=bool(before.get('association_signature') and before['association_signature']!=job.get('association_signature'))
        if job.get('status')=='已完成' and (operational or dependency_changed):
            job.update(status='待生成',render_checked=True,render_dirty=True,render_dirty_reason='关联资料已更新',error='')
            job.pop('render_timing',None)
        elif job.get('status') in {'需配置','需处理'} and operational:
            job.update(status='待生成',error='')
        if before.get('error') and before.get('error')==before.get('match_error') and not job.get('match_error'):
            job.update(status='待生成',error='')
    return updated,changed


class RefreshWorker(QtCore.QThread):
    progress=QtCore.pyqtSignal(str)
    def __init__(self,state,parent=None):
        super().__init__(parent);self.state=state;self.cancel=threading.Event();self.result={}
    def run(self):
        try:
            self.result=scan(self.state,self.cancel,lambda done,total:
                self.progress.emit(f'正在刷新当前项目的关联资料：{done}/{total}…'))
        except Exception as error:self.result=dict(error=str(error))


def start(owner):
    if owner.is_batch_busy():
        owner.status.setText('当前项目正在渲染或处理，结束后可刷新关联资料。');return
    owner.edit_timer.stop()
    if not owner.persist():return
    snapshot=dict(active_profile=owner.state['active_profile'],settings=owner.read_settings(),
        jobs=copy.deepcopy(owner.state['jobs']),copy_pool=copy.deepcopy(owner.state['copy_pool']),
        render_configs=copy.deepcopy(owner.state.get('render_configs',{})))
    worker=RefreshWorker(snapshot,owner)
    # Reuse the import lifecycle: cancel/close/profile switching already guard it.
    owner.import_worker=worker
    owner._set_work_controls(True)
    owner.status.setText('正在重新检查当前项目全部文案与任务的关联资料…')
    worker.progress.connect(owner.status.setText,QtCore.Qt.ConnectionType.QueuedConnection)
    connect_thread_finished(worker,owner,'import_worker',lambda:finish(owner,worker))
    worker.start()


def finish(owner,worker):
    owner.import_worker=None
    result=worker.result
    try:
        if worker.cancel.is_set() or result.get('cancelled') or not owner.isVisible():
            owner.status.setText('已取消刷新，原关联保留。');return
        if result.get('error'):raise ValueError(result['error'])
        updated,changed=apply_results(owner.state,result)
        if changed:owner.state=owner.store.save(updated,owner.state['revision'])
        from .layers import ImageCache
        owner._image_cache=ImageCache()
        from .thumbnails import service
        paths={p for record in result['records'] for p in record['data']['dependency_files']}
        service().invalidate(paths)
        owner.refresh_tables();owner.refresh_preview()
        message=f"刷新完成：检查{result['total']}条记录，找到{result['voice_folders']}条任务人声"
        if not owner.controls['use_voice'].isChecked():message+='（人声控件未启用）'
        if result['missing_folders']:message+=f"；{result['missing_folders']}个目录未找到人声"
        if result['issues']:message+=f"；{len(result['issues'])}项需检查"
        owner.status.setText(message);owner.status.setToolTip('\n'.join(result['issues']))
        if result['issues']:
            QtWidgets.QMessageBox.warning(owner,'部分关联资料需检查',message+'\n\n'+'\n'.join(result['issues'][:20]))
    except Exception as error:
        owner.status.setText('刷新失败：'+str(error))
        QtWidgets.QMessageBox.warning(owner,'关联资料未刷新',str(error))
    finally:
        # Restoring a hidden dialog must not start another preview during shutdown.
        if not owner.isVisible():owner.jobs.set_read_only(False)
        owner._set_work_controls(owner.is_batch_busy())
