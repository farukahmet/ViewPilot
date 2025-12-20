"""
StateController - Centralized state management for ViewPilot.

Eliminates race conditions by replacing scattered time.time() locks and boolean flags
with a formal transaction-based pattern using monotonic timing and priority arbitration.
"""

import time
from enum import Enum, IntEnum
from threading import RLock
from typing import Optional


class UpdateSource(Enum):
    """Identifies the origin of a state update for causal tracking."""
    USER_DRAG = "user_drag"           # Slider/property drag in UI
    HISTORY_NAV = "history_nav"       # Back/forward history navigation
    VIEW_RESTORE = "view_restore"     # Saved view restoration
    CAMERA_SWITCH = "camera_switch"   # Camera dropdown selection change
    INTERNAL_SYNC = "internal_sync"   # UI <-> viewport synchronization
    VIEWPORT_POLL = "viewport_poll"   # Background monitor polling


class LockPriority(IntEnum):
    """Priority levels for lock arbitration. Higher priority preempts lower."""
    LOW = 0       # VIEWPORT_POLL - background monitoring
    NORMAL = 1    # USER_DRAG, INTERNAL_SYNC - standard UI operations
    HIGH = 2      # CAMERA_SWITCH - mode transitions
    CRITICAL = 3  # HISTORY_NAV, VIEW_RESTORE - must complete atomically


class StateController:
    """
    Singleton controller for coordinating state updates across ViewPilot.
    
    Key features:
    - RLock for thread safety (supports nested acquisition)
    - Priority-based arbitration (higher priority can preempt)
    - Monotonic grace periods (immune to wall-clock drift)
    - Transaction tracking for debugging
    """
    
    _instance: Optional['StateController'] = None
    _lock = RLock()  # Class-level lock for singleton creation
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._state_lock = RLock()
        
        # Current transaction state
        self._active_source: Optional[UpdateSource] = None
        self._active_priority: LockPriority = LockPriority.LOW
        self._transaction_depth: int = 0
        
        # Grace period timing (monotonic)
        self._grace_period_end: float = 0.0
        self._grace_period_source: Optional[UpdateSource] = None
        
        # Skip flags (replaces utils.skip_enum_load)
        self._skip_enum_load: bool = False
        
        self._initialized = True
    
    def begin_update(self, source: UpdateSource, priority: LockPriority) -> bool:
        """
        Attempt to start a state update transaction.
        
        Args:
            source: The origin of this update (for debugging/logging)
            priority: The priority level of this update
            
        Returns:
            True if transaction was acquired, False if blocked by higher priority
        """
        with self._state_lock:
            # Re-entrant: same source can nest
            if self._active_source == source:
                self._transaction_depth += 1
                return True
            
            # Check if blocked by higher or equal priority
            if self._active_source is not None and priority < self._active_priority:
                return False
            
            # Acquire transaction
            self._active_source = source
            self._active_priority = priority
            self._transaction_depth = 1
            return True
    
    def end_update(self):
        """Release the current transaction."""
        with self._state_lock:
            if self._transaction_depth > 0:
                self._transaction_depth -= 1
                
            if self._transaction_depth == 0:
                self._active_source = None
                self._active_priority = LockPriority.LOW
    
    def transaction(self, source: UpdateSource, priority: LockPriority):
        """
        Context manager for safe update lifecycle.
        
        Usage:
            with controller.transaction(UpdateSource.VIEW_RESTORE, LockPriority.CRITICAL) as acquired:
                if acquired:
                    # do your work here
                    pass
        
        Ensures end_update() is called even if an exception occurs.
        """
        from contextlib import contextmanager
        
        @contextmanager
        def _transaction():
            acquired = self.begin_update(source, priority)
            try:
                yield acquired
            finally:
                if acquired:
                    self.end_update()
        
        return _transaction()
    
    def start_grace_period(self, duration: float, source: Optional[UpdateSource] = None):
        """
        Start a grace period during which certain operations should be suppressed.
        
        Uses time.monotonic() to be immune to wall-clock adjustments.
        
        Args:
            duration: Duration in seconds
            source: Optional source that initiated the grace period
        """
        with self._state_lock:
            self._grace_period_end = time.monotonic() + duration
            self._grace_period_source = source or self._active_source
    
    def is_in_grace_period(self) -> bool:
        """Check if currently in a grace period."""
        with self._state_lock:
            return time.monotonic() < self._grace_period_end
    
    def should_record_history(self) -> bool:
        """
        Determine if the current state change should be recorded to history.
        
        Returns False during:
        - Grace periods (after restoration)
        - HISTORY_NAV or VIEW_RESTORE transactions (to prevent re-recording)
        """
        with self._state_lock:
            if self.is_in_grace_period():
                return False
            
            if self._active_source in (UpdateSource.HISTORY_NAV, UpdateSource.VIEW_RESTORE):
                return False
            
            return True
    
    def is_update_in_progress(self, priority_threshold: Optional[LockPriority] = None) -> bool:
        """
        Check if an update transaction is currently active.
        
        Args:
            priority_threshold: If provided, only returns True if active priority >= threshold
        """
        with self._state_lock:
            if self._active_source is None:
                return False
            
            if priority_threshold is not None:
                return self._active_priority >= priority_threshold
            
            return True
    
    @property
    def skip_enum_load(self) -> bool:
        """Flag to skip auto-loading when syncing enum programmatically."""
        return self._skip_enum_load
    
    @skip_enum_load.setter
    def skip_enum_load(self, value: bool):
        self._skip_enum_load = value
    
    @property
    def active_source(self) -> Optional[UpdateSource]:
        """The currently active update source, if any."""
        return self._active_source

    @property
    def grace_period_source(self) -> Optional[UpdateSource]:
        """The source that initiated the current grace period."""
        return self._grace_period_source
    
    def reset(self):
        """Reset all state (for testing or addon reload)."""
        with self._state_lock:
            self._active_source = None
            self._active_priority = LockPriority.LOW
            self._transaction_depth = 0
            self._grace_period_end = 0.0
            self._grace_period_source = None
            self._skip_enum_load = False


# Module-level accessor for convenience
_controller: Optional[StateController] = None


def get_controller() -> StateController:
    """Get the singleton StateController instance."""
    global _controller
    if _controller is None:
        _controller = StateController()
    return _controller


def reset_controller():
    """Reset the controller state (useful for addon reload)."""
    global _controller
    if _controller is not None:
        _controller.reset()
