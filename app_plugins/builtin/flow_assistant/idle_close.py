"""Automatic browser closure is disabled at the user's request."""


def request_close(service, client, params, message, *, grace=True):
    # Keep this internal entry point inert for older callers. Browsers remain
    # open, and task admission never waits for a native close acknowledgement.
    return False
