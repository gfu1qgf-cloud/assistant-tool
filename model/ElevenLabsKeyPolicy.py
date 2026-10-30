"""User-owned paid reserve labels, separate from worker-owned key status."""
import copy
from model.ApiKeyHelper import api_key_id, normalize_api_keys, select_api_keys_for_attempt
from model.ElevenLabsPermissions import permission_value, TTS

PREFERENCES = 'elevenlabs_key_preferences'


def normalize_preferences(value, keys=None):
    if not isinstance(value, dict):
        return {}
    valid = {api_key_id(key) for key in normalize_api_keys(keys)} if keys is not None else None
    result = {}
    for identifier, item in value.items():
        if not isinstance(identifier, str) or len(identifier) != 20 or any(c not in '0123456789abcdef' for c in identifier):
            continue
        if valid is not None and identifier not in valid or not isinstance(item, dict):
            continue
        label = str(item.get('label') or '')[:80].strip()
        paid = item.get('paid') is True
        if label or paid:
            result[identifier] = {'label': label, 'paid': paid}
    return result


def is_paid(preferences, key):
    item = preferences.get(api_key_id(key), {}) if isinstance(preferences, dict) else {}
    return isinstance(item, dict) and item.get('paid') is True


def _known_denials(keys, statuses, capability):
    usable, _, _ = select_api_keys_for_attempt(keys, statuses)
    return bool(keys) and len(usable) == len(keys) and all(
        permission_value(statuses.get(api_key_id(key), {}), capability) is False for key in keys)


def paid_access_error(details):
    """Definite access rejection only. A generic 402/403 is not authorization."""
    if details.get('status_code') not in {400, 401, 402, 403}:
        return False
    codes = {str(details.get(name, '')).lower() for name in ('code', 'status', 'type')}
    if codes & {'insufficient_credits', 'quota_exceeded'}:
        return False
    if codes & {'subscription_required', 'paid_plan_required', 'feature_not_available',
                'voice_access_denied', 'model_access_denied', 'free_users_not_allowed'}:
        return True
    message = str(details.get('message', '')).lower()
    return details.get('status_code') in {401, 402, 403} and any(text in message for text in (
        'free users cannot use library voices', 'voice is not available for free users',
        'paid subscription is required', 'requires a paid subscription'))


class KeyCandidates(list):
    """List-compatible host-only context; never persisted in plugin jobs."""
    def __init__(self, keys, statuses, preferences):
        super().__init__(normalize_api_keys(keys))
        self.statuses = copy.deepcopy(statuses)
        self.preferences = normalize_preferences(preferences, self)


class AttemptPlan:
    def __init__(self, keys, statuses, preferences, capability):
        statuses = statuses if isinstance(statuses, dict) else {}
        self.keys = normalize_api_keys(keys)
        self.preferences = normalize_preferences(preferences, self.keys)
        normal = [key for key in self.keys if not is_paid(self.preferences, key)]
        reserve = [key for key in self.keys if is_paid(self.preferences, key)]
        self.normal, _, self.selection = select_api_keys_for_attempt(normal, statuses, capability=capability)
        self.reserve, _, _ = select_api_keys_for_attempt(reserve, statuses, capability=capability)
        # Previously confirmed endpoint denials can authorize the same feature.
        # Exhaustion, invalid keys, cooldown and an all-paid list cannot.
        self.permission_rejected = _known_denials(normal, statuses, capability)

    def attempts(self):
        yield from self.normal
        if self.permission_rejected:
            yield from self.reserve


def select_tts_candidates(keys, statuses, start_index=0, *, preferences=None):
    """Keep reserves available to the request boundary without authorizing spend."""
    statuses = statuses if isinstance(statuses, dict) else {}
    pool = normalize_api_keys(keys)
    preferences = normalize_preferences(preferences, pool)
    normal = [key for key in pool if not is_paid(preferences, key)]
    reserve = [key for key in pool if is_paid(preferences, key)]
    selected, next_index, selection = select_api_keys_for_attempt(normal, statuses, start_index, capability=TTS)
    if selected:
        return KeyCandidates(selected + reserve, statuses, preferences), next_index, selection
    if reserve and _known_denials(normal, statuses, TTS):
        return KeyCandidates(normal + reserve, statuses, preferences), next_index, selection
    if reserve and not normal:
        selection = {'mode': 'paid_reserved'}
    return KeyCandidates([], statuses, preferences), next_index, selection
