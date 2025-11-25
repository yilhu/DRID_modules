"""
data_bus.py

Thread-safe shared data hub for the detection unit on Raspberry Pi.

Core responsibilities:
    - Own and expose the main shared queues:
        * image_queue            : raw frames (read_camera.py -> yolo_detector.py)
        * detect_queue           : detection results (yolo_detector.py -> decision_logic.py / logger.py / lora_comm.py / http_server.py)
        * processed_image_queue  : processed / annotated frames for UI / HTTP
        * error_log              : structured error entries for logger / watchdog

    - Provide generic queue helpers (for both core and module-specific queues):
        * queue_put(...)
        * queue_get(...)
        * drain_queue(...)
        * get_latest_from_queue(...)

    - Provide a single semantic helper that keeps detection and processed-image
      queues aligned:
        * push_detection_with_image(...)

    - Maintain shared state:
        * motor_location (dict)
        * deter_flag (bool)
        * generic registry for module-specific resources
        * per-module health info and snapshots for watchdog / http_server
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List


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

    This item includes both the annotated frame and the detection data so that
    http_server (and other UI modules) can display bounding boxes and labels
    without touching detect_queue, which is reserved for decision_logic.
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
# Core DataBus class
# ---------------------------------------------------------------------------

class DataBus:
    """
    Central hub for all shared data.

    One DataBus instance should be created in main.py and passed to all modules.
    Modules are expected to:
        - use the exposed queues directly (image_queue, detect_queue, etc.),
        - use the generic queue helpers for consistent behaviour,
        - optionally use push_detection_with_image() when they need detection
          and processed-image queues to stay aligned.
    """

    def __init__(
        self,
        image_queue_size: int = 3,
        detect_queue_size: int = 20,
        processed_image_queue_size: int = 20,
        error_queue_size: int = 200,
    ) -> None:
        # Bounded queues for streaming data
        self.image_queue: queue.Queue = queue.Queue(maxsize=image_queue_size)
        self.detect_queue: queue.Queue = queue.Queue(maxsize=detect_queue_size)
        self.processed_image_queue: queue.Queue = queue.Queue(
            maxsize=processed_image_queue_size
        )
        self.error_log: queue.Queue = queue.Queue(maxsize=error_queue_size)

        # Motor state
        self._motor_location_lock = threading.Lock()
        self._motor_location: Dict[str, Any] = {}

        # Global deterrence flag
        self._deter_flag_lock = threading.Lock()
        self._deter_flag: bool = False

        # Generic key-value registry for module-specific resources
        self._registry_lock = threading.Lock()
        self._registry: Dict[str, Any] = {}

        # Lock for pushing detection + processed image together
        self._det_proc_lock = threading.Lock()

        # Per-module health (for watchdog/http_server)
        self._health_lock = threading.Lock()
        self._module_health: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Generic queue helpers (public, for core + private queues)
    # ------------------------------------------------------------------

    def queue_put(
        self,
        q: queue.Queue,
        item: Any,
        drop_oldest_if_full: bool = False,
    ) -> None:
        """
        Put an item into a queue (bounded or unbounded).

        - If q.maxsize == 0 (unbounded), this simply calls q.put(item).
        - If q.maxsize > 0 (bounded):
            * When drop_oldest_if_full is False, q.put(item) may block until
              space is available.
            * When drop_oldest_if_full is True and the queue is full, the
              oldest item is removed with get_nowait() before putting the
              new one, so this call will not block due to a full queue.
        """
        # For unbounded queues, q.full() is always False.
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

        timeout:
            None  -> block indefinitely until an item is available
            0     -> non-blocking; return None if empty
            >0    -> block up to timeout seconds

        Returns:
            - The item if successful.
            - None if the queue is empty and timeout expired (or immediately
              when timeout == 0).
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
        Non-blocking drain of a queue.

        - Repeatedly calls get_nowait() until the queue is empty or
          max_items (if provided) is reached.
        - Returns a list of items (possibly empty).
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

        - Repeatedly calls get_nowait() until the queue is empty.
        - Returns the last item retrieved, or None if the queue was empty.

        Use this for UI modules that only care about the most recent state
        (e.g. last processed frame), not the full history.
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
        Convenience method for yolo_detector.py.

        Push a detection result and its corresponding processed frame so
        that items in detect_queue and processed_image_queue refer to
        the same logical frame (same timestamp + meta).

        processed_image_queue carries copies of detection data so that
        http_server can display bounding boxes / labels without consuming
        detect_queue.

        This is the only semantic helper that couples two queues; all other
        type-specific push/get logic is expected to live in the respective
        modules.
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
            self.queue_put(
                self.detect_queue,
                det_item,
                drop_oldest_if_full=drop_oldest_if_full,
            )
            self.queue_put(
                self.processed_image_queue,
                img_item,
                drop_oldest_if_full=drop_oldest_if_full,
            )

    # ------------------------------------------------------------------
    # Motor location API
    # ------------------------------------------------------------------

    def set_motor_location(self, **kwargs: Any) -> None:
        """
        Update the motor location/state.

        Example:
            data_bus.set_motor_location(
                angle=theta,
                x=current_x,
                y=current_y,
            )
        """
        with self._motor_location_lock:
            self._motor_location.update(kwargs)

    def get_motor_location(self) -> Dict[str, Any]:
        """
        Return a copy of the current motor location/state.
        """
        with self._motor_location_lock:
            return dict(self._motor_location)

    # ------------------------------------------------------------------
    # Deterrence flag API
    # ------------------------------------------------------------------

    def set_deter_flag(self, value: bool) -> None:
        """
        Set the global deterrence flag.
        """
        with self._deter_flag_lock:
            self._deter_flag = bool(value)

    def get_deter_flag(self) -> bool:
        """
        Read the global deterrence flag.
        """
        with self._deter_flag_lock:
            return self._deter_flag

    # ------------------------------------------------------------------
    # Generic registry for module-specific resources
    # ------------------------------------------------------------------

    def get_or_create(self, key: str, factory: Callable[[], Any]) -> Any:
        """
        Get a resource by key from the internal registry, creating it on the
        first access with the given factory.

        Intended usage:
            tx_q = data_bus.get_or_create(
                "lora.tx_queue",
                lambda: queue.Queue(maxsize=10)
            )

        This method is thread-safe.
        """
        with self._registry_lock:
            if key not in self._registry:
                self._registry[key] = factory()
            return self._registry[key]

    def has_key(self, key: str) -> bool:
        """
        Check if a key exists in the registry.
        """
        with self._registry_lock:
            return key in self._registry

    def registry_keys(self) -> List[str]:
        """
        Return a list of all registry keys (for debugging / watchdog).
        """
        with self._registry_lock:
            return list(self._registry.keys())

    # ------------------------------------------------------------------
    # Module health API (for watchdog/http_server)
    # ------------------------------------------------------------------

    def update_module_health(self, module: str, **fields: Any) -> None:
        """
        Update health information for a given module.

        Typically called by watchdog.py after checking heartbeats,
        step durations, exception counters, etc.
        """
        with self._health_lock:
            h = self._module_health.setdefault(module, {})
            h.update(fields)

    def get_module_health_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """
        Return a snapshot of current module health info.
        """
        with self._health_lock:
            return {m: dict(info) for m, info in self._module_health.items()}

    # ------------------------------------------------------------------
    # Snapshot for debugging / watchdog
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """
        Return a lightweight snapshot of core DataBus state.

        Safe to call from watchdog.py / http_server.py for monitoring.
        """
        with self._motor_location_lock, self._deter_flag_lock:
            snapshot = {
                "motor_location": dict(self._motor_location),
                "deter_flag": self._deter_flag,
                "image_queue_size": self.image_queue.qsize(),
                "detect_queue_size": self.detect_queue.qsize(),
                "processed_image_queue_size": self.processed_image_queue.qsize(),
                "error_log_size": self.error_log.qsize(),
            }
        with self._registry_lock:
            snapshot["registry_keys"] = list(self._registry.keys())
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
]
