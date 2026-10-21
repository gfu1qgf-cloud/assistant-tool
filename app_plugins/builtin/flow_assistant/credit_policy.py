"""One session balance observation, then conservative local reservations."""
import time
import math
from .timing import value as timing_value


def balance_ready(client, now=None, preferences=None):
    now = time.time() if now is None else now
    credits = client.get('credits')
    valid = (type(credits) is int or client.get('credits_local') is True and
             type(credits) is float and math.isfinite(credits))
    lifetime = timing_value(preferences, 'credit_validity_seconds')
    observed = client.get('credits_at', 0)
    if type(observed) not in (int, float) or not math.isfinite(observed) or observed <= 0: return False
    return (bool(client.get('account')) and valid and not client.get('identity_unconfirmed') and
            (lifetime == 0 or observed > now - lifetime))


def debit_balance(client, clients, cost):
    """Reserve before a possibly paid send, even if its response is lost.

    No speculative refunds on timeouts. Reconnection or manual refresh
    reconciles the balance. Windows of one account share the same balance.
    """
    if type(client.get('credits')) not in (int, float):
        return
    balance = max(0, client['credits'] - cost)
    for current in clients:
        if str(current.get('account') or '').casefold() == str(client.get('account') or '').casefold():
            current.update(credits=balance, credits_local=True, credits_estimated=True)


def credit_order(client):
    """Two tiers; unknown 50 is a sorting hint, never a spendable balance."""
    credits = client.get('credits')
    if type(credits) not in (int, float) or not math.isfinite(credits):
        return (0, 50)
    return (0 if credits < 100 else 1, credits)
