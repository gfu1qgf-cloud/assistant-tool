"""Conservative source-colour evidence for AI matte edges and finger gaps."""


def screen_removal(source_rgb, alpha, parameters):
    """Return a soft removal weight, or None when screen cleanup is inactive.

    Test the original source, since a foreground estimator may already have
    neutralised screen RGB. Restrict to the uncertain matte band: opaque green
    clothing in the core remains intact. Explicit keep/remove strokes win later.
    This is an AI refinement, separate from RGB-only despill's alpha contract.
    """
    if (not parameters['screen_cleanup'] or parameters['despill'] == 'none'
            or not parameters['despill_amount']): return None
    import cv2
    import numpy as np
    channel = 1 if parameters['despill'] == 'green' else 2
    others = [i for i in range(3) if i != channel]
    screen = source_rgb[:, :, channel].astype(np.float32)
    other = np.maximum(source_rgb[:, :, others[0]], source_rgb[:, :, others[1]])
    # Only strongly saturated screen colour is evidence for a hole; ordinary
    # skin spill is handled by RGB correction without reducing its opacity.
    confidence = np.clip(((screen-other)/np.maximum(screen, 1)-.5)/.35, 0, 1)
    confidence *= confidence*(3-2*confidence)
    confidence *= np.clip((screen-32)/32, 0, 1)
    matte = np.rint(np.clip(alpha, 0, 1)*255).astype(np.uint8)
    core = cv2.erode(matte, np.ones((3, 3), np.uint8), iterations=parameters['despill_width'],
                     borderType=cv2.BORDER_REPLICATE)
    confidence *= np.clip((1-core.astype(np.float32)/255)*2, 0, 1)
    confidence *= parameters['despill_amount']/100
    return confidence
