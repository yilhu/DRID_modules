"""
decision_logic.py

Module responsible for analyzing historical detection data (from detect_queue) 
for temporal and intensity consistency before setting the deterrence flag 
in the DataBus.

The module implements the following criteria:
1. Temporal Consistency: Detected frames must exceed MIN_FRAME_RATIO of all frames 
   within TIME_WINDOW_S.
2. Intensity: Total accumulated confidence score must be >= MIN_TOTAL_SCORE.
3. Cooldown: Must respect the COOLDOWN_S period between consecutive 'deter_flag=True' triggers.
4. Auto-Reset: The deter_flag is automatically reset to False after RESET_DELAY_S.
"""

import time
from typing import Any, Dict, List
import numpy as np

# Assume these imports are available in the project structure
from module_base import BaseModule
from data_bus import DataBus, DetectionItem, ErrorEntry

# --- DEFAULT CONFIGURATION (Fallbacks only) ---
DEFAULT_TIME_WINDOW_S = 2.0
DEFAULT_MIN_FRAME_RATIO = 0.6 
DEFAULT_MIN_TOTAL_SCORE = 8.0
DEFAULT_COOLDOWN_S = 15.0
DEFAULT_RESET_DELAY_S = 2.0 
DEFAULT_QUEUE_TIMEOUT = 0.05 # Increased default slightly for better CPU yielding
DEFAULT_MAX_FAIL = 5


