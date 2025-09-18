"""
Event context management using ContextVar for async-safe context propagation.
"""

from contextvars import ContextVar
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.events import EventInfo

# Context variable to store the current event across async boundaries
current_event: ContextVar[Optional['EventInfo']] = ContextVar('current_event', default=None)


def set_current_event(event: 'EventInfo') -> None:
    """Set the current event in the context."""
    current_event.set(event)


def get_current_event() -> Optional['EventInfo']:
    """Get the current event from the context."""
    return current_event.get()


def clear_current_event() -> None:
    """Clear the current event from the context."""
    current_event.set(None)