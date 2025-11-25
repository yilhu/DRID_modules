### `BaseModule`

`BaseModule` is a common threaded skeleton for all long-running modules. It:

- runs in its own thread and repeatedly calls `step()`,
- tracks basic health metrics for external monitoring,
- automatically stops after too many consecutive failures.

#### Key parameters

    BaseModule(
        name: str,
        data_bus: DataBus,
        daemon: bool = True,
        max_consecutive_fail: int = 5,
        fail_backoff_s: float = 0.1,
    )

- `name`: module / thread name.  
- `data_bus`: shared data bus used to exchange data with other modules.  
- `max_consecutive_fail`: if `step()` raises this many times in a row, the module stops itself.  
- `fail_backoff_s`: short sleep after a failed `step()` to avoid busy loops.  
- `daemon`: whether the thread is daemonized.

The class also exposes simple health fields (e.g. `last_beat_ts`, `last_step_duration`, `ok_count`, `fail_count`, `last_exception_str`) that can be read by a watchdog.

#### Workflow and subclassing

1. `run()` calls `setup()` once, then loops calling `step()` until stopped, and finally calls `teardown()`.  
2. Each `step()` updates timing and success/failure counters; repeated failures trigger auto-stop.  
3. `stop()` requests shutdown; `should_stop()` lets `step()` check for this inside long or blocking loops.

To use it, create a subclass that:

- **must implement** `step(self)` with the module’s core logic,  
- **may override** `setup(self)` and `teardown(self)` to open/close resources,  
- should call `self.should_stop()` inside long loops to exit cleanly.



### `SerialModule`

`SerialModule` is a convenience base class for modules that communicate over a serial port. It extends `BaseModule` and adds:

- reading serial settings from the global `"config"` registry on the `DataBus`,
- opening the serial port in `setup()` and closing it in `teardown()`,
- a helper `_ensure_serial()` that subclasses call before doing serial I/O.

#### Key parameters

    SerialModule(
        name: str,
        data_bus: DataBus,
        config_prefix: str,
        default_port: str,
        default_baudrate: int,
        daemon: bool = True,
        max_consecutive_fail: int = 5,
        fail_backoff_s: float = 0.1,
    )

- `name`: module / thread name (passed to `BaseModule`).  
- `data_bus`: shared `DataBus` instance.  
- `config_prefix`: prefix for config keys; e.g. `"lora"` → reads `"lora_serial_port"` and `"lora_serial_baud"` from `data_bus["config"]`.  
- `default_port`: fallback serial port if config does not define one.  
- `default_baudrate`: fallback baudrate if config does not define one.  
- `daemon`, `max_consecutive_fail`, `fail_backoff_s`: forwarded to `BaseModule` for threading and failure handling.

At construction time, `SerialModule` reads the global config dict from the `DataBus` registry entry `"config"` and resolves:

- `self.port` from `"<config_prefix>_serial_port"` or `default_port`,  
- `self.baudrate` from `"<config_prefix>_serial_baud"` or `default_baudrate`.

The active serial handle is stored in:

- `self.ser` (a `serial.Serial` instance or `None`).

#### Workflow and subclassing

- `setup()`  
  Called once before the main loop. It simply calls `_ensure_serial()` to open the serial port.

- `teardown()`  
  Called once on shutdown. It closes `self.ser` (if open) and clears the reference.

- `_ensure_serial()`  
  Ensures that the serial port is open. If `self.ser` is `None` or closed, it opens the port using `self.port` and `self.baudrate` with non-blocking read (`timeout=0.0`) and `write_timeout=1.0`.  
  On failure, it logs an error and raises, so `BaseModule` can apply its backoff and auto-stop logic.

To use it, create a subclass that:

- **must implement** `step(self)` (inherited abstract method from `BaseModule`) and call `_ensure_serial()` before doing any serial I/O,  
- **may override** `setup(self)` / `teardown(self)` if extra resources are needed,  
- can access `self.ser` directly (e.g. `self.ser.read()`, `self.ser.write(...)`) once `_ensure_serial()` has succeeded.



### `DataBus`

`DataBus` is the central, thread-safe hub for all shared data in the detection unit running on the Raspberry Pi.  
A single instance is created in `main.py` and passed to all modules.

It:

- owns the main shared queues,
- provides generic helpers for working with queues,
- offers one semantic helper to keep detection and processed-image streams aligned,
- maintains a small amount of shared state, a generic registry, and basic health info for monitoring.

---

#### Core queues

The constructor creates four bounded queues:

- `image_queue`  
  Raw frames (`ImageItem`) from `read_camera.py` → `yolo_detector.py`.

- `detect_queue`  
  Detection results (`DetectionItem`) from `yolo_detector.py` → `decision_logic.py` / `logger.py` / `lora_comm.py` / `http_server.py`.

- `processed_image_queue`  
  Processed / annotated frames (`ProcessedImageItem`) for UI / HTTP.

- `error_log`  
  Structured error entries (`ErrorEntry`) for logging and watchdogs.

The payload types `ImageItem`, `ProcessedImageItem`, `DetectionItem`, and `ErrorEntry` are simple dataclasses defined in the same module for clarity.

---

#### Generic queue helpers

All modules are expected to use the generic helpers for both core queues and any additional queues created via the registry:

- `queue_put(q, item, drop_oldest_if_full=False)`  
  Put an item into a bounded or unbounded queue. When `drop_oldest_if_full=True` and the queue is full, the oldest item is dropped before inserting the new one.

- `queue_get(q, timeout=None)`  
  FIFO read with configurable blocking:
  - `timeout=None`: block until an item is available  
  - `timeout=0`: non-blocking, return `None` if empty  
  - `timeout>0`: block up to `timeout` seconds

- `drain_queue(q, max_items=None)`  
  Non-blocking drain; repeatedly calls `get_nowait()` until empty or `max_items` is reached.

- `get_latest_from_queue(q)`  
  Non-blocking helper that consumes all items and returns only the last one (useful for UI modules that only care about the most recent frame/state).

---

#### Detection + processed-image pairing

`push_detection_with_image(...)` is a convenience helper for `yolo_detector.py`:

- pushes a `DetectionItem` into `detect_queue` and a matching `ProcessedImageItem` into `processed_image_queue`,
- uses a shared timestamp and `meta` plus an internal lock to keep the two queues aligned.

This allows UI modules to consume `processed_image_queue` without interfering with the detection stream used by decision logic and logging.  
All other type-specific push/get logic lives in the respective modules.

---

#### Shared state, registry, and health

`DataBus` also maintains:

- **Motor location and deterrence flag**  
  - `set_motor_location(...)` / `get_motor_location()` for a shared motor state dict.  
  - `set_deter_flag(value)` / `get_deter_flag()` for a global deterrence flag.

- **Generic registry for module-specific resources**  
  - `get_or_create(key, factory)` to lazily create and share arbitrary objects (e.g. extra queues).  
  - `has_key(key)` and `registry_keys()` for inspection.

- **Per-module health information**  
  - `update_module_health(module, **fields)` to store heartbeat / error metrics.  
  - `get_module_health_snapshot()` to retrieve a copy of all health entries.

Finally, `snapshot()` returns a lightweight dictionary with queue sizes, shared flags, registry keys, and health info, intended for use by `watchdog.py` and `http_server.py` for status pages and debugging.
