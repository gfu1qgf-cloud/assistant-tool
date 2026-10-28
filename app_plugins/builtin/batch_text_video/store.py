"""Private plugin data. No credentials and no writes to the host configuration."""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

from app_paths import DATA_ROOT as APP_ROOT
from model.AppDataStorage import data_file, data_directory
from .layers import DEFAULT_LAYERS, normalize_layers
from .effects import blur_strength
from .voice_effects import DEFAULTS as VOICE_FX_DEFAULTS, normalized as normalize_voice_fx
from .image_background import DEFAULTS as IMAGE_BACKGROUND_DEFAULTS, normalized as normalize_image_background
from .canvas_settings import DEFAULTS as CANVAS_DEFAULTS, normalized as normalize_canvas

SCHEMES = {"static_text": "静态文字版"}
DEFAULTS = {
    "scheme": "static_text", **CANVAS_DEFAULTS, "fit": "contain",
    "auto_crop_black_bars": True,
    "geometry_units": "percent", "show_layer_bounds": True,
    "background_tint_color":"#000000","background_tint_strength":0,
    "background_blur":0,
    "vertical": "center", "title_align": "center", "body_align": "left",
    "font": "Segoe UI", "title_min": 36, "title_max": 64,
    "body_min": 26, "body_max": 42, "title_color": "#d3e55a",
    "body_color": "#ffffff", "margin_x": 8, "margin_top": 8,
    "margin_bottom": 18, "line_spacing": 1.15, "gap": 22,
    "darkness": 14, "music_volume": 70, "keep_audio": False, "music_pool_id": "",
    "skip_silence": True, "silence_db": -50, "silence_seconds": 0.6,
    "fade_seconds": 0.5, "overwrite": False, "keep_media_cache": True, "output_dir": "", "ffmpeg_path": "",
    "video_encoder": "cpu", "encoder_fallback": True, "export_workers": 1, "export_quality": "high",
    "use_voice": False, "voice_pool_id": "", "voice_pool_mode": "task", "music_enabled": True,
    "duration_strategy": "loop", "loop_transition": "fade",
    "unvoiced_duration_mode": "source", "unvoiced_duration_seconds": 15.0,
    "transition_seconds": 0.4, "min_speed": 0.5, "slow_interpolation": "repeat",
    "voice_volume": 100, "voice_music_volume": 25,
    "voice_eq": True, "eq_frequency": 1500, "eq_width": 2.0, "eq_gain": -9,
    "voice_duck": True, "duck_threshold_db": -30, "duck_ratio": 6,
    "duck_attack_ms": 20, "duck_release_ms": 350,
    **VOICE_FX_DEFAULTS,
    **IMAGE_BACKGROUND_DEFAULTS,
    "layers": DEFAULT_LAYERS,
}
_LOCK = threading.RLock()


def fresh_state():
    from .render_settings import KEYS
    settings = copy.deepcopy(DEFAULTS)
    settings['export_workers'] = 2
    return {"version": 1, "revision": 0, "settings": settings,
            'render_settings': {key: copy.deepcopy(settings[key]) for key in KEYS},
            "jobs": [], "music": [], "next_music_id": "", "backgrounds": [],
            "copy_pool": [], "image_pool": [], "resource_pools": [], "active_profile": "default",
            "profiles": [{"id": "default", "name": "默认配置", "data": None}],
            "render_plans": []}


def file_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def add_paths(items, paths, music=False):
    known = {file_identity(item["path"]) for item in items}
    added = 0
    for path in paths:
        path = Path(path).resolve()
        identity = file_identity(path)
        if not path.is_file() or identity in known:
            continue
        item = {"id": uuid.uuid4().hex, "path": str(path)}
        item.update({"cursor": 0.0, "last_used": "", "audio_key": ""} if music else {
            "title": "", "body": "", "name": path.stem + "_文字版",
            "status": "待生成", "output": "", "error": ""})
        items.append(item)
        known.add(identity)
        added += 1
    return added


