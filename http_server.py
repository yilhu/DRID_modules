"""
http_server.py (Flask version)

HTTP server module for exposing DataBus state on 127.0.0.1:5000.

- Runs as a BaseModule (thread) and periodically pulls selected values
  / latest queue items from the shared DataBus into an internal snapshot.
- Starts a Flask-based HTTP server (in a separate thread) that serves
  the latest snapshot to external clients.

Endpoints:
    GET /status
        -> JSON with latest snapshot (motor state, flags, detections, etc.)

    GET /image
        -> latest JPEG frame (if available), or 404 if none.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Dict, Optional, Tuple

try:
    import cv2  # For JPEG encoding of frames
except ImportError:
    cv2 = None  # type: ignore

from flask import Flask, jsonify, Response
from werkzeug.serving import make_server

from data_bus import BaseModule, DataBus, get_latest_from_queue  # adjust import if needed


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 5000
POLL_INTERVAL = 1.0  # seconds, can be overridden via ctor


# Which keys from DataBus to publish, and how.
# "snapshot_key": ("mode", "databus_key")
#
# Supported modes:
#   - "value"             : read a plain value from DataBus
#   - "queue_latest"      : get latest item from a queue
#   - "queue_latest_image": get latest frame from a queue, encode as JPEG;
#                           snapshot only stores metadata, image served via /image
EXPORT_CONFIG: Dict[str, Tuple[str, str]] = {
    "motor_location": ("value", "motor_location"),
    "deter_flag": ("value", "deter_flag"),
    "latest_detection": ("queue_latest", "detect_queue"),
    "processed_frame": ("queue_latest_image", "processed_image_queue"),
    # Example if watchdog writes aggregated health info:
    # "health": ("value", "watchdog.health_dict"),
}


# ---------------------------------------------------------------------------
# JSON utilities
# ---------------------------------------------------------------------------

def json_default(obj: Any) -> Any:
    """
    Fallback JSON serializer for non-standard objects.
    """
    try:
        import numpy as np  # type: ignore
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:
        pass

    return str(obj)


# ---------------------------------------------------------------------------
# HttpServerModule
# ---------------------------------------------------------------------------

class HttpServerModule(BaseModule):
    """
    Module that periodically pulls data from DataBus into a snapshot and
    exposes that snapshot via a small Flask-based HTTP server.

    Typical use:

        data_bus = DataBus()
        http_server = HttpServerModule(
            data_bus=data_bus,
            host="127.0.0.1",
            port=5000,
            poll_interval=1.0,
        )
        http_server.start()

    Then on the Pi:
        curl http://127.0.0.1:5000/status
        curl http://127.0.0.1:5000/image > latest.jpg
    """

    def __init__(
        self,
        data_bus: DataBus,
        name: str = "http_server",
        host: str = HOST,
        port: int = PORT,
        poll_interval: float = POLL_INTERVAL,
        export_config: Optional[Dict[str, Tuple[str, str]]] = None,
        daemon: bool = True,
    ) -> None:
        super().__init__(name=name, data_bus=data_bus, daemon=daemon)
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.export_config = export_config or dict(EXPORT_CONFIG)

        # Snapshot of values exposed via /status
        self._snapshot_lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {}

        # Latest encoded JPEG frame (served via /image)
        self._image_lock = threading.Lock()
        self._latest_image_jpeg: Optional[bytes] = None
        self._latest_image_ts: Optional[float] = None

        # Flask app & WSGI server
        self.app = Flask(__name__)
        self._setup_routes()
        self._server = None
        self._server_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # Flask routes
    # ------------------------------------------------------------------ #

    def _setup_routes(self) -> None:
        module = self  # capture self for inner functions

        @self.app.route("/", methods=["GET"])
        @self.app.route("/status", methods=["GET"])
        @self.app.route("/state", methods=["GET"])
        def status() -> Any:
            snapshot = module.get_snapshot()
            out = {
                "server_time": time.time(),
                "data": snapshot,
            }
            return module._json_response(out, status=200)

        @self.app.route("/image", methods=["GET"])
        def image() -> Any:
            image_bytes, ts = module.get_latest_image()
            if image_bytes is None:
                out = {"error": "no_image"}
                return module._json_response(out, status=404)
            return Response(image_bytes, mimetype="image/jpeg")

    # ------------------------------------------------------------------ #
    # BaseModule hooks
    # ------------------------------------------------------------------ #

    def setup(self) -> None:
        """
        Start the Flask-based HTTP server in a separate thread.
        """
        self._server = make_server(self.host, self.port, self.app)
        # Optional: tune timeout if needed (default is fine for low load)
        # self._server.timeout = 1.0

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"{self.name}_flask_thread",
            daemon=True,
        )
        self._server_thread.start()

    def step(self) -> None:
        """
        Periodically refresh the internal snapshot from DataBus.

        This method is called in a loop by BaseModule.run().
        """
        t0 = time.time()
        self._update_snapshot()

        # Sleep until next polling time, but remain responsive to stop()
        while True:
            if self.should_stop():
                break
            elapsed = time.time() - t0
            remaining = self.poll_interval - elapsed
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))

    def teardown(self) -> None:
        """
        Stop the HTTP server gracefully.
        """
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Snapshot & image helpers
    # ------------------------------------------------------------------ #

    def _update_snapshot(self) -> None:
        """
        Read configured items from DataBus and update the snapshot and
        latest image buffer (if configured).
        """
        new_snapshot: Dict[str, Any] = {}
        new_snapshot["snapshot_time"] = time.time()

        for snapshot_key, (mode, databus_key) in self.export_config.items():
            try:
                if mode == "value":
                    value = self.data_bus.get_or_create(databus_key, lambda: None)
                    new_snapshot[snapshot_key] = value

                elif mode == "queue_latest":
                    q = self.data_bus.get_or_create(databus_key, lambda: queue.Queue())
                    item = get_latest_from_queue(q)
                    new_snapshot[snapshot_key] = item

                elif mode == "queue_latest_image":
                    q = self.data_bus.get_or_create(databus_key, lambda: queue.Queue())
                    frame = get_latest_from_queue(q)
                    meta: Dict[str, Any] = {
                        "available": False,
                        "last_update": None,
                        "error": None,
                    }

                    if frame is not None and cv2 is not None:
                        try:
                            ok, buf = cv2.imencode(".jpg", frame)
                            if ok:
                                jpeg_bytes = buf.tobytes()
                                with self._image_lock:
                                    self._latest_image_jpeg = jpeg_bytes
                                    self._latest_image_ts = time.time()
                                meta["available"] = True
                                meta["last_update"] = self._latest_image_ts
                            else:
                                meta["error"] = "jpeg_encode_failed"
                        except Exception as e:
                            meta["error"] = f"encode_exception: {e}"
                    elif frame is not None and cv2 is None:
                        meta["error"] = "cv2_not_available"

                    new_snapshot[snapshot_key] = meta

                else:
                    new_snapshot[snapshot_key] = {
                        "error": f"unknown_mode:{mode}",
                    }
            except Exception as e:
                new_snapshot[snapshot_key] = {
                    "error": f"exception:{e}",
                }

        # Atomically swap snapshot
        with self._snapshot_lock:
            self._snapshot = new_snapshot

    def get_snapshot(self) -> Dict[str, Any]:
        """
        Return a shallow copy of the current snapshot for HTTP handlers.
        """
        with self._snapshot_lock:
            return dict(self._snapshot)

    def get_latest_image(self) -> Tuple[Optional[bytes], Optional[float]]:
        """
        Return the latest encoded JPEG bytes and timestamp.
        """
        with self._image_lock:
            return self._latest_image_jpeg, self._latest_image_ts

    # ------------------------------------------------------------------ #
    # Response helpers
    # ------------------------------------------------------------------ #

    def _json_response(self, payload: Dict[str, Any], status: int = 200) -> Any:
        """
        Helper to jsonify with custom default and status code.
        """
        # Use json.dumps to respect our json_default, then wrap with Response
        body = json.dumps(payload, default=json_default)
        resp = Response(body, status=status, mimetype="application/json")
        return resp
