"""Legacy encoder import compatibility; no model implementation belongs here."""
from model.image_retrieval.encoder import (
    ChineseImageEncoder, MODEL_IDS, MODEL_REVISION, MODEL_SPECS,
    ModelLoadError, combine_search_vectors,
)

__all__ = ["ChineseImageEncoder", "MODEL_IDS", "MODEL_REVISION", "MODEL_SPECS",
           "ModelLoadError", "combine_search_vectors"]
