"""
yolo_detector.py

Detects objects using the Ultralytics YOLO model (v8+).
Consumes: data_bus.image_queue (ImageItem: raw frame - expected to be a numpy array/cv2 frame)
Produces: data_bus.detect_queue (DetectionItem)
          [MODIFIED] DataBus Registry Snapshot (ProcessedImageItem -> "latest_processed_image_item")
"""

import time
import cv2
from typing import Any, List, Tuple, Optional, Dict
from ultralytics import YOLO
import numpy as np

# Assume BaseModule and DataBus are available in the project structure
from module_base import BaseModule
from data_bus import DataBus, ImageItem, ErrorEntry, DetectionItem, ProcessedImageItem

# --- DEFAULT CONFIGURATION (Fallbacks only, actual values loaded from DataBus) ---
DEFAULT_MODEL_PATH = "/home/arthurseray/Desktop/DRID/DRID_Modules/thermal_person_ncnn_model"
DEFAULT_INPUT_IMAGE_SIZE = 640
DEFAULT_CONF_THRESHOLD = 0.25
DEFAULT_IOU_THRESHOLD = 0.45
DEFAULT_QUEUE_TIMEOUT = 0.1
DEFAULT_MAX_FAIL = 5 # Default for BaseModule failure handling


class YoloDetector(BaseModule):
    """
    Module responsible for object detection using Ultralytics YOLO.
    It reads configuration from the DataBus 'config' state registry.
    """
    
    # Configuration prefix to match keys in DataBus.config
    CONFIG_PREFIX = "yolo"

    def __init__(
        self,
        data_bus: DataBus,
        name: str = "Yolo",
        daemon: bool = True,
        **kwargs: Any, # Allows passing BaseModule arguments (e.g., fail_backoff_s)
    ):
        
        self.config_prefix = self.CONFIG_PREFIX
        
        # Get the unified configuration dictionary from the DataBus state registry
        cfg: Dict[str, Any] = data_bus.get_state("config", {})
        
        # Helper to retrieve config values with fallback to defaults
        def get_cfg(key: str, default: Any) -> Any:
            """Retrieves a configuration value using the module's prefix."""
            full_key = f"{self.config_prefix}_{key}"
            # Use provided default only if the key is missing from the config dict
            return cfg.get(full_key, default)

        # 1. BaseModule Configuration
        max_fail = get_cfg("max_consecutive_fail", DEFAULT_MAX_FAIL)
        
        # Pass configuration to BaseModule.__init__
        super().__init__(
            name=name, 
            data_bus=data_bus, 
            daemon=daemon,
            max_consecutive_fail=max_fail,
            **kwargs
        )
        
        # 2. YoloDetector Specific Configuration
        self.model_weights_path: str = get_cfg("model_weights_path", DEFAULT_MODEL_PATH)
        self.input_img_size: int = int(get_cfg("input_img_size", DEFAULT_INPUT_IMAGE_SIZE))
        self.conf_threshold: float = float(get_cfg("conf_threshold", DEFAULT_CONF_THRESHOLD))
        self.iou_threshold: float = float(get_cfg("iou_threshold", DEFAULT_IOU_THRESHOLD))
        self.bgr_to_rgb: bool = bool(get_cfg("bgr_to_rgb_conversion", False))
        self.queue_timeout_s: float = float(get_cfg("queue_timeout_s", DEFAULT_QUEUE_TIMEOUT))
        
        self.model: Optional[YOLO] = None 
        self.class_names: Dict[int, str] = {}


    def setup(self) -> None:
        """
        Load the YOLO model before the main loop starts and apply runtime settings.
        """
        print(f"[{self.name}] Setup: Loading model from {self.model_weights_path}...")
        
        # Load the model using the configured path
        self.model = YOLO(self.model_weights_path, task="detect")
        
        # Get class names for labeling results
        self.class_names = self.model.names
        
        print(f"[{self.name}] Setup: Model loaded. Input size: {self.input_img_size}")

    def teardown(self) -> None:
        """
        Clean up resources if necessary.
        """
        # (YOLO model object usually cleans itself up, but this hook is available)
        pass

    def _process_yolo_results(self, frame: np.ndarray, results: Any) -> Tuple[np.ndarray, List[Any], List[float], List[str]]:
        """
        Helper to parse YOLO results and draw annotations.

        Returns:
            processed_frame, boxes_list, scores_list, labels_list
        """
        
        boxes_list: List[Any] = []
        scores_list: List[float] = []
        labels_list: List[str] = []
        
        annotated_frame = frame.copy()
        
        # We only expect one results object from model() call with batch size 1
        result = results[0]

        for box in result.boxes:
            # Apply confidence and IOU filtering are typically done in the model() call, 
            # but we ensure we only draw/collect boxes that pass the required conf threshold
            conf = float(box.conf[0])
            if conf < self.conf_threshold: # Check confidence (redundant if passed to model() call)
                continue
                
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
            cls_id = int(box.cls[0])
            label = self.class_names.get(cls_id, f"Unknown_{cls_id}")

            # Collect data for DataBus queues
            boxes_list.append([x1, y1, x2, y2])
            scores_list.append(conf)
            labels_list.append(label)

            # Draw box on the frame for processed_image_queue
            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            text = f"{label} {conf:.2f}"
            cv2.putText(
                annotated_frame,
                text,
                (x1, max(y1 - 5, 15)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        return annotated_frame, boxes_list, scores_list, labels_list

    def step(self):
        """
        1. Read the next frame from the image_queue.
        2. Perform YOLO prediction.
        3. Draw results on the frame.
        4. Push detection data to the queue and processed frame to the registry.
        """
        
        image_item: ImageItem = self.data_bus.queue_get(
            q=self.data_bus.image_queue,
            timeout=self.queue_timeout_s,
        )

        if image_item is None:
            return

        # --- Process Frame ---
        raw_frame = image_item.frame
        
        if raw_frame is None or not isinstance(raw_frame, np.ndarray):
            self.data_bus.queue_put(
                self.data_bus.error_log,
                ErrorEntry(time.time(), self.name, "WARNING", "Received invalid frame data from image_queue."),
                drop_oldest_if_full=True
            )
            return

        frame_meta = dict(image_item.meta)
        frame_meta['detector_module'] = self.name
        
        # --- Input Frame Preparation for YOLO ---
        input_frame_for_yolo = raw_frame
        # If configured, convert BGR (OpenCV default) to RGB 
        if self.bgr_to_rgb:
            input_frame_for_yolo = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2RGB)
        
        # 2. Perform YOLO inference
        t_yolo_start = time.time()
        
        # Run prediction with runtime parameters (conf/iou thresholds)
        results = self.model(
            input_frame_for_yolo, 
            imgsz=self.input_img_size, 
            conf=self.conf_threshold, # Filter results below this confidence
            iou=self.iou_threshold,   # NMS threshold
            verbose=False
        )
        t_yolo_end = time.time()
        
        frame_meta['yolo_inference_ms'] = (t_yolo_end - t_yolo_start) * 1000.0

        # 3. Draw results and parse data
        (
            processed_frame, 
            boxes, 
            scores, 
            labels
        ) = self._process_yolo_results(raw_frame, results) # Pass raw_frame to ensure BGR draw colors

        # 4. Push detection data (queue) and processed frame (registry snapshot)
        # [MODIFIED] push_detection_with_image now handles both the detect_queue push 
        # and the "latest_processed_image_item" state update.
        self.data_bus.push_detection_with_image(
            boxes=boxes,
            scores=scores,
            labels=labels,
            processed_frame=processed_frame,
            meta=frame_meta,
            drop_oldest_if_full=True, 
        )
        
        # Optional: Log detection event
        if boxes:
            log_message = f"Detection found: {len(boxes)} object(s)."
            error_entry = ErrorEntry(
                timestamp=time.time(),
                module=self.name,
                level="INFO",
                message=log_message
            )
            self.data_bus.queue_put(
                self.data_bus.error_log, 
                error_entry, 
                drop_oldest_if_full=True
            )