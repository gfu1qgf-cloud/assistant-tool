"""Explicit attempt retirement and bounded recovery before paid submission."""
import time

from .policy import block_for
from .store import submission_risk
from .timing import value as timing_value, request_timeout
from .transfer_policy import retry_info


class TaskReset:
    def refresh_reset_holds(self):
        holds = self.store.reset_holds()
        for client in self.clients.values():
            account = str(client.get('account') or '').strip().casefold()
            client['reset_wait_until'] = max((hold['until'] for hold in holds if
                hold['browser'] == client.get('id') or hold['profile'] and hold['profile'] == client.get('profile') or
                account and account == hold['account']), default=0)

    def reset_tasks(self, command, *, automatic=False):
        ids = command.get('ids', [])
        rows = [row for row in self.store.rows() if row['id'] in ids]
        now = time.time()
        holds = {}
        for row in rows:
            requests = [p for p in self.pending.values() if p['job'] == row['id'] and p['attempt'] == row['attempt']]
            # A retired page operation may still complete after its socket is
            # gone. Give it its normal timeout; other accounts can proceed now.
            if row['status'] in {'preparing', 'uploading'} or requests:
                action = 'prepare' if row['status'] == 'preparing' else 'upload_images'
                holds[row['id']] = max([now + request_timeout(self.preferences, action)] +
                    [p['at'] + request_timeout(self.preferences, p['action']) for p in requests])
        result = self.store.reset_tasks(ids, versions=command.get('versions', {}),
            confirmed_submitted=command.get('confirmed_submitted') is True,
            busy_ids=self.archiving, holds=holds, automatic=automatic)
        reset = set(result['reset'])
        if reset:
            saved = self.store.settings()
            if 'image_affinities' in self.preferences or 'image_affinities' in saved:
                self.preferences = {**self.preferences, 'image_affinities':saved.get('image_affinities', {})}
        for request in self.pending.values():
            if request['job'] in reset:
                request['retired'] = True
        for row in rows:
            if row['id'] not in reset: continue
            profile = row['remote'].get('profile')
            self.launched.discard(profile)
            self.launching.pop(profile, None)
            if not automatic and not self.safety_paused:
                self.released.add(row['id'])
            if submission_risk(row, self.store.attempt_charged(row['attempt'])):
                for client in self.clients.values():
                    if client.get('id') == row['browser']:
                        client['batch_running'] = True
                        self.request(client, 'snapshot')
        self.refresh_reset_holds()
        if not automatic:
            self.tasks_reset.emit({'success': True, 'request_id': command.get('request_id'),
                'held': self.safety_paused, **result})
        return bool(reset)

    def watch_unsubmitted(self):
        """Do not bind an unpaid orphan to a browser ID forever."""
        changed = False
        now = time.time()
        for row in self.store.rows():
            if row['status'] not in {'preparing', 'uploading'}: continue
            if self.safety_paused or self.paused and row['id'] not in self.released: continue
            if submission_risk(row) or self.store.attempt_charged(row['attempt']): continue
            if any(p['job'] == row['id'] and p['attempt'] == row['attempt'] and not p.get('retired')
                   for p in self.pending.values()): continue
            client = next((c for c in self.clients.values() if c.get('authorized') and c.get('id') == row['browser']), None)
            if client and any(p['client'] == client['id'] and p['action'] == 'repair_project' for p in self.pending.values()):
                continue
            info = retry_info(row, 'upload')
            unavailable = (not client or client.get('page_ready') is False or client.get('batch_running') or
                block_for(client, self.preferences) or client.get('blocked') or
                not client.get('account') or client.get('credits') is None)
            if row['status'] == 'uploading' and (not info.get('next_at') or info.get('stopped') or not unavailable): continue
            since = info.get('waiting_since') or row['remote'].get('connection_wait_at')
            if not since:
                self.store.update(row['id'], row['status'], remote={**row['remote'], 'connection_wait_at': now}, error=row['error'])
                changed = True
                continue
            if now - since < timing_value(self.preferences, 'upload_reassign_wait_seconds'): continue
            count = row['remote'].get('upload_recovery', {}).get('count', 0)
            limit = timing_value(self.preferences, 'upload_reassign_attempts')
            if not limit: continue
            if count >= limit:
                self.store.update(row['id'], 'failed', error=f'提交前连接恢复已达 {limit} 次；未提交生成，可使用“完全重置并重新调度”。')
                changed = True
                continue
            changed |= self.reset_tasks({'ids': [row['id']], 'versions': {row['id']: {
                key: row[key] for key in ('attempt', 'browser', 'status')}}}, automatic=True)
        return changed
