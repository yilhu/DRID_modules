"""
http_server.py (Flask version) - Dashboard Optimized

HTTP server module for exposing DataBus state and processed images.
Features:
- Live Dashboard: Combines Video Stream + Real-time JSON Status.
- Reads ALL configuration from DataBus.get_state("config").
- Uses unified DataBus.get_state() for thread-safe access to shared state.
- Reads latest image from DataBus registry ("latest_processed_image_item").
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime

# Import Flask components
from flask import Flask, Response, render_template_string
from werkzeug.serving import make_server

# Conditional imports for image processing
try:
    import cv2  # For JPEG encoding of frames
    import numpy as np
except ImportError:
    cv2 = None  # type: ignore
    np = None   # type: ignore

# Import necessary components from the common modules
from data_bus import DataBus, ProcessedImageItem, DEFAULT_CONFIG_STRUCTURE
from module_base import BaseModule


# ---------------------------------------------------------------------------
# Helpers for JSON Serialization
# ---------------------------------------------------------------------------

def json_default(value: Any) -> Any:
    """Helper to serialize complex types (like numpy arrays) into JSON."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='ignore')
    return repr(value)

# ---------------------------------------------------------------------------
# HTTP Server Module
# ---------------------------------------------------------------------------

class HttpServerModule(BaseModule):
    """
    A module that runs an HTTP server (Flask) in a separate thread.
    Serves a unified dashboard with MJPEG stream and AJAX-updated status.
    """
    
    # Configuration prefix to match keys in DataBus.config
    CONFIG_PREFIX = "http_server"

    def __init__(
        self,
        data_bus: DataBus,
        name: str = "Server",
        daemon: bool = True,
        **kwargs: Any,
    ):
        
        self.config_prefix = self.CONFIG_PREFIX
        
        # Get the unified configuration dictionary
        cfg: Dict[str, Any] = data_bus.get_state("config", {})
        
        def get_cfg(key: str, default: Any = None) -> Any:
            """Retrieves a configuration value using the module's prefix."""
            full_key = f"{self.config_prefix}_{key}"
            return cfg.get(full_key, default)

        # 1. BaseModule Configuration
        # Handle priority: kwargs > config > default
        if "max_consecutive_fail" in kwargs:
            max_fail = kwargs.pop("max_consecutive_fail")
        else:
            default_max_fail = DEFAULT_CONFIG_STRUCTURE.get(f"{self.CONFIG_PREFIX}_max_consecutive_fail", 5)
            max_fail = get_cfg("max_consecutive_fail", default_max_fail)
        
        super().__init__(
            name=name, 
            data_bus=data_bus, 
            daemon=daemon,
            max_consecutive_fail=max_fail,
            **kwargs
        )
        
        # 2. HttpServer Specific Configuration
        self.host: str = get_cfg("host", "127.0.0.1")
        self.port: int = int(get_cfg("port", 5000))
        self.poll_interval: float = float(get_cfg("poll_interval", 1.0))
        self.export_keys: List[str] = get_cfg("export_config_keys", ["motor_location", "deter_flag"])

        # --- Internal Runtime State ---
        self.app = Flask(__name__)
        self._server: Optional[make_server] = None
        self._server_thread: Optional[threading.Thread] = None

        self._snapshot: Dict[str, Any] = {}
        self._snapshot_lock = threading.Lock()
        
        self._latest_image_jpeg: Optional[bytes] = None
        self._latest_image_meta: Dict[str, Any] = {}
        self._image_lock = threading.Lock()

        # Bind Flask routes
        self.app.add_url_rule("/status", view_func=self._handle_status, methods=["GET"])
        self.app.add_url_rule("/image", view_func=self._handle_image, methods=["GET"])
        self.app.add_url_rule("/stream", view_func=self._handle_stream, methods=["GET"])
        self.app.add_url_rule("/", view_func=self._handle_home, methods=["GET"])

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def setup(self) -> None:
        """Initialize and start the WSGI server."""
        print(f"[{self.name}] Setup: Starting HTTP server on http://{self.host}:{self.port}...")
        self._server = make_server(self.host, self.port, self.app, threaded=True)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        self._update_snapshot()

    def step(self) -> None:
        """Update snapshot and wait for events."""
        self._update_snapshot()
        # Wait efficiently for state changes or timeout
        self.data_bus.wait_for_state_change(timeout=self.poll_interval)
        
    def teardown(self) -> None:
        """Stop the server."""
        if self._server:
            self._server.shutdown()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2)

    # ------------------------------------------------------------------ #
    # Data Logic
    # ------------------------------------------------------------------ #

    def _update_snapshot(self) -> None:
        """Update the internal state snapshot from DataBus."""
        new_snapshot: Dict[str, Any] = {}
        
        # 1. Health & Configured Keys
        new_snapshot["health"] = self.data_bus.get_module_health_snapshot()
        for key in self.export_keys:
            new_snapshot[key] = self.data_bus.get_state(key)

        # 2. Latest Image Logic
        latest_item: Optional[ProcessedImageItem] = self.data_bus.get_state("latest_processed_image_item", None)
        
        if latest_item and cv2 is not None:
            jpeg_bytes = self._generate_jpeg(latest_item.frame)
            image_meta = {
                "timestamp": latest_item.timestamp,
                "width": latest_item.frame.shape[1],
                "height": latest_item.frame.shape[0],
                "detection_count": len(latest_item.boxes),
                "meta": latest_item.meta,
            }
            if jpeg_bytes:
                with self._image_lock:
                    self._latest_image_jpeg = jpeg_bytes
                    self._latest_image_meta = image_meta
            new_snapshot["latest_processed_image"] = image_meta
        else:
            with self._image_lock:
                 new_snapshot["latest_processed_image"] = self._latest_image_meta if self._latest_image_meta else {"status": "No data"}

        with self._snapshot_lock:
            self._snapshot = new_snapshot

    def get_snapshot(self) -> Dict[str, Any]:
        with self._snapshot_lock:
            return dict(self._snapshot)

    def get_latest_image_data(self) -> Tuple[Optional[bytes], Dict[str, Any]]:
        with self._image_lock:
            return self._latest_image_jpeg, dict(self._latest_image_meta)

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def _json_response(self, payload: Dict[str, Any], status: int = 200) -> Response:
        body = json.dumps(payload, default=json_default)
        return Response(body, status=status, mimetype="application/json")
        
    def _generate_jpeg(self, frame: np.ndarray) -> Optional[bytes]:
        if cv2 is None: return None
        # Assuming frame is BGR
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
        try:
            _, jpeg_buffer = cv2.imencode('.jpg', frame, encode_param)
            return jpeg_buffer.tobytes()
        except Exception as e:
            print(f"[{self.name}] JPEG Error: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Routes
    # ------------------------------------------------------------------ #

    def _handle_status(self) -> Response:
        snapshot = self.get_snapshot()
        snapshot["server_timestamp"] = time.time()
        return self._json_response(snapshot)

    def _handle_image(self) -> Response:
        jpeg_bytes, _ = self.get_latest_image_data()
        if jpeg_bytes is None:
            return self._json_response({"error": "No image data"}, status=503)
        return Response(jpeg_bytes, status=200, mimetype="image/jpeg")

    def _handle_stream(self) -> Response:
        def generate():
            while not self.should_stop():
                jpeg_bytes, _ = self.get_latest_image_data()
                if jpeg_bytes:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
                time.sleep(0.05) # ~20 FPS cap
        return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')
    
    def _handle_home(self) -> Response:
        """
        Dashboard Home:
        - Displays /stream (Video)
        - Displays /status (JSON) updated via JavaScript
        """
        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>DRID System Dashboard</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background-color: #f4f4f9;
                    color: #333;
                    margin: 0;
                    padding: 20px;
                    transition: background-color 0.5s ease;
                }}
                h1 {{ margin-bottom: 20px; }}
                
                .dashboard-container {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 20px;
                    align-items: flex-start;
                }}
                
                /* Video Section */
                .video-card {{
                    background: white;
                    padding: 10px;
                    border-radius: 8px;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                    flex: 1 1 640px; /* Grow/Shrink with base width 640 */
                    max-width: 100%;
                }}
                .video-card img {{
                    width: 100%;
                    height: auto;
                    border-radius: 4px;
                    display: block;
                }}

                /* Status Section */
                .status-card {{
                    background: white;
                    padding: 20px;
                    border-radius: 8px;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                    flex: 1 1 400px;
                    max-height: 80vh;
                    overflow-y: auto;
                }}
                pre {{
                    background: #2d2d2d;
                    color: #76e068;
                    padding: 15px;
                    border-radius: 5px;
                    font-size: 13px;
                    overflow-x: auto;
                    white-space: pre-wrap; /* Wrap long lines */
                }}

                /* Dynamic State Classes */
                .state-deterrence {{
                    background-color: #ff0000 !important; 
                    color: white !important;              
                    transition: background-color 0.1s;
                }}
            </style>
        </head>
        <body id="body-el">
            <h1>DRID System Dashboard</h1>
            
            <div class="dashboard-container">
                <div class="video-card">
                    <h3>Live Camera Stream</h3>
                    <img src="/stream" alt="Waiting for stream...">
                </div>

                <div class="status-card">
                    <h3>System Telemetry (Live)</h3>
                    <div id="connection-status" style="font-size: 0.8em; color: gray; margin-bottom: 5px;">Connecting...</div>
                    <pre id="json-display">Loading data...</pre>
                </div>
            </div>

            <script>
                const statusUrl = "/status";
                const jsonDisplay = document.getElementById('json-display');
                const bodyEl = document.getElementById('body-el');
                const connStatus = document.getElementById('connection-status');
                
                // Function to update the dashboard
                async function updateDashboard() {{
                    try {{
                        const response = await fetch(statusUrl);
                        if (!response.ok) throw new Error('Network response was not ok');
                        
                        const data = await response.json();
                        
                        // 1. Update JSON Text
                        jsonDisplay.textContent = JSON.stringify(data, null, 2);
                        
                        // 2. Visual Warning if Deterrence is active
                        // Checks if 'deter_flag' exists and is true
                        if (data.deter_flag === true) {{
                            bodyEl.classList.add('state-deterrence');
                        }} else {{
                            bodyEl.classList.remove('state-deterrence');
                        }}

                        connStatus.textContent = "Last Updated: " + new Date().toLocaleTimeString();
                        connStatus.style.color = "green";

                    }} catch (error) {{
                        console.error('Fetch error:', error);
                        connStatus.textContent = "Connection Lost. Retrying...";
                        connStatus.style.color = "red";
                    }}
                }}

                // Update every 500ms (2 FPS for data)
                setInterval(updateDashboard, 500);
                
                // Initial call
                updateDashboard();
            </script>
        </body>
        </html>
        """
        return Response(html, mimetype='text/html')