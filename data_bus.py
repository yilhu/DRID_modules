"""
data_bus.py

Thread-safe shared data hub for the detection unit on Raspberry Pi.

Core responsibilities:
    - Own and expose the main shared queues (excluding processed_image_queue, now a state).
    - Provide generic queue helpers.
    - Provide a single semantic helper (push_detection_with_image) that updates the registry.
    - Maintain ALL shared state (including latest processed image) in a unified, thread-safe registry.
    - Provide a thread-safe state change notification mechanism (using Condition).
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List, Tuple


# ---------------------------------------------------------------------------
# Typed payloads for clarity
# ---------------------------------------------------------------------------

@dataclass
class ImageItem:
    """Raw image produced by read_camera.py and consumed by yolo_detector.py."""
    frame: Any              # e.g. numpy.ndarray; kept generic
    timestamp: float        # seconds since epoch
    meta: Dict[str, Any]    # extra information (frame id, camera id, etc.)


@dataclass
class ProcessedImageItem:
    """
    Processed image produced by yolo_detector.py.

    This item includes both the annotated frame and the detection data.
    """
    frame: Any              # processed / annotated frame
    boxes: Any              # list/array of bounding boxes
    scores: Any             # list/array of confidence scores
    labels: Any             # list/array of class labels
    timestamp: float        # usually same as corresponding DetectionItem
    meta: Dict[str, Any]


@dataclass
class DetectionItem:
    """Detection result produced by yolo_detector.py."""
    boxes: Any              # list/array of bounding boxes
    scores: Any             # list/array of confidence scores
    labels: Any             # list/array of class labels
    timestamp: float        # time of the processed frame
    meta: Dict[str, Any]


@dataclass
class ErrorEntry:
    """Structured error log entry for logger.py / watchdog.py."""
    timestamp: float
    module: str
    level: str              # "INFO", "WARNING", "ERROR", "CRITICAL", etc.
    message: str
    details: Optional[str] = None


# ---------------------------------------------------------------------------
# Configuration Definition (Simplified structure for demonstration)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_STRUCTURE = {
    # --- ReadCamera Configuration ---
    "camera_device_index": 0,
    "camera_req_width": None,
    "camera_req_height": None,
    "camera_period_s": 0.0,
    "camera_fourcc": None,
    "camera_buffer_size": 1,
    "camera_max_consecutive_fail": 5, # From BaseModule

    # --- YoloDetector Configuration ---
    "yolo_model_weights_path": "/default/path/to/model",
    "yolo_input_img_size": 640,
    "yolo_conf_threshold": 0.25,
    "yolo_iou_threshold": 0.45,
    "yolo_bgr_to_rgb_conversion": False,
    "yolo_queue_timeout_s": 0.1,
    "yolo_max_consecutive_fail": 5, # From BaseModule

    # --- HttpServerModule Configuration ---
    "http_server_host": "127.0.0.1",
    "http_server_port": 5000,
    "http_server_poll_interval": 1.0,
    # Export config is complex; using a simplified list of keys for demonstration
    "http_server_export_config_keys": ["motor_location", "deter_flag"], 
    "http_server_max_consecutive_fail": 5, # From BaseModule
}


# ---------------------------------------------------------------------------
# Core DataBus class
# ---------------------------------------------------------------------------

class DataBus:
    """
    Central hub for all shared data, state, and resources.
    """

    def __init__(
        self,
        config_override: Optional[Dict[str, Any]] = None,
        image_queue_size: int = 3,
        detect_queue_size: int = 20,
        # [REMOVED] processed_image_queue_size argument is no longer needed.
        error_queue_size: int = 200,
    ) -> None:
        # Bounded queues for streaming data
        self.image_queue: queue.Queue = queue.Queue(maxsize=image_queue_size)
        self.detect_queue: queue.Queue = queue.Queue(maxsize=detect_queue_size)
        # [REMOVED] self.processed_image_queue definition is removed.
        self.error_log: queue.Queue = queue.Queue(maxsize=error_queue_size)

        # ------------------------------------------------------------------
        # Unified Registry for ALL Shared State and Module Resources
        # ------------------------------------------------------------------
        self._registry_lock = threading.Lock()
        self._registry: Dict[str, Any] = {}
        
        # Condition variable for state change notifications
        self._state_condition = threading.Condition(self._registry_lock) 

        # 1. Initialize 'config' as a core state attribute, allowing overrides
        initial_config = dict(DEFAULT_CONFIG_STRUCTURE)
        if config_override:
            initial_config.update(config_override)
        self.set_state("config", initial_config)
        
        # 2. Initialize core dynamic states 
        self.set_state("motor_location", {})
        self.set_state("deter_flag", False)
        # [NEW] Initialize the registry key for the latest processed image snapshot
        self.set_state("latest_processed_image_item", None)
        
        # Lock for pushing detection + processed image together
        # Note: This lock now only protects the detect_queue put operation.
        self._det_proc_lock = threading.Lock()

        # Per-module health (for watchdog/http_server)
        self._health_lock = threading.Lock()
        self._module_health: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Generic queue helpers
    # ------------------------------------------------------------------

    def queue_put(
        self,
        q: queue.Queue,
        item: Any,
        drop_oldest_if_full: bool = False,
    ) -> None:
        """
        Put an item into a queue. If bounded and drop_oldest_if_full is True,
        it non-blockingly removes the oldest item if the queue is full.
        """
        if drop_oldest_if_full and q.maxsize > 0 and q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                # Race condition; safe to ignore.
                pass
        q.put(item)

    def queue_get(
        self,
        q: queue.Queue,
        timeout: Optional[float] = None,
    ) -> Optional[Any]:
        """
        Get the next (oldest) item from a queue (FIFO semantics).
        Returns None if timeout expires.
        """
        try:
            if timeout is None:
                return q.get(block=True)
            elif timeout == 0:
                return q.get_nowait()
            else:
                return q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_queue(
        self,
        q: queue.Queue,
        max_items: Optional[int] = None,
    ) -> List[Any]:
        """
        Non-blocking drain of a queue. Returns a list of items.
        """
        items: List[Any] = []
        while max_items is None or len(items) < max_items:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            items.append(item)
        return items

    def get_latest_from_queue(self, q: queue.Queue) -> Optional[Any]:
        """
        Non-blocking helper that returns the last available item in a queue.
        Used for UI modules that only care about the most recent state.
        (Retained for image_queue/error_log usage, but not for processed images).
        """
        latest: Any = None
        got_any = False
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            latest = item
            got_any = True
        return latest if got_any else None

    # ------------------------------------------------------------------
    # Detection + processed-image paired push helper
    # ------------------------------------------------------------------

    def push_detection_with_image(
        self,
        boxes: Any,
        scores: Any,
        labels: Any,
        processed_frame: Any,
        meta: Optional[Dict[str, Any]] = None,
        drop_oldest_if_full: bool = False,
    ) -> None:
        """
        Push a detection result to detect_queue AND store the corresponding 
        processed frame item as the latest state snapshot in the registry.
        """
        if meta is None:
            meta = {}

        timestamp = time.time()

        det_item = DetectionItem(
            boxes=boxes,
            scores=scores,
            labels=labels,
            timestamp=timestamp,
            meta=dict(meta),
        )
        img_item = ProcessedImageItem(
            frame=processed_frame,
            boxes=boxes,
            scores=scores,
            labels=labels,
            timestamp=timestamp,
            meta=dict(meta),
        )

        with self._det_proc_lock:
            # 1. Push Detection Item to queue (for Decision Logic module)
            self.queue_put(
                self.detect_queue,
                det_item,
                drop_oldest_if_full=drop_oldest_if_full,
            )
            # [REMOVED] The queue_put for self.processed_image_queue is removed.

        # 2. Store Processed Image Item in Registry (for Logger and HTTP Server)
        # This guarantees access to the latest item without queue contention.
        self.set_state("latest_processed_image_item", img_item)

    # ------------------------------------------------------------------
    # Unified State API
    # ------------------------------------------------------------------

    def set_state(self, key: str, value: Any) -> None:
        """
        Set a generic shared state key's value. Thread-safe.
        Notifies listeners if the 'deter_flag' is set to True.
        """
        with self._registry_lock:
            old_value = self._registry.get(key)
            self._registry[key] = value
            
            # Notify listeners if 'deter_flag' changes to True.
            if key == "deter_flag" and value and old_value != value:
                self._state_condition.notify_all()

    def get_state(self, key: str, default: Any = None) -> Any:
        """
        Get a generic shared state key's value. Thread-safe.
        Returns a copy of mutable objects (dict/list) if the key exists.
        """
        with self._registry_lock:
            value = self._registry.get(key, default)
            
            if key in self._registry:
                # Return a copy of mutable objects (e.g., motor_location)
                if isinstance(value, dict):
                    return dict(value)
                if isinstance(value, list):
                    return list(value)
                
            return value

    def update_state(self, key: str, **kwargs: Any) -> None:
        """
        Atomically update a dictionary-like state item (e.g., 'motor_location').
        """
        with self._registry_lock:
            current_state = self._registry.setdefault(key, {})
            if isinstance(current_state, dict):
                current_state.update(kwargs)
            else:
                raise TypeError(f"State key '{key}' is not a dictionary and cannot be updated with kwargs.")

    # [NEW] State change waiting mechanism
    def wait_for_state_change(self, timeout: float) -> None:
        """
        Wait until a state change notification (currently only deter_flag=True) is 
        received or the timeout expires.
        """
        with self._state_condition:
            # wait() releases the lock and blocks until notified or timeout
            self._state_condition.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Generic registry for module-specific resources
    # ------------------------------------------------------------------

    def get_or_create(self, key: str, factory: Callable[[], Any]) -> Any:
        """
        Get a resource by key from the internal registry, creating it on the
        first access with the given factory. Thread-safe.
        """
        with self._registry_lock:
            if key not in self._registry:
                self._registry[key] = factory()
            return self._registry[key]

    def has_key(self, key: str) -> bool:
        """
        Check if a key exists in the registry (state or resource). Thread-safe.
        """
        with self._registry_lock:
            return key in self._registry

    def registry_keys(self) -> List[str]:
        """
        Return a list of all registry keys (for debugging / watchdog). Thread-safe.
        """
        with self._registry_lock:
            return list(self._registry.keys())

    # ------------------------------------------------------------------
    # Module health API (for watchdog/http_server)
    # ------------------------------------------------------------------

    def update_module_health(self, module: str, **fields: Any) -> None:
        """
        Update health information for a given module. Thread-safe.
        """
        with self._health_lock:
            h = self._module_health.setdefault(module, {})
            h.update(fields)

    def get_module_health_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """
        Return a snapshot of current module health info. Thread-safe.
        """
        with self._health_lock:
            return {m: dict(info) for m, info in self._module_health.items()}

    # ------------------------------------------------------------------
    # Snapshot for debugging / watchdog
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """
        Return a lightweight snapshot of core DataBus state, including queue sizes
        and key shared state items. Safe for monitoring.
        """
        snapshot = {
            "image_queue_size": self.image_queue.qsize(),
            "detect_queue_size": self.detect_queue.qsize(),
            # [REMOVED] processed_image_queue_size removed
            "error_log_size": self.error_log.qsize(),
        }
        
        # Access unified registry for core state (config, motor, deter, image item)
        with self._registry_lock:
            # Use get_state to correctly retrieve copies of complex objects
            snapshot["motor_location"] = self.get_state("motor_location", {})
            snapshot["deter_flag"] = self.get_state("deter_flag", False)
            snapshot["latest_processed_image_item_exists"] = self.get_state("latest_processed_image_item", None) is not None
            snapshot["registry_keys"] = list(self._registry.keys())
            
            # Include config key list for inspection
            config_dict = self._registry.get("config", {})
            snapshot["config_keys"] = list(config_dict.keys())

        with self._health_lock:
            snapshot["module_health"] = {
                m: dict(info) for m, info in self._module_health.items()
            }
            
        return snapshot


__all__ = [
    "DataBus",
    "ImageItem",
    "ProcessedImageItem",
    "DetectionItem",
    "ErrorEntry",
    "DEFAULT_CONFIG_STRUCTURE",
]