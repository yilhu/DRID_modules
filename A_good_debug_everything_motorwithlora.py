"""
debug_everything_motorwithlora.py

Full Integration Test Script (Optimized).
Based strictly on the structure of the working 'debug_detection_system.py'.

Pipelines: Camera -> YOLO -> DecisionLogic -> DataBus -> MotorWithLora (Trigger) -> Logger
Also runs HttpServer for monitoring.
"""

import time
import sys
import threading
import signal
import os
from typing import Dict, Any

# --- Import System Modules ---
from data_bus import DataBus
from read_camera import ReadCamera
from yolo_detector import YoloDetector
from decision_logic import DecisionLogicModule
from motor_with_lora import MotorWithLora
from logger import LoggerModule
from http_server import HttpServerModule

# --- DEBUG SETTINGS ---
# Adjust according to your hardware
CAMERA_INDEX = 0 
MODEL_PATH = "/home/arthurseray/Desktop/DRID/DRID_Modules/thermal_person_ncnn_model"
LOG_DIR = "./everything_debug_logs"
ARCHIVE_DIR = "./everything_debug_archive"

def get_debug_config() -> Dict[str, Any]:
    """
    Generates a debug configuration dictionary.
    Using the exact settings that worked in your detection test.
    """
    return {
        # Camera
        "camera_device_index": CAMERA_INDEX,
        "camera_period_s": 0.11, # ~9 FPS
        "camera_buffer_size": 1,
        
        # YOLO
        "yolo_model_weights_path": MODEL_PATH,
        "yolo_input_img_size": 256,
        "yolo_conf_threshold": 0.3,
        "yolo_iou_threshold": 0.45,
        "yolo_bgr_to_rgb_conversion": False,
        
        # Decision Logic
        "decision_logic_time_window_s": 2.0,
        "decision_logic_min_frame_ratio": 0.65,
        "decision_logic_cooldown_s": 10.0,
        "decision_logic_reset_delay_s": 3.0,
        "decision_logic_min_total_score": 10.0,
        
        # Logger
        "logger_working_log_path": os.path.join(LOG_DIR, "system_log.txt"),
        "logger_image_archive_dir": ARCHIVE_DIR,
        
        # HTTP Server
        "http_server_port": 5000,
        # Default poll interval is usually fine, but ensure it's not aggressive
        "http_server_poll_interval": 0.1 
    }

def main():
    # 1. Prepare directories
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    # 2. Initialize DataBus
    print("[Main] Initializing DataBus with FULL config...")
    bus = DataBus(config_override=get_debug_config())

    # 3. Instantiate modules 
    # INCLUDES MOTOR THIS TIME
    print("[Main] Instantiating modules (including Motor)...")
    modules = [
        ReadCamera(name="Camera", data_bus=bus),
        YoloDetector(name="YOLO", data_bus=bus),
        DecisionLogicModule(name="Logic", data_bus=bus),
        MotorWithLora(name="Motor", data_bus=bus), # <--- Added Motor
        LoggerModule(name="Logger", data_bus=bus),
        HttpServerModule(name="Server", data_bus=bus)
    ]

    # 4. Setup signal handling (Standard Pattern)
    stop_event = threading.Event()
    
    def signal_handler(sig, frame):
        print("\n[Main] Shutdown signal received (Ctrl+C). Stopping system...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 5. Start modules
    print("[Main] Starting all threads...")
    for mod in modules:
        mod.start()

    # 6. Main loop
    print("-" * 50)
    print(f"[Main] System is RUNNING (Motor Active).")
    print(f"[Main] HTTP Dashboard available at: http://127.0.0.1:5000")
    print("[Main] Press Ctrl+C to stop.")
    print("-" * 50)

    try:
        # Mimic the 'good' script: Just sleep. 
        # Do NOT call bus.snapshot() here to avoid locking the bus from the main thread.
        while not stop_event.is_set():
            time.sleep(0.5) 
            
    except Exception as e:
        print(f"[Main] Unexpected error: {e}")
        stop_event.set()
        
    finally:
        # 7. Cleanup
        print("\n[Main] Stopping modules...")
        for mod in modules:
            mod.stop()
        
        # Wait for threads to finish
        for mod in modules:
            # Short timeout to prevent hanging if Motor gets stuck in sleep
            mod.join(timeout=2.0)
            if mod.is_alive():
                print(f"[Main] Warning: {mod.name} did not terminate cleanly.")
            else:
                print(f"[Main] {mod.name} stopped.")
                
        print("[Main] Debug session finished.")

if __name__ == "__main__":
    main()