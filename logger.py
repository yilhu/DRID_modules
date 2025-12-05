"""
logger.py

Module responsible for recording critical system states and archiving the 
processed image upon deterrence flag activation (deter_flag=True).

- Listens for the 'deter_flag' state change.
- [MODIFIED] Reads latest image directly from the DataBus registry 
  ("latest_processed_image_item"), eliminating the need for queue competition 
  and unreliable time delays.
- Logs key state (motor_location) immediately.
- Archives the latest processed image to a file.
"""

import time
import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

# Assume these imports are available in the project structure
from module_base import BaseModule
from data_bus import DataBus, ProcessedImageItem, ErrorEntry
# Reusing the json_default helper from http_server for complex object serialization
# NOTE: json_default is assumed to be imported from http_server, or defined/imported elsewhere.
from http_server import json_default 

# --- DEFAULT CONFIGURATION (Fallbacks only) ---
DEFAULT_WORKING_LOG_PATH = "logs/working_log.txt"
DEFAULT_IMAGE_ARCHIVE_DIR = "archive/detections"
# [REMOVED] DEFAULT_IMAGE_PULL_DELAY_S is removed as it's no longer needed.
# How often to check the deter_flag when it's False (Polling interval)
DEFAULT_POLL_INTERVAL_S = 0.5 
DEFAULT_MAX_FAIL = 5


class LoggerModule(BaseModule):
    """
    A module that listens for the 'deter_flag' state change and archives 
    critical data for later analysis (post-event forensics).
    """

    CONFIG_PREFIX = "logger"

    def __init__(
        self,
        data_bus: DataBus,
        name: str = "Logger",
        daemon: bool = True,
        **kwargs: Any,
    ):
        
        self.config_prefix = self.CONFIG_PREFIX
        
        cfg: Dict[str, Any] = data_bus.get_state("config", {})
        
        def get_cfg(key: str, default: Any) -> Any:
            """Retrieves a configuration value using the module's prefix."""
            full_key = f"{self.config_prefix}_{key}"
            return cfg.get(full_key, default)

        # 1. BaseModule Configuration
        max_fail = get_cfg("max_consecutive_fail", DEFAULT_MAX_FAIL)
        
        super().__init__(
            name=name, 
            data_bus=data_bus, 
            daemon=daemon,
            max_consecutive_fail=max_fail,
            **kwargs
        )
        
        # 2. Logger Specific Configuration
        self.log_path: str = get_cfg("working_log_path", DEFAULT_WORKING_LOG_PATH)
        self.archive_dir: str = get_cfg("image_archive_dir", DEFAULT_IMAGE_ARCHIVE_DIR)
        # [REMOVED] self.image_pull_delay_s configuration is removed.
        self.poll_interval_s: float = float(get_cfg("poll_interval_s", DEFAULT_POLL_INTERVAL_S))

        # 3. Internal State
        # Flag to ensure a single TRUE->FALSE deter_flag cycle is logged only once
        self._is_logging_active: bool = False 

    def setup(self) -> None:
        """Create necessary directories."""
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)
        print(f"[{self.name}] Setup: Logging to {self.log_path}, archiving to {self.archive_dir}.")
        
    def teardown(self) -> None:
        """Cleanup hook."""
        pass

    def _log_event(self) -> None:
        """
        Executes the logging and image archiving process upon deterrence trigger.
        [MODIFIED] Reads image item directly from the DataBus registry.
        """
        timestamp = time.time()
        # Create a unique ID based on the timestamp for file and log entry
        event_id = datetime.fromtimestamp(timestamp).strftime("%Y%m%d_%H%M%S")
        log_entry: Dict[str, Any] = {
            "event_id": event_id,
            "timestamp_utc": datetime.utcfromtimestamp(timestamp).isoformat(),
            "trigger_source": self.name,
        }

        # --- A. Immediate State Logging (motor_location) ---
        log_entry["motor_location"] = self.data_bus.get_state("motor_location", {})
        
        # --- B. Direct Image Pull and Archiving from Registry ---
        
        # [MODIFIED] Pull the latest image item from the DataBus registry.
        # This is thread-safe and guaranteed to return the last item written by YoloDetector.
        latest_item: Optional[ProcessedImageItem] = self.data_bus.get_state(
            "latest_processed_image_item",
            None
        )

        if latest_item:
            # 1. Log Image Metadata (Detections, Timestamp, etc.)
            image_meta = {
                "item_timestamp": latest_item.timestamp,
                "width": latest_item.frame.shape[1] if hasattr(latest_item.frame, 'shape') else 'N/A',
                "height": latest_item.frame.shape[0] if hasattr(latest_item.frame, 'shape') else 'N/A',
                "detection_count": len(latest_item.boxes),
                "meta": latest_item.meta,
            }
            log_entry["processed_image_info"] = image_meta
            
            # 2. Archive the annotated image
            archive_filename = f"{event_id}_det.jpg"
            archive_path = os.path.join(self.archive_dir, archive_filename)
            
            try:
                # Assuming cv2 is available for image saving
                import cv2 
                # latest_item.frame is assumed to be a numpy array/cv2 frame
                cv2.imwrite(archive_path, latest_item.frame)
                log_entry["image_archive_path"] = archive_path
            except Exception as e:
                log_entry["image_archive_error"] = f"Failed to save image: {e}"
                
        else:
            # If the item is None (e.g., system started but no frame processed yet)
            log_entry["processed_image_info"] = {"status": "No image item found in registry."}


        # --- C. Write entry to working_log.txt ---
        try:
            with open(self.log_path, 'a') as f:
                # Use the json_default helper to serialize complex types
                f.write(json.dumps(log_entry, default=json_default) + "\n")
            print(f"[{self.name}] LOGGED EVENT: {event_id}. Image archive complete.")
        except Exception as e:
            # Report failure to error_log queue
            self.data_bus.queue_put(
                self.data_bus.error_log,
                ErrorEntry(timestamp, self.name, "ERROR", f"Failed to write log file: {e}"),
                drop_oldest_if_full=True
            )

    def step(self) -> None:
        """
        Periodically checks the deter_flag state and ensures the logging process 
        is triggered only once per event cycle (TRUE->FALSE transition).
        """
        current_flag = self.data_bus.get_state("deter_flag", False)

        if current_flag and not self._is_logging_active:
            # State transition: FALSE -> TRUE (New event detected)
            self._is_logging_active = True
            self._log_event()
            
        elif not current_flag and self._is_logging_active:
            # State transition: TRUE -> FALSE (Event cycle ended)
            # Reset internal state to be ready for the next event
            self._is_logging_active = False

        # If the flag is TRUE and logging is active (event in progress), we do nothing 
        # and wait for DecisionLogicModule to reset the flag.
        
        time.sleep(self.poll_interval_s)