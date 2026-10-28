"""Keep the saved FLOW launch group attached to Chrome's group identity."""
from qt_compat import QtCore

GROUP_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
SAVED_LIST = '@saved-list'


def catalog(launcher):
    return [(group['id'], group['name'], launcher.get_group_profile_directories(group['id']))
            for group in getattr(launcher, 'profile_groups', [])]


def resolve(preferences, groups):
    """Migrate old member snapshots only when they identify one group safely."""
    group_id = preferences.get('profile_group_id')
    profiles = list(preferences.get('profiles') or [])
    if group_id:
        return next(((key, members) for key, _name, members in groups if key == group_id), ('', []))
    if not profiles:
        return '', []
    exact = [(key, members) for key, _name, members in groups if members == profiles]
    matches = exact or [(key, members) for key, _name, members in groups if set(profiles) <= set(members)]
    return matches[0] if len(matches) == 1 else (SAVED_LIST, profiles)


class SchedulingGroups:
    def __init__(self, dialog, launcher):
        self.dialog, self.launcher = dialog, launcher
        self.applied = None
        self.rebuild('', [])
        changed = getattr(launcher, 'catalog_changed', None)
        if changed is not None:
            changed.connect(self.changed)

    def selection(self):
        combo = self.dialog.group
        return combo.currentData(GROUP_ROLE) or '', list(combo.currentData() or [])

    def rebuild(self, selected, profiles):
        combo = self.dialog.group
        with QtCore.QSignalBlocker(combo):
            combo.clear()
            combo.addItem('只使用已连接浏览器', [])
            combo.setItemData(0, '', GROUP_ROLE)
            for key, name, members in catalog(self.launcher):
                combo.addItem(name, members)
                combo.setItemData(combo.count()-1, key, GROUP_ROLE)
            if selected == SAVED_LIST:
                combo.addItem('已保存的调度列表（请选择分组后保存）', profiles)
                combo.setItemData(combo.count()-1, SAVED_LIST, GROUP_ROLE)
            index = combo.findData(selected, GROUP_ROLE)
            combo.setCurrentIndex(max(0, index))

    def load(self, preferences):
        self.applied = resolve(preferences, catalog(self.launcher))
        self.rebuild(*self.applied)
        self.synchronize(preferences)

    def values(self):
        group_id, profiles = self.selection()
        self.applied = group_id, profiles
        return {'profiles': profiles, 'profile_group_id': None if group_id == SAVED_LIST else group_id}

    def synchronize(self, previous):
        group_id, profiles = self.applied
        # An ambiguous legacy list remains unchanged until the user chooses.
        if group_id == SAVED_LIST:
            return
        if profiles != (previous.get('profiles') or []) or group_id != (previous.get('profile_group_id') or ''):
            self.dialog.send('preferences', value={'profiles': profiles, 'profile_group_id': group_id})

    def changed(self):
        self.rebuild(*self.selection())
        if self.applied is not None:
            key, profiles = self.applied
            previous = {'profile_group_id': key, 'profiles': profiles}
            if key and key != SAVED_LIST:
                self.applied = next(((gid, members) for gid, _name, members in catalog(self.launcher)
                                     if gid == key), ('', []))
            self.synchronize(previous)
        self.dialog.update_browsers()
        self.dialog.setup.apply_state(self.dialog.state)
