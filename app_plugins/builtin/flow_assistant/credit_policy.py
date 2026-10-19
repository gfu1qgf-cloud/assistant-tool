"""One session balance observation, then conservative local reservations."""
import time
import math


def balance_ready(client, now=None):
    now = time.time() if now is None else now
    credits = client.get('credits')
    valid = (type(credits) is int or client.get('credits_local') is True and
             type(credits) is float and math.isfinite(credits))
    return (bool(client.get('account')) and valid and
            (client.get('credits_local') is True or client.get('credits_at', 0) > now - 90))


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
