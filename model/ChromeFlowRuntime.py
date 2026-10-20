"""Keep Flow UI work running when its native Chrome window is covered."""
import re
from urllib.parse import urlsplit

FLOW_BACKGROUND_FLAGS = (
    '--disable-backgrounding-occluded-windows',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
)


def background_flags(targets):
    # These are browser-process switches: an already running Chrome process
    # needs a normal full restart before they take effect. Never restart it here.
    for target in targets:
        try:
            url = urlsplit(str(target))
            if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
                continue
            if url.hostname == 'flow.google.com' or (
                    url.hostname == 'labs.google' and re.match(r'^/fx/(?:[a-zA-Z-]+/)?tools/flow(?:/|$)', url.path)):
                return list(FLOW_BACKGROUND_FLAGS)
        except ValueError:
            continue
    return []
