# DRID System (Detection, Response, and Intelligence Device)

DRID is a Raspberry Pi-based multi-threaded intelligent security system. It integrates USB camera capture, real-time YOLO object detection, temporal logic decision-making, stepper motor control, and LoRa communication.

The system core uses a **Producer-Consumer** architecture, managing data exchange and state via a thread-safe centralized `DataBus`.

## 🛠️ Quick Start

### 1. Environmental Dependencies
Ensure the Raspberry Pi has the following Python libraries installed:

```bash
pip install opencv-python ultralytics flask RPi.GPIO pyserial numpy

### 2. Run Integration Test
The system provides an integration debug script containing all modules. Before running, ensure hardware connections are correct (see Hardware section below).

```bash
python3 A_good_debug_everything_motorwithlora.py

Here is the third item of the **Quick Start** section:

```markdown
### 3. Access Dashboard
Once the system is running, access the following URL in a browser on the same LAN:
`http://<RaspberryPi_IP>:5000`

## ⚙️ Configuration Reference

All configuration items are stored in the `DataBus`'s `config` dictionary. You can modify these values in the `get_debug_config()` function within `A_good_debug_everything_motorwithlora.py`.

### 1. 📷 Camera Module (ReadCamera)
*Prefix: `camera_`*
Responsible for reading video streams from USB devices and controlling frame rate to reduce CPU load.

| Key | Type | Recommended | Description |
| :--- | :--- | :--- | :--- |
| `camera_device_index` | int | `0` | Camera device ID (usually `/dev/video0` is 0). |
| `camera_period_s` | float | `0.11` | Sampling period (seconds). `0.11` is approx 9 FPS. Lowering increases CPU load. |
| `camera_buffer_size` | int | `1` | Hardware buffer size. **Must be set to 1** to ensure real-time performance and avoid processing old frames. |
| `camera_req_width` | int | `None` | (Optional) Force camera width, e.g., `640`. `None` uses default. |
| `camera_req_height` | int | `None` | (Optional) Force camera height, e.g., `480`. `None` uses default. |

### 2. 🧠 YOLO Detector Module (YoloDetector)
*Prefix: `yolo_`*
Responsible for loading the model and performing inference on every frame.

| Key | Type | Recommended | Description |
| :--- | :--- | :--- | :--- |
| `yolo_model_weights_path` | str | *(Path)* | Absolute path to `.pt` or NCNN model file. |
| `yolo_input_img_size` | int | `256` | Model input image size. Must match training size (e.g., 256, 416, 640). |
| `yolo_conf_threshold` | float | `0.3` | Confidence threshold (0.0 - 1.0). Boxes below this are discarded. |
| `yolo_iou_threshold` | float | `0.45` | NMS (Non-Maximum Suppression) IOU threshold to remove overlapping boxes. |
| `yolo_bgr_to_rgb_conversion`| bool | `False` | Set `True` if trained on RGB (standard YOLOv8); `False` for OpenCV default BGR. |

### 3. ⚖️ Decision Logic Module (DecisionLogicModule)
*Prefix: `decision_logic_`*
To prevent false positives, this module analyzes detection history over time to decide whether to trigger an alarm (`deter_flag`).

| Key | Type | Recommended | Description |
| :--- | :--- | :--- | :--- |
| `decision_logic_time_window_s` | float | `2.0` | Time window (seconds). The system only analyzes data within this recent window. |
| `decision_logic_min_frame_ratio`| float | `0.65` | Min frame ratio (0.0 - 1.0). E.g., `0.65` means 65% of frames in the window must have detections. |
| `decision_logic_min_total_score`| float | `10.0` | Min total score. Sum of all confidence scores in the window must exceed this. |
| `decision_logic_cooldown_s` | float | `10.0` | Cooldown (seconds). Prevents re-triggering immediately after an alarm. |
| `decision_logic_reset_delay_s` | float | `3.0` | Auto-reset delay. Duration `deter_flag` remains `True` after triggering. |

### 4. 🪵 Logger & Archive Module (LoggerModule)
*Prefix: `logger_`*
Responsible for logging events and saving image evidence when an alarm triggers.

| Key | Type | Recommended | Description |
| :--- | :--- | :--- | :--- |
| `logger_working_log_path` | str | `./logs/...` | Path to store text log files. |
| `logger_image_archive_dir` | str | `./archive` | Directory to save alarm snapshots (`.jpg`). |

### 5. 🌐 HTTP Server Module (HttpServerModule)
*Prefix: `http_server_`*
Provides the Web monitoring interface.

| Key | Type | Recommended | Description |
| :--- | :--- | :--- | :--- |
| `http_server_port` | int | `5000` | Web server port number. |
| `http_server_poll_interval` | float | `0.1` | Status poll interval. Affects refresh rate of JSON data on the dashboard. |

## 🔌 Hardware Configuration (Hardware Constants)

⚠️ **Note:** The hardware pin configuration for the `MotorWithLora` module is currently **Hardcoded** at the top of the `motor_with_lora.py` file. To change pins, you must edit that file directly.

**File Location:** `motor_with_lora.py`

```python
# ==========================================
# HARDWARE CONFIGURATION (Edit inside motor_with_lora.py)
# ==========================================
DIR_PIN  = 22           # Direction Control Pin (BCM)
STEP_PIN = 27           # Step Pulse Pin (BCM)
EN_PIN   = 17           # Enable Pin (BCM, Active LOW)

# Mechanical Parameters
STEPS_PER_REV = 200     # Steps per revolution (Usually 200 for 1.8deg motors)
MICROSTEPS    = 16      # Microstepping setting
SWEEP_ANGLE_TOTAL = 120 # Total scan angle

# Communication Parameters
SERIAL_PORT = '/dev/serial0' # LoRa Serial Port Address
BAUD_RATE   = 9600           # LoRa Baud Rate

## 📁 Directory Structure

After running the system, the following directories are automatically generated to store data:

* **`everything_debug_logs/`**: Stores system running logs (`system_log.txt`), recording the timestamp, motor angle, and trigger reason for every detection event.
* **`everything_debug_archive/`**: Stores on-site snapshots (`YYYYMMDD_HHMMSS_det.jpg`) when alarms occur.

