"""Repo Archaeologist — reconstruct why code is the way it is."""

from .models import Findings, Investigation, TimelineEvent, ToolCall

__version__ = "0.1.0"
__all__ = ["Findings", "Investigation", "TimelineEvent", "ToolCall", "run_investigation"]


def run_investigation(**kwargs):
    """Lazy re-export so importing the package doesn't require the SDK."""
    from .agent import run_investigation as _run

    return _run(**kwargs)
