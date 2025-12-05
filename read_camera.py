"""
read_camera.py

USB camera reader module (Optimized for CVBS/USB Adapters).
Uses configuration from DataBus.get_state("config").
"""

from __future__ import annotations

import time
from typing import Optional, Dict, Any

import cv2
import numpy as np

# Import necessary components from the common modules
from data_bus import DataBus, ImageItem
from module_base import BaseModule


class ReadCamera(BaseModule):
    """
    USB camera reader implemented as a BaseModule thread.
    """
    
    # Configuration prefix to match keys in DataBus.config
    CONFIG_PREFIX = "camera"

    def __init__(
        self,
        data_bus: DataBus,
        name: str = "read_camera",
        daemon: bool = True,
        **kwargs: Any,
    ) -> None:
        
        self.config_prefix = self.CONFIG_PREFIX

        # Get the unified configuration dictionary from the DataBus state registry
        cfg: Dict[str, Any] = data_bus.get_state("config", {})

        # Helper to retrieve configuration values using the predefined prefix
        def get_cfg(key: str, default: Any = None) -> Any:
            """Retrieves a configuration value using the module's prefix."""
            full_key = f"{self.config_prefix}_{key}"
            # Use provided default only if the key is missing from the config dict
            return cfg.get(full_key, default)

        # --- Configuration Attributes (Read from DataBus config) ---
        
        # 1. BaseModule Configuration
        default_max_fail = 5
        max_fail = get_cfg("max_consecutive_fail", default_max_fail)
        
        # Pass name, data_bus, daemon, and the now-required max_consecutive_fail 
        # back to the BaseModule parent constructor
        super().__init__(
            name=name, 
            data_bus=data_bus, 
            daemon=daemon,
            max_consecutive_fail=max_fail, 
            **kwargs
        )
        
        # 2. ReadCamera Specific Configuration
        self.device_index: int = int(get_cfg("device_index", 0))
        self.req_width: Optional[int] = get_cfg("req_width")
        self.req_height: Optional[int] = get_cfg("req_height")
        self.period_s: float = float(get_cfg("period_s", 0.0))
        self.fourcc_str: Optional[str] = get_cfg("fourcc")
        self.buffer_size: int = int(get_cfg("buffer_size", 1))

        # --- Internal Runtime Attributes ---
        self.cap: Optional[cv2.VideoCapture] = None
        self._last_frame_ts: float = 0.0

    def setup(self) -> None:
        """
        Open the USB camera with specific settings for CVBS adapters.
        """
        print(f"[{self.name}] Setup: Opening camera {self.device_index}...")
        
        # Utilisation de V4L2 explicitement pour Linux/RPi
        self.cap = cv2.VideoCapture(self.device_index, cv2.CAP_V4L2)

        if not self.cap.isOpened():
            raise RuntimeError(f"Failed to open USB camera (index={self.device_index})")

        # 1. Configuration du format de pixel (FourCC)
        if self.fourcc_str and len(self.fourcc_str) == 4:
            fourcc_code = cv2.VideoWriter_fourcc(*self.fourcc_str)
            self.cap.set(cv2.CAP_PROP_FOURCC, fourcc_code)
            print(f"[{self.name}] FourCC set to: {self.fourcc_str}")

        # 2. Configuration Résolution
        if self.req_width is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.req_width)
        if self.req_height is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.req_height)

        # 3. Configuration Latence (Buffer Size)
        # Très important pour le temps réel sur RPi
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, self.buffer_size)

        # Warm up
        for _ in range(3):
            ok, _ = self.cap.read()
            if not ok:
                break

        self._last_frame_ts = time.time()
        print(f"[{self.name}] Camera opened successfully.")

    def step(self) -> None:
        """
        Grabs one frame and pushes it to DataBus.image_queue.
        """
        
        # Gestion de la fréquence de capture
        now = time.time()
        if self.period_s > 0.0:
            remaining = self.period_s - (now - self._last_frame_ts)
            if remaining > 0:
                time.sleep(remaining)
                now = time.time()
            
        if self.cap is None or not self.cap.isOpened():
            raise RuntimeError("USB camera is not opened")

        # Capture
        ok, frame = self.cap.read()
        
        if not ok or frame is None:
            raise RuntimeError("Failed to read frame from USB camera")

        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)
        
        image_item = ImageItem(
            frame=frame,
            timestamp=now,
            meta={
                "camera_prefix": self.config_prefix,
                "device_index": self.device_index,
                "width": frame.shape[1],
                "height": frame.shape[0],
            },
        )

        # Push to queue (non-blocking)
        self.data_bus.queue_put(
            self.data_bus.image_queue,
            image_item,
            drop_oldest_if_full=True,
        )

        self._last_frame_ts = now

    def teardown(self) -> None:
        if self.cap is not None:
            print(f"[{self.name}] Releasing camera device.")
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None