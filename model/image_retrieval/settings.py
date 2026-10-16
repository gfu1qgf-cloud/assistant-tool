"""Only shared model parameters; result presentation and source pools stay in plugins."""
from .encoder import MODEL_IDS


def normalize_model_settings(value=None):
    value = value if isinstance(value, dict) else {}
    model = str(value.get("model") or "base")
    return {"model": model if model in MODEL_IDS else "base"}
