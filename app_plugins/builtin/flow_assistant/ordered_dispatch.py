"""One FIFO head and a consumable snapshot of connected browser identities."""
from .connection_status import configured_profiles
from .policy import batch_wait, estimated_cost
from .browser_table import natural_key


def browser_order(client, preferences):
    from .high_credit import is_high
    profiles = configured_profiles(preferences)
    profile = client.get('profile', '')
    return (int(is_high(client, preferences)),
            profiles.index(profile) if profile in profiles else len(profiles), natural_key(profile))


def queue_head(service, rows, batches):
    # rows() is durable SQLite insertion order. Explicit releases only admit
    # selected rows; priority, duration and retry delays never let later rows pass.
    row = next((row for row in rows if row['status'] == 'queued' and
                (not service.paused or row['id'] in service.released)), None)
    if not row: return []
    cost = estimated_cost(row['payload'], service.observed_costs)
    if (batch_wait(row['payload'], batches, cost) or
            row['payload'].get('budget') and cost is not None and cost > row['payload']['budget']): return []
    return [row]


def snapshot_clients(service, clients):
    ordered = sorted(clients, key=lambda c: browser_order(c, service.preferences))
    current = {c['id']: c for c in ordered}
    snapshot = [identifier for identifier in getattr(service, 'browser_snapshot', []) if identifier in current]
    snapshot.extend(c['id'] for c in ordered if c['id'] not in snapshot)
    service.browser_snapshot = snapshot
    # A cycle only rotates connected, eligible identities. Account exclusivity
    # and image ownership are checked separately before selecting any entry.
    return [current[identifier] for identifier in snapshot]


def take(service, client):
    snapshot = getattr(service, 'browser_snapshot', [])
    if client['id'] in snapshot:
        snapshot.remove(client['id'])
        snapshot.append(client['id'])
