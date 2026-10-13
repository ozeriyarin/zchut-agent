"""zchut: Hebrew-first RAG assistant for Israeli bureaucracy over public sources."""

import warnings

from pydantic.warnings import UnsupportedFieldAttributeWarning

__version__ = "0.1.0"

# llama-index-core triggers this pydantic warning at import time; it is not ours to fix.
warnings.filterwarnings("ignore", category=UnsupportedFieldAttributeWarning)