class DecisionLogicModule(BaseModule):
    """
    Module that implements the complex logic for triggering deterrence 
    based on historical, time-weighted detection data, and manages its duration.
    """

    CONFIG_PREFIX = "decision_logic"

    def __init__(
        self,
        name: str,
        data_bus: DataBus,
        daemon: bool = True,
        **kwargs: Any,
    ):
        
        self.config_prefix = self.CONFIG_PREFIX
        cfg: Dict[str, Any] = data_bus.get_state("config", {})
        
        def get_cfg(key: str, default: Any) -> Any:
            """Retrieves a configuration value using the module's prefix."""
            full_key = f"{self.config_prefix}_{key}"
            return cfg.get(full_key, default)

        # 1. BaseModule Configuration (passed to super())
        max_fail = get_cfg("max_consecutive_fail", DEFAULT_MAX_FAIL)
        
        super().__init__(
            name=name, 
            data_bus=data_bus, 
            daemon=daemon,
            max_consecutive_fail=max_fail,
            **kwargs
        )
        
        # 2. DecisionLogic Specific Configuration
        self.time_window_s: float = float(get_cfg("time_window_s", DEFAULT_TIME_WINDOW_S))
        self.min_frame_ratio: float = float(get_cfg("min_frame_ratio", DEFAULT_MIN_FRAME_RATIO))
        self.min_total_score: float = float(get_cfg("min_total_score", DEFAULT_MIN_TOTAL_SCORE))
        self.cooldown_s: float = float(get_cfg("cooldown_s", DEFAULT_COOLDOWN_S))
        self.queue_timeout_s: float = float(get_cfg("queue_timeout_s", DEFAULT_QUEUE_TIMEOUT))
        self.reset_delay_s: float = float(get_cfg("reset_delay_s", DEFAULT_RESET_DELAY_S))
        
        # 3. Internal State
        # For Cooldown Check: Timestamp of the last successful deterrence trigger
        self._last_deter_ts: float = 0.0
        # For Auto-Reset: Timestamp when deter_flag was last set to True
        self._deter_active_ts: float = 0.0 
        # Buffer for historical analysis
        self._history_buffer: List[DetectionItem] = []

    def setup(self) -> None:
        """Initialize module internal state before running the main loop."""
        print(f"[{self.name}] Setup: Decision Logic initialized. Window={self.time_window_s}s, Min Ratio={self.min_frame_ratio}, Reset Delay={self.reset_delay_s}s.")
        
        # Initialize timestamps if the flag is already active (e.g., after system reboot)
        if self.data_bus.get_state("deter_flag", False):
            self._last_deter_ts = time.time()
            self._deter_active_ts = time.time()

    def teardown(self) -> None:
        """Clean up resources."""
        pass

    def _update_history_buffer(self, new_items: List[DetectionItem]) -> None:
        """
        Adds new items to the buffer and prunes items older than the time window.
        """
        if new_items:
            self._history_buffer.extend(new_items)
        
        cutoff_ts = time.time() - self.time_window_s
        
        self._history_buffer = [
            item for item in self._history_buffer 
            if item.timestamp >= cutoff_ts
        ]

    def _check_presence_criteria(self) -> bool:
        """
        Checks both Temporal Consistency (frame ratio) and Detection Intensity (total score).
        """
        if not self._history_buffer:
            return False

        total_frames_in_window = len(self._history_buffer)
        detected_frames_count = 0
        total_confidence_score = 0.0
        
        # Safety check: if the window is too small for reliable statistics
        if total_frames_in_window < 3: # Reduced slightly for faster debug response
             return False

        for item in self._history_buffer:
            # Check if this frame has any detection
            if item.boxes and len(item.boxes) > 0:
                detected_frames_count += 1
                
                # Accumulate the scores for Intensity Check
                if item.scores is not None and len(item.scores) > 0:
                    try:
                        scores_array = np.array(item.scores, dtype=float)
                        total_confidence_score += np.sum(scores_array)
                    except Exception:
                        pass
        
        # 1. Temporal Check: Did detected frames meet the minimum ratio?
        current_ratio = detected_frames_count / total_frames_in_window
        temporal_pass = (current_ratio >= self.min_frame_ratio)
        
        # 2. Intensity Check: Was the total confidence score high enough?
        intensity_pass = (total_confidence_score >= self.min_total_score)

        return temporal_pass and intensity_pass

    def _check_cooldown(self) -> bool:
        """
        Checks if the deterrence cooldown period has passed since the last trigger.
        """
        now = time.time()
        elapsed = now - self._last_deter_ts
        return elapsed >= self.cooldown_s

    def step(self):
        """
        Main logic loop: 
        1. Auto-Reset Check.
        2. Consume data.
        3. [CRITICAL] Sleep if no data to avoid CPU spinning.
        4. Update history and Logic check.
        """
        now = time.time()
        
        # -----------------------------------------------------------
        # 1. Auto-Reset Check (Time-based, must run regardless of new data)
        # -----------------------------------------------------------
        is_flag_active = self.data_bus.get_state("deter_flag", False)
        
        if is_flag_active and (now - self._deter_active_ts >= self.reset_delay_s):
            # Execute reset operation
            self.data_bus.set_state("deter_flag", False)
            
            # Log the reset
            log_message = f"RESET: deter_flag automatically cleared after {self.reset_delay_s}s."
            self.data_bus.queue_put(
                self.data_bus.error_log,
                ErrorEntry(now, self.name, "INFO", log_message),
                drop_oldest_if_full=True
            )
            is_flag_active = False 

        # -----------------------------------------------------------
        # 2. Consume the detect queue
        # -----------------------------------------------------------
        new_items: List[DetectionItem] = self.data_bus.drain_queue(
            q=self.data_bus.detect_queue,
            max_items=20 
        )

        # -----------------------------------------------------------
        # 3. [CRITICAL FIX] CPU Yielding / Anti-Spinning
        # -----------------------------------------------------------
        # If there is no new data, the logic has nothing to update.
        # We MUST sleep to release the GIL and let Yolo/Camera threads run.
        if not new_items:
            time.sleep(self.queue_timeout_s) 
            return

        # -----------------------------------------------------------
        # 4. Update Buffer & Check Logic (Only runs if new data arrived)
        # -----------------------------------------------------------
        self._update_history_buffer(new_items)
        
        # Check for genuine target presence
        is_target_present = self._check_presence_criteria()

        if is_target_present:
            # Check Cooldown
            if self._check_cooldown():
                # All triggering criteria met: Trigger deterrence
                
                # Get current stats for logging
                total_frames = len(self._history_buffer)
                detected_frames = sum(1 for item in self._history_buffer if item.boxes and len(item.boxes) > 0)
                
                log_message = (
                    f"TRIGGERED: Temporal ({detected_frames}/{total_frames} frames, Ratio {self.min_frame_ratio}) "
                    f"AND Intensity met. Cooldown passed."
                )
                self.data_bus.queue_put(
                    self.data_bus.error_log,
                    ErrorEntry(now, self.name, "INFO", log_message),
                    drop_oldest_if_full=True
                )
                
                # Set deter_flag to True
                self.data_bus.set_state("deter_flag", True)
                
                # Record both timestamps
                self._last_deter_ts = now
                self._deter_active_ts = now