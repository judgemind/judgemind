"""Centralized LLM model identifiers.

Defines the default model constants used across Judgemind packages.
Each constant can be overridden via an environment variable for
testing or experimentation without code changes.

When Anthropic releases a new model version, update the corresponding
``_*_MODEL_ID`` constant here — all packages will pick up the change
automatically.
"""

from __future__ import annotations

import os

# The canonical Haiku model identifier.  Update this single value when
# Anthropic releases a new version.
_HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"

#: Default Claude Haiku model used across all Judgemind packages.
#: Override at runtime with the ``HAIKU_MODEL`` environment variable.
DEFAULT_HAIKU_MODEL: str = os.environ.get("HAIKU_MODEL", _HAIKU_MODEL_ID)

# The canonical Opus model identifier.
_OPUS_MODEL_ID = "claude-opus-4-20250514"

#: Default Claude Opus model used for full-agent / tool-use tasks.
#: Override at runtime with the ``OPUS_MODEL`` environment variable.
DEFAULT_OPUS_MODEL: str = os.environ.get("OPUS_MODEL", _OPUS_MODEL_ID)
