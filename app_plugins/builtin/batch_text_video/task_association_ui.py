"""Revalidate the selected task on use, without background polling or watches."""
import copy
import json
from qt_compat import QtCore
from .task_associations import apply


class AssociationAccess(QtCore.QObject):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner
        self.dependency_files=[]

    def request_token(self,job,voice=''):
        from .task_associations import dependencies,stamp
        owner=self.owner
        settings=dict(layers=owner.layer_panel._layers,use_voice=owner.controls['use_voice'].isChecked(),
            voice_pool_mode=owner.controls['voice_pool_mode'].currentData())
        signature,files=dependencies(job,settings,scan_artifacts=False)
        from .pool_preview import pool_key
        trial=getattr(owner,'pool_preview',None)
        for layer in trial.layers if trial else []:
            items=layer.get('sequence_items',[])
            if not items:continue
            chosen=trial.choices.get(pool_key(layer))
            item=next((v for v in items if v['id']==chosen),items[layer.get('sequence_cursor',0)%len(items)])
            if item.get('path'):files.append(item['path'])
        if voice:files.append(voice)
        # Directory metadata catches newly created task media/timing records.
        # No folder enumeration, global scan, watcher, or timer is needed here.
        from pathlib import Path
        dirs={str(Path(p).parent) for p in files if p}
        if job.get('task_dir'):dirs.add(job['task_dir'])
        revisions=[stamp(p) for p in sorted(dirs|set(files)|set(self.dependency_files))]
        if job.get('source_sheet'):revisions.append(stamp(job['source_sheet']))
        return self.binding_token(job),signature,revisions

    def refresh_rows(self):
        if self.owner.isVisible() and not self.owner.is_batch_busy():self.owner.refresh_tables()

    def context(self,job,settings):
        owner=self.owner
        raw=next((v for v in owner.state['copy_pool' if owner.current_copy_id else 'jobs'] if v['id']==job.get('id')),job)
        key=job.get('render_config_id')
        configs={key:self.owner.state['render_configs'][key]} if key and key in self.owner.state.get('render_configs',{}) else {}
        return dict(job=copy.deepcopy(raw),settings=copy.deepcopy(settings),render_configs=copy.deepcopy(configs),profile=self.owner.state['active_profile'],token=self.binding_token(raw))

    def binding_token(self,job):
        from .components import walk_components
        from .task_associations import explicit_voice
        owner=self.owner
        # Read scalar bindings in-place, never copy/scan the entire resource pool.
        refs=[(layer['id'],*(layer.get(k,'') for k in ('path','source_file','caption_file','caption_cursor_image')))
              for layer in walk_components(owner.layer_panel._layers)]
        return json.dumps([owner.state['active_profile'],job.get('id',''),job.get('task_dir',''),explicit_voice(job),refs,
            owner.controls['use_voice'].isChecked(),owner.controls['voice_pool_mode'].currentData(),
            owner.controls['voice_pool_id'].currentData(),owner.controls['background_pool_id'].currentData()],ensure_ascii=False)

    def accept(self,result):
        data=result.get('associations');context=result.get('association_context')
        if not data or not context:return True
        owner=self.owner
        if not owner.isVisible() or context['profile']!=owner.state['active_profile'] or owner.is_batch_busy():return False
        current=owner.active_entry()
        if current.get('id')!=context['job'].get('id'):return False
        if context.get('token')!=self.binding_token(current):return False
        self.dependency_files=data['dependency_files']
        key,identifier=('copy_pool',owner.current_copy_id) if owner.current_copy_id else ('jobs',owner.current_id)
        source=next((v for v in owner.state[key] if v['id']==identifier),None)
        if source is None:return True
        before=copy.deepcopy(source)
        if not apply(source,data['proposal'],strict=False):return True
        from .task_associations import explicit_voice
        automatic_audio=owner.controls['use_voice'].isChecked() and not explicit_voice(source)
        operational=(any(v in data['proposal']['fields'] for v in ('path','voice_path'))
            or automatic_audio and 'task_subtitle_path' in data['proposal']['fields'] or bool(data['proposal']['assignment']))
        prior_signature=before.get('association_signature')
        updated_dependency=prior_signature and prior_signature!=source.get('association_signature')
        if updated_dependency:
            from .thumbnails import service
            service().invalidate(data['dependency_files'])
            from .layers import ImageCache
            owner._image_cache=ImageCache()
        if source.get('status')=='已完成' and (operational or updated_dependency):
            source.update(status='待生成',render_checked=True,render_dirty=True,render_dirty_reason='关联资料已更新',error='')
            source.pop('render_timing',None)
        elif source.get('status') in {'需配置','需处理'} and operational:
            source.update(status='待生成',error='')
        if before.get('error') and before.get('error')==before.get('match_error') and not source.get('match_error'):
            source.update(status='待生成',error='')
        owner.edit_timer.stop()
        owner.persist()
        QtCore.QTimer.singleShot(0,self.refresh_rows)
        return True