class Store:
    def __init__(self, directory=None):
        self.directory = data_directory(directory or APP_ROOT / "BatchTextVideo").path
        self.path = self.directory / "state.json"

    def load(self):
        with _LOCK:
            managed = data_file(self.path)
            if not managed.exists():
                return fresh_state()
            state = managed.read_json()
            if state.get("version") != 1 or not isinstance(state.get("jobs"), list) or not isinstance(state.get("music"), list):
                raise ValueError("批量文案视频记录格式异常，未清空或覆盖；请检查 state.json。")
            old_settings = state.get('settings', {})
            if 'background_visual' not in old_settings and 'image_background_motion' in old_settings:
                old_settings.update(normalize_image_background(old_settings))
            state["settings"] = {**copy.deepcopy(DEFAULTS), **old_settings}
            state['settings'].update(normalize_canvas(state['settings']))
            state['settings'].update(normalize_voice_fx(state['settings']))
            state['settings'].update(normalize_image_background(state['settings']))
            from .adaptation import normalize_duration
            normalize_duration(state["settings"])
            from model.MediaEncoding import normalize_export_settings
            normalize_export_settings(state['settings'])
            state["settings"]["background_blur"] = blur_strength(state["settings"])
            state["settings"]["layers"] = normalize_layers(state["settings"].get("layers"))
            state.setdefault("backgrounds", [])
            from .pools import migrate_pools
            migrate_pools(state)
            if "profiles" not in state:
                state.update(active_profile="default", profiles=[{"id": "default", "name": "默认配置", "data": None}])
            state.setdefault("render_plans", [])
            if (not isinstance(state["profiles"], list) or not state["profiles"] or
                    len({item["id"] for item in state["profiles"]}) != len(state["profiles"]) or
                    not any(item["id"] == state.get("active_profile") for item in state["profiles"])):
                raise ValueError("配置列表格式异常，未清空或覆盖原记录。")
            from .render_settings import apply_global
            return apply_global(state, DEFAULTS)

    def current_revision(self):
        """Publish guard: read the saved token without rebuilding every layer."""
        with _LOCK:
            managed = data_file(self.path)
            if not managed.exists():
                return 0
            value = managed.read_json()
            if value.get('version') != 1 or not isinstance(value.get('revision'), int):
                raise ValueError('批量文案视频记录格式异常，未提交成品。')
            return value['revision']

    def save(self, state, expected_revision, *, touch_profile=True):
        with _LOCK:
            current = self.load()
            from .render_settings import normalized, apply_global
            baseline = state.get('render_settings', current['render_settings'])
            requested = normalized(state['settings'], DEFAULTS)
            changed_globals = requested != baseline
            if changed_globals and baseline != current['render_settings']:
                raise ValueError('全局渲染设置已被其他窗口修改，请重新载入后再调整。')
            state = copy.deepcopy(state)
            state['render_settings'] = requested if changed_globals else copy.deepcopy(current['render_settings'])
            if current["revision"] != expected_revision:
                # A background render may have committed another profile while
                # this editor was open. Merge only that other profile's data.
                same_active = current['active_profile'] == state.get('active_profile')
                old = next((p for p in state.get('profiles', []) if p['id'] == state.get('active_profile')), None)
                live = next((p for p in current['profiles'] if p['id'] == current['active_profile']), None)
                same_profiles = {p['id'] for p in state.get('profiles', [])} == {p['id'] for p in current['profiles']}
                if not (same_active and same_profiles and old and live and
                        old.get('revision', 0) == live.get('revision', 0)):
                    raise ValueError("当前配置已被其他窗口或后台任务修改，请重新载入后再操作。")
                state = copy.deepcopy(state)
                state['profiles'] = [next(p for p in state['profiles'] if p['id'] == live['id'])
                    if p['id'] == live['id'] else copy.deepcopy(p) for p in current['profiles']]
                state['render_plans'] = copy.deepcopy(current.get('render_plans', []))
            state = copy.deepcopy(state)
            apply_global(state, DEFAULTS)
            from .adaptation import normalize_duration
            state['settings'].update(normalize_canvas(state['settings']))
            state['settings'].update(normalize_voice_fx(state['settings']))
            state['settings'].update(normalize_image_background(state['settings']))
            normalize_duration(state["settings"])
            from model.MediaEncoding import normalize_export_settings
            normalize_export_settings(state['settings'])
            state["settings"]["background_blur"] = blur_strength(state["settings"])
            state["settings"]["layers"] = normalize_layers(state["settings"].get("layers"))
            from .pools import migrate_pools, snapshot
            migrate_pools(state)
            state.setdefault("active_profile", "default")
            state.setdefault("profiles", [{"id": "default", "name": "默认配置", "data": None}])
            profile = next(item for item in state["profiles"] if item["id"] == state["active_profile"])
            profile["data"] = snapshot(state)
            if touch_profile:
                profile['revision'] = profile.get('revision', 0) + 1
            state["revision"] = current["revision"] + 1
            data_file(self.path).write_json(state, indent=2)
            if changed_globals:
                from .render_slots import configure
                configure(file_identity(self.path), state['render_settings']['export_workers'])
            return state

    def profile_state(self, identifier):
        """One profile's saved resources with its own revision token."""
        with _LOCK:
            state = Store.load(self)
            profile = next((p for p in state['profiles'] if p['id'] == identifier), None)
            if profile is None:
                raise ValueError('渲染配置已不存在，请重新加入队列。')
            if identifier != state['active_profile']:
                if not isinstance(profile.get('data'), dict):
                    raise ValueError('渲染配置尚无保存内容。')
                state.update(copy.deepcopy(profile['data']))
            state['active_profile'] = identifier
            state['revision'] = profile.get('revision', 0)
            state['settings'].update(normalize_canvas(state['settings']))
            from .render_settings import apply_global
            apply_global(state, DEFAULTS)
            return state

    def save_profile(self, state, expected_revision, identifier):
        """Commit only this profile; never replace the editor's active project."""
        from .pools import snapshot
        with _LOCK:
            current = Store.load(self)
            profile = next((p for p in current['profiles'] if p['id'] == identifier), None)
            if profile is None or profile.get('revision', 0) != expected_revision:
                raise ValueError('此项目在排队期间被修改，已停止该项目导出；其他项目不受影响。')
            data = snapshot(state)
            data['settings'].update(copy.deepcopy(current['render_settings']))
            profile['data'] = copy.deepcopy(data)
            profile['revision'] = expected_revision + 1
            if current['active_profile'] == identifier:
                current.update(copy.deepcopy(data))
            current['revision'] += 1
            data_file(self.path).write_json(current, indent=2)
            result = copy.deepcopy(state)
            result['revision'] = profile['revision']
            return result

    def save_render_settings(self, values, *, expected=None):
        from .render_settings import normalized, apply_global
        values = normalized(values, DEFAULTS)
        with _LOCK:
            current = Store.load(self)
            if expected is not None and expected != current['render_settings'] and values != current['render_settings']:
                raise ValueError('全局渲染设置已被其他窗口修改，请重新载入后再调整。')
            if values != current['render_settings']:
                current['render_settings'] = values
                apply_global(current, DEFAULTS)
                current['revision'] += 1
                data_file(self.path).write_json(current, indent=2)
            from .render_slots import configure
            configure(file_identity(self.path), current['render_settings']['export_workers'])
            return copy.deepcopy(current['render_settings'])

    def add_render_plan(self, identifier, ids):
        with _LOCK:
            state = self.load()
            profile = next((p for p in state['profiles'] if p['id'] == identifier), None)
            if profile is None:
                raise ValueError('当前配置已不存在。')
            if any(p['profile_id'] == identifier and p['status'] in {'staged', 'submitted'}
                   for p in state['render_plans']):
                raise ValueError('这个配置已在全局队列中，请先处理或移除原计划。')
            if not ids or len(ids) != len(set(ids)):
                raise ValueError('请选择不重复的待导出视频。')
            jobs = state['jobs'] if state['active_profile'] == identifier else profile['data']['jobs']
            if not set(ids).issubset({job['id'] for job in jobs}):
                raise ValueError('队列中的任务已变化，请重新选择。')
            plan = {'id': uuid.uuid4().hex, 'profile_id': identifier, 'name': profile['name'],
                    'ids': list(ids), 'status': 'staged', 'task_id': '', 'error': ''}
            state['render_plans'].append(plan)
            state['revision'] += 1
            data_file(self.path).write_json(state, indent=2)
            return plan

    def update_render_plan(self, plan_id, status, task_id='', error='', *, timing=None):
        with _LOCK:
            state = self.load()
            plan = next((p for p in state['render_plans'] if p['id'] == plan_id), None)
            if plan is None:
                return
            plan.update(status=status, task_id=task_id or plan.get('task_id', ''), error=str(error))
            if isinstance(timing, dict):
                plan['render_timing'] = copy.deepcopy({key: value for key, value in timing.items() if key != 'items'})
            state['revision'] += 1
            data_file(self.path).write_json(state, indent=2)

    def sync_render_selection(self, identifier, ids, *, expected_profile_revision=None):
        """Upsert only an unsubmitted plan; never alter an executing snapshot."""
        with _LOCK:
            state=Store.load(self)
            profile=next((p for p in state['profiles'] if p['id']==identifier),None)
            if profile is None: raise ValueError('当前项目已不存在。')
            if expected_profile_revision is not None and profile.get('revision',0)!=expected_profile_revision:
                raise ValueError('项目任务已被其他窗口修改，未覆盖渲染勾选。')
            if any(p['profile_id']==identifier and p['status']=='submitted' for p in state['render_plans']):
                return False
            jobs=state['jobs'] if state['active_profile']==identifier else (profile.get('data') or {}).get('jobs',[])
            if len(ids)!=len(set(ids)) or not set(ids).issubset({job['id'] for job in jobs}):
                raise ValueError('渲染勾选包含已变化的任务。')
            plan=next((p for p in state['render_plans'] if p['profile_id']==identifier and p['status']=='staged'),None)
            known_ids=[job['id'] for job in jobs]
            if plan and plan['ids']==list(ids) and plan.get('known_ids')==known_ids: return False
            if not ids:
                if plan is None: return False
                state['render_plans'].remove(plan)  # Only the pending plan, never a job or file.
            elif plan:
                plan.update(ids=list(ids),known_ids=known_ids,selection_managed=True,error='')
            else:
                state['render_plans'].append({'id':uuid.uuid4().hex,'profile_id':identifier,'name':profile['name'],
                    'ids':list(ids),'known_ids':known_ids,'status':'staged','task_id':'','error':'','selection_managed':True})
            state['revision']+=1
            data_file(self.path).write_json(state,indent=2)
            return True

    def clear_render_selection(self, identifier):
        with _LOCK:
            state=Store.load(self)
            if any(p['profile_id']==identifier and p['status']=='submitted' for p in state['render_plans']):
                raise ValueError('执行中的项目不可取消勾选；请使用停止／取消任务。')
            profile=next((p for p in state['profiles'] if p['id']==identifier),None)
            if profile is None: return
            data=state if state['active_profile']==identifier else profile.get('data') or {}
            for job in data.get('jobs',[]): job['render_checked']=False
            from .pools import snapshot
            profile['data']=snapshot(data)
            profile['revision']=profile.get('revision',0)+1
            state['render_plans']=[p for p in state['render_plans'] if not (
                p['profile_id']==identifier and p['status']=='staged')]
            state['revision']+=1
            data_file(self.path).write_json(state,indent=2)

    def remove_render_plan(self, plan_id, *, active_task_ids=(), finished_task_ids=()):
        with _LOCK:
            state = self.load()
            plan = next((p for p in state['render_plans'] if p['id'] == plan_id), None)
            if plan is None:
                return
            if (plan.get('task_id') in active_task_ids or
                    plan['status'] in {'submitted', 'queued', 'running', 'canceling'}
                    and plan.get('task_id') not in finished_task_ids):
                raise ValueError('渲染计划已提交，请先在后台任务中取消。')
            # Removing a plan must also clear its render intent, otherwise the
            # next table refresh immediately recreates it. Keep selections
            # owned by another pending/executing plan of the same project.
            protected = {identifier for other in state['render_plans']
                if other['id'] != plan_id and other['profile_id'] == plan['profile_id']
                and other['status'] in {'staged', 'submitted'} for identifier in other['ids']}
            profile = next((p for p in state['profiles'] if p['id'] == plan['profile_id']), None)
            if profile is not None:
                data = state if state['active_profile'] == plan['profile_id'] else profile.get('data') or {}
                changed = False
                for job in data.get('jobs', []):
                    if job['id'] in plan['ids'] and job['id'] not in protected and job.get('render_checked') is not False:
                        job['render_checked'] = False
                        changed = True
                if changed:
                    from .pools import snapshot
                    profile['data'] = snapshot(data)
                    profile['revision'] = profile.get('revision', 0) + 1
            state['render_plans'] = [p for p in state['render_plans'] if p['id'] != plan_id]
            state['revision'] += 1
            data_file(self.path).write_json(state, indent=2)

    def read_profile(self, identifier=""):
        """Read one saved project/profile without switching or saving anything."""
        from .pools import snapshot, migrate_pools
        state = self.load()
        identifier = identifier or state["active_profile"]
        selected = next((item for item in state["profiles"] if item["id"] == identifier), None)
        if selected is None:
            raise ValueError("文案视频配置已不存在，请重新选择。")
        data = snapshot(state) if identifier == state["active_profile"] else copy.deepcopy(selected["data"])
        if not isinstance(data, dict):
            raise ValueError("所选配置尚无保存内容。")
        data.setdefault("resource_pools", [])
        data['settings'].update(copy.deepcopy(state['render_settings']))
        migrate_pools(data)
        return selected["name"], data

    def select_profile(self, state, identifier):
        from .pools import snapshot
        state = copy.deepcopy(state)
        current = next(item for item in state["profiles"] if item["id"] == state["active_profile"])
        current["data"] = snapshot(state)
        selected = next(item for item in state["profiles"] if item["id"] == identifier)
        if not selected.get("data"):
            raise ValueError("配置没有内容，原配置未更改。")
        selected_data = copy.deepcopy(selected["data"])
        # Older profiles must not inherit the custom pools of the current profile.
        selected_data.setdefault("resource_pools", [])
        state.update(selected_data, active_profile=identifier)
        state["settings"] = {**copy.deepcopy(DEFAULTS), **state["settings"]}
        from .render_settings import apply_global
        apply_global(state, DEFAULTS)
        return self.save(state, state["revision"], touch_profile=False)

    def create_profile(self, state, name, clone=False):
        from .pools import snapshot
        name = str(name).strip()
        if not name or len(name) > 80 or any(item["name"] == name for item in state["profiles"]):
            raise ValueError("配置名称不能为空、重复或超过80字。")
        state = copy.deepcopy(state)
        data = snapshot(state) if clone else snapshot(fresh_state())
        identifier = uuid.uuid4().hex
        state["profiles"].append({"id": identifier, "name": name, "data": data})
        return self.select_profile(state, identifier)

    def delete_profile(self, identifier, *, expected_profile_revision, finished_task_ids=()):
        """Delete project records only; never touch referenced media files."""
        from .pools import snapshot
        from .render_settings import apply_global
        with _LOCK:
            state = Store.load(self)
            selected = next((p for p in state['profiles'] if p['id'] == identifier), None)
            if selected is None:
                raise ValueError('项目已不存在，请刷新后再操作。')
            if selected.get('revision', 0) != expected_profile_revision:
                raise ValueError('项目已被其他窗口或后台任务修改，未删除；请重新载入。')
            if any(p['profile_id'] == identifier and p['status'] == 'submitted'
                   and p.get('task_id') not in finished_task_ids
                   for p in state['render_plans']):
                raise ValueError('项目已提交渲染，请先在后台任务中停止或取消，待结束后再删除。')
            state['profiles'] = [p for p in state['profiles'] if p['id'] != identifier]
            state['render_plans'] = [p for p in state['render_plans'] if p['profile_id'] != identifier]
            if not state['profiles']:
                # A fresh identity prevents a stale worker/editor from targeting
                # the replacement via a reused default project identifier.
                fresh = snapshot(fresh_state())
                replacement = {'id': uuid.uuid4().hex, 'name': '默认配置', 'revision': 0, 'data': fresh}
                state['profiles'].append(replacement)
            if state['active_profile'] == identifier:
                replacement = state['profiles'][0]
                if not isinstance(replacement.get('data'), dict):
                    raise ValueError('其他项目的保存内容异常，未删除当前项目。')
                state.update(copy.deepcopy(replacement['data']), active_profile=replacement['id'])
            apply_global(state, DEFAULTS)
            state['revision'] += 1
            data_file(self.path).write_json(state, indent=2)
            return state

    def completed(self, state, job_id, output, music_plan, sequence_plan=None):
        """Advance the rotation/playhead only after a verified output exists."""
        if not Path(output).is_file():
            raise FileNotFoundError("成品尚未保存，不能推进音乐进度。")
        state = self.completion_state(state, job_id, output, music_plan, sequence_plan)
        return self.save(state, state['revision'])

    def completion_state(self, state, job_id, output, music_plan, sequence_plan=None):
        """Pure reservation/commit calculation; never writes or advances persisted state."""
        state = copy.deepcopy(state)
        if sequence_plan:
            from .components import commit_sequences
            state["settings"]["layers"] = commit_sequences(state["settings"]["layers"],sequence_plan)
        job = next(item for item in state["jobs"] if item["id"] == job_id)
        job.update(status="已完成", output=str(output), error="", render_checked=False)
        if music_plan:
            from .custom_pools import get_pool
            pool_id = music_plan.get("pool_id", "")
            pool = get_pool(state, pool_id, "music") if pool_id else None
            items = pool["items"] if pool else state["music"]
            music = next(item for item in items if item["id"] == music_plan["id"])
            music.update(cursor=music_plan["cursor"], audio_key=music_plan["audio_key"],
                         last_used=datetime.now().astimezone().isoformat(timespec="seconds"))
            index = items.index(music)
            next_id = items[(index + 1) % len(items)]["id"]
            if pool:
                pool["next_id"] = next_id
            else:
                state["next_music_id"] = next_id
        return state


class ProfileStore(Store):
    """Render worker's profile-scoped view of the shared private state file."""
    def __init__(self, directory, identifier):
        super().__init__(directory)
        self.identifier = identifier

    def load(self):
        return Store.profile_state(self, self.identifier)

    def current_revision(self):
        with _LOCK:
            value = data_file(self.path).read_json()
            if value.get('version') != 1:
                raise ValueError('批量文案视频记录格式异常，未提交成品。')
            profile = next((p for p in value['profiles'] if p['id'] == self.identifier), None)
            if profile is None:
                raise ValueError('渲染配置已不存在，请重新加入队列。')
            return profile.get('revision', 0)

    def save(self, state, expected_revision):
        return Store.save_profile(self, state, expected_revision, self.identifier)
