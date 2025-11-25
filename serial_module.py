# serial_module.py

from __future__ import annotations

import time
from typing import Optional, Dict, Any

import serial  # pyserial

from module_base import BaseModule
from data_bus import DataBus


class SerialModule(BaseModule):
    """
    Base class for modules that talk over a serial port.

    Responsibilities:
      - Read serial configuration from DataBus "config" registry with a prefix.
      - Open/close the serial port in setup()/teardown().
      - Provide _ensure_serial() helper for subclasses.
    """

    def __init__(
        self,
        name: str,
        data_bus: DataBus,
        config_prefix: str,
        default_port: str,
        default_baudrate: int,
        daemon: bool = True,
        max_consecutive_fail: int = 5,
        fail_backoff_s: float = 0.1,
    ):
        super().__init__(
            name=name,
            data_bus=data_bus,
            daemon=daemon,
            max_consecutive_fail=max_consecutive_fail,
            fail_backoff_s=fail_backoff_s,
        )

        self.config_prefix = config_prefix

        # Read global config dict from DataBus registry
        cfg: Dict[str, Any] = self.data_bus.get_or_create("config", lambda: {})

        # Example keys: "lora_serial_port", "lora_serial_baud"
        self.port: str = cfg.get(f"{config_prefix}_serial_port", default_port)
        self.baudrate: int = int(
            cfg.get(f"{config_prefix}_serial_baud", default_baudrate)
        )

        self.ser: Optional[serial.Serial] = None

    # ---------- setup/teardown ----------

    def setup(self) -> None:
        """
        Open the serial port once before the main loop.
        """
        self._ensure_serial()

    def teardown(self) -> None:
        """
        Close the serial port on shutdown.
        """
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                # Only log in local logger; BaseModule health will record tracebacks if needed.
                self.logger.warning("Error while closing serial port", exc_info=True)
            finally:
                self.ser = None

    # ---------- helper ----------

    def _ensure_serial(self) -> None:
        """
        Make sure the serial port is open. If not, open it.

        Raise on failure so BaseModule's error handling can apply backoff
        and eventually stop the module.
        """
        if self.ser is not None and self.ser.is_open:
            return

        self.logger.info(
            f"[{self.name}] Opening serial port {self.port} @ {self.baudrate}..."
        )
        try:
            # Non-blocking read: timeout=0
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.0,
                write_timeout=1.0,
            )
        except Exception as e:
            self.logger.error(
                f"[{self.name}] Failed to open serial port {self.port}: {e}"
            )
            # Optional: also log via DataBus
            # self.data_bus.log_error(self.name, "ERROR",
            #                         f"Failed to open serial port {self.port}", str(e))
            raise
