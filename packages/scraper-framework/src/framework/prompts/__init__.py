"""Per-county LLM extraction prompts.

Each county's system prompt lives in its own module for easier navigation
and smaller diffs when prompts are updated.  All prompt constants are
re-exported here for convenient access::

    from framework.prompts import RIVERSIDE_SYSTEM_PROMPT
"""

from __future__ import annotations

from .contra_costa import CONTRA_COSTA_SYSTEM_PROMPT
from .fresno import FRESNO_SYSTEM_PROMPT
from .riverside import RIVERSIDE_SYSTEM_PROMPT
from .san_bernardino import SAN_BERNARDINO_SYSTEM_PROMPT
from .san_diego import SAN_DIEGO_SYSTEM_PROMPT
from .san_francisco import SAN_FRANCISCO_SYSTEM_PROMPT
from .santa_clara import SANTA_CLARA_SYSTEM_PROMPT
from .ventura import VENTURA_SYSTEM_PROMPT

__all__ = [
    "CONTRA_COSTA_SYSTEM_PROMPT",
    "FRESNO_SYSTEM_PROMPT",
    "RIVERSIDE_SYSTEM_PROMPT",
    "SAN_BERNARDINO_SYSTEM_PROMPT",
    "SAN_DIEGO_SYSTEM_PROMPT",
    "SAN_FRANCISCO_SYSTEM_PROMPT",
    "SANTA_CLARA_SYSTEM_PROMPT",
    "VENTURA_SYSTEM_PROMPT",
]
