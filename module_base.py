import threading
import time
from abc import ABC, abstractmethod
import traceback
from typing import TYPE_CHECKING, Dict, Any

if TYPE_CHECKING:
    from data_bus import DataBus


class BaseModule(ABC, threading.Thread):
    """
    Generic base class for all modules.

    Responsibilities:
      - Run as a dedicated thread.
      - Call step() in a loop to perform actual work.
      - Collect health metrics (read by the watchdog, written to the data bus by watchdog).
      - Automatically stop the module when too many consecutive failures occur.

    Conventions:
      - Subclasses typically implement step(), and may override setup()/teardown().
      - For long loops / blocking operations inside step(), periodically check should_stop().
    """

    def __init__(
        self,
        name: str,
        data_bus: "DataBus",
        daemon: bool = True,
        max_consecutive_fail: int = 5,
        fail_backoff_s: float = 0.1,
    ):
        super().__init__(name=name, daemon=daemon)
        self.name = name
        self.data_bus = data_bus
        self._stop_event = threading.Event()

        # Consecutive failure control
        self.max_consecutive_fail = max_consecutive_fail
        self._fail_backoff_s = fail_backoff_s
        self._consecutive_fail = 0

        # Health metrics (protected by lock)
        self._health_lock = threading.Lock()
        self.last_beat_ts: float = 0.0
        self.last_step_duration: float = 0.0
        self.last_exception_str: str = ""
        self.last_exception_time: float = 0.0
        self.ok_count: int = 0
        self.fail_count: int = 0

    # ---------- Control interface ----------

    def stop(self):
        """
        Request this module to stop.
        The thread will exit the run() loop once step() returns.
        """
        self._stop_event.set()

    def should_stop(self) -> bool:
        """
        Helper for subclasses to check inside step().

        Recommended pattern inside long loops or blocking sections:
            if self.should_stop():
                return
        """
        return self._stop_event.is_set()

    # ---------- Optional setup/teardown hooks ----------

    def setup(self) -> None:
        """
        Optional: called once before the main loop starts.

        Subclasses may override this to open resources (camera, serial ports, sockets, ...).
        Exceptions here will abort the thread before entering the loop.
        """
        pass

    def teardown(self) -> None:
        """
        Optional: called once after the main loop exits.

        Subclasses may override this to close resources.
        Exceptions here are caught and only logged in the health metrics.
        """
        pass

    @abstractmethod
    def step(self):
        """
        One unit of work for the module.

        Guidelines for subclasses:
          - Avoid indefinite blocking (use timeouts on queue.get(), I/O, etc.).
          - In internal long loops, periodically call self.should_stop() and return if True.
        """
        pass

    # ---------- Main loop ----------

    def run(self):
        """
        Main scheduling loop:
          - Calls setup() once
          - Repeatedly calls step()
          - Measures duration
          - Updates success/failure counters
          - Stores last exception as a string with traceback
          - Automatically stops this module after too many consecutive failures
          - Calls teardown() once on exit
        """
        # One-time setup
        try:
            self.setup()
        except Exception as e:
            now = time.time()
            exc_str = f"{repr(e)}\n{traceback.format_exc()}"
            with self._health_lock:
                self.fail_count += 1
                self._consecutive_fail += 1
                self.last_exception_str = exc_str
                self.last_exception_time = now
                self.last_step_duration = 0.0
                self.last_beat_ts = now
            # Setup failure: do not enter the main loop
            self._stop_event.set()
            return

        # Main loop
        while not self._stop_event.is_set():
            t0 = time.time()
            try:
                self.step()
                now = time.time()
                with self._health_lock:
                    self.ok_count += 1
                    self._consecutive_fail = 0
                    self.last_exception_str = ""
                    self.last_exception_time = 0.0
                    self.last_step_duration = now - t0
                    self.last_beat_ts = now

            except Exception as e:
                now = time.time()
                exc_str = f"{repr(e)}\n{traceback.format_exc()}"

                with self._health_lock:
                    self.fail_count += 1
                    self._consecutive_fail += 1
                    self.last_exception_str = exc_str
                    self.last_exception_time = now
                    self.last_step_duration = now - t0
                    self.last_beat_ts = now

                # Stop this module if too many consecutive failures
                if self._consecutive_fail >= self.max_consecutive_fail:
                    self._stop_event.set()
                else:
                    # Small backoff on failure to avoid busy-looping at 100% CPU
                    time.sleep(self._fail_backoff_s)

        # One-time teardown
        try:
            self.teardown()
        except Exception as e:
            now = time.time()
            exc_str = f"{repr(e)}\n{traceback.format_exc()}"
            with self._health_lock:
                self.fail_count += 1
                self.last_exception_str = exc_str
                self.last_exception_time = now
