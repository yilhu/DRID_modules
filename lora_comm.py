"""
lora_comm.py

LoRa communication module for Raspberry Pi, talking to an RP2040 + SX1262
bridge over USB/UART.

RP2040 firmware protocol (from main.c):

- Host -> RP2040 (TX):
    Send one line:
        TX:<payload>\n
  RP2040 will transmit <payload> over LoRa.

- RP2040 -> Host (RX):
    When a LoRa frame is received:
        RX:<payload>\r\n
        RSSI:<rssi> dBm, SNR:<snr_raw>/4 dB\r\n

- Other lines:
    TXDONE
    RXTIMEOUT
    RXERROR
    TXTIMEOUT
    OnTxTimeout
    OnRxTimeout
    OnRxError
    ...
  are status / debug messages.

DataBus interface (registry-based):

    - "lora.tx_queue": queue.Queue
        Other modules put messages here to be sent over LoRa.
        Accepted formats:
            - str      -> used directly as payload
            - bytes    -> decoded as UTF-8 (errors replaced)
            - dict     -> dict["payload"] is used as payload if present;
                           otherwise str(dict) is used.

    - "lora.rx_queue": queue.Queue
        This module puts dict objects here when a LoRa frame is received.
        Example entry:
            {
                "timestamp": <float>,
                "payload": "<payload_str>",
                "rssi_dbm": -71.5,
                "snr_db": 4.0,
                "raw_lines": ["RX:...", "RSSI:..."]
            }

    - "lora.health": dict
        A shared health summary updated by this module, for watchdog
        and http_server to read. Example structure:
            {
                "rx_count": int,
                "tx_count": int,
                "error_count": int,
                "last_rx_ts": float | None,
                "last_tx_ts": float | None,
                "last_error_ts": float | None,
                "last_error_type": str | None,
                "last_rssi_dbm": float | None,
                "last_snr_db": float | None,
                "last_rx_payload": str | None,
                "last_status_line": str | None,
            }

    - "config": dict (global config dictionary, optional)
        Used here for serial configuration:
            config["lora_serial_port"] = "/dev/ttyACM1"
            config["lora_serial_baud"] = 9600

This module is designed as a SerialModule subclass, to be monitored by your watchdog.
"""

import time
import queue
from typing import Optional, Dict, Any

import serial  # pyserial

from data_bus import DataBus
from serial_module import SerialModule


class LoRaCommModule(SerialModule):
    """
    LoRa communication module using RP2040 as a UART-to-LoRa bridge.
    """

    def __init__(self, name: str, data_bus: DataBus, daemon: bool = True):
        # SerialModule will:
        #   - read config["lora_serial_port"] / ["lora_serial_baud"] if present
        #   - default to /dev/ttyACM0 @ 115200 otherwise
        #   - handle setup()/teardown() -> open/close serial
        super().__init__(
            name=name,
            data_bus=data_bus,
            config_prefix="lora",
            default_port="/dev/ttyACM0",
            default_baudrate=115200,
            daemon=daemon,
        )

        # Queues registered in DataBus (private to LoRa module and its users)
        self.tx_queue: queue.Queue = self.data_bus.get_or_create(
            "lora.tx_queue",
            lambda: queue.Queue(maxsize=10),
        )
        self.rx_queue: queue.Queue = self.data_bus.get_or_create(
            "lora.rx_queue",
            lambda: queue.Queue(maxsize=10),
        )

        # Internal RX buffer for assembling lines
        self._rx_buf = bytearray()

        # Pending RX payload waiting for RSSI line
        self._pending_rx: Optional[Dict[str, Any]] = None

        # Simple TX rate limiting (to avoid flooding RP2040)
        self.last_tx_time: float = 0.0
        self.min_tx_interval: float = 0.05  # seconds

        # Health summary shared via DataBus registry
        default_health = {
            "rx_count": 0,
            "tx_count": 0,
            "error_count": 0,
            "last_rx_ts": None,
            "last_tx_ts": None,
            "last_error_ts": None,
            "last_error_type": None,
            "last_rssi_dbm": None,
            "last_snr_db": None,
            "last_rx_payload": None,
            "last_status_line": None,
        }
        self.health: Dict[str, Any] = self.data_bus.get_or_create(
            "lora.health",
            lambda: default_health.copy(),
        )

    # ------------------------------------------------------------------
    # BaseModule required step()
    # ------------------------------------------------------------------

    def step(self):
        """
        One iteration of the module logic:
            1) Read and parse any available lines from RP2040.
            2) Send at most one pending TX message to RP2040.
        """
        # 1) Ensure serial port is available (SerialModule helper)
        self._ensure_serial()

        # 2) Read and parse incoming data from serial
        self._read_serial_and_parse()

        # 3) Send pending TX message, if any
        self._send_one_tx_if_available()

    # ------------------------------------------------------------------
    # Serial reading and parsing
    # ------------------------------------------------------------------

    def _read_serial_and_parse(self):
        """
        Read all available bytes from serial (non-blocking) and parse
        into text lines separated by '\n'. For each line, call
        _handle_line().
        """
        if self.ser is None or not self.ser.is_open:
            return

        try:
            bytes_waiting = self.ser.in_waiting
        except (OSError, serial.SerialException) as e:
            self.logger.error(f"Serial in_waiting error: {e}")
            # self.data_bus.log_error(self.name, "ERROR",
            #                         "LoRa serial in_waiting error", str(e))
            raise

        if bytes_waiting <= 0:
            return

        try:
            data = self.ser.read(bytes_waiting)
        except (
            OSError,
            serial.SerialTimeoutException,
            serial.SerialException,
        ) as e:
            self.logger.error(f"Serial read error: {e}")
            # self.data_bus.log_error(self.name, "ERROR",
            #                         "LoRa serial read error", str(e))
            raise

        if not data:
            return

        self._rx_buf.extend(data)

        # Process all complete lines currently in the buffer
        while True:
            newline_idx = self._rx_buf.find(b"\n")
            if newline_idx == -1:
                break  # no complete line yet

            # Extract one line including '\n'
            line_bytes = self._rx_buf[: newline_idx + 1]
            del self._rx_buf[: newline_idx + 1]

            # Decode to string
            try:
                line = line_bytes.decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                line = line_bytes.decode("latin1", errors="replace")

            line = line.rstrip("\r\n")
            if line:
                self._handle_line(line)

    # ------------------------------------------------------------------
    # Parsing RP2040 output lines and updating health
    # ------------------------------------------------------------------

    def _handle_line(self, line: str):
        """
        Handle one complete line from RP2040 and update the health summary.
        """
        ts = time.time()

        self.logger.debug(f"LoRa bridge line: {line}")
        self.health["last_status_line"] = line

        # RX payload line
        if line.startswith("RX:"):
            payload = line[3:]

            # Update health summary for RX
            self.health["rx_count"] += 1
            self.health["last_rx_ts"] = ts
            self.health["last_rx_payload"] = payload

            # If there is already a pending RX without RSSI, push it anyway
            if self._pending_rx is not None:
                self.logger.warning(
                    "Previous RX had no RSSI line; pushing it without link quality."
                )
                try:
                    self.data_bus.queue_put(
                        self.rx_queue,
                        self._pending_rx,
                        drop_oldest_if_full=True,
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to push pending RX into lora.rx_queue: {e}"
                    )

            # Start a new pending RX entry
            self._pending_rx = {
                "timestamp": ts,
                "payload": payload,
                "rssi_dbm": None,
                "snr_db": None,
                "raw_lines": [line],
            }

        # RSSI/SNR line, expected after RX
        elif line.startswith("RSSI:"):
            rssi_dbm, snr_db = self._parse_rssi_snr(line)

            # Update health summary with latest link quality
            self.health["last_rssi_dbm"] = rssi_dbm
            self.health["last_snr_db"] = snr_db

            if self._pending_rx is not None:
                self._pending_rx["rssi_dbm"] = rssi_dbm
                self._pending_rx["snr_db"] = snr_db
                self._pending_rx["raw_lines"].append(line)

                # Push the completed RX entry to the RX queue
                try:
                    self.data_bus.queue_put(
                        self.rx_queue,
                        self._pending_rx,
                        drop_oldest_if_full=True,
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to push RX message into lora.rx_queue: {e}"
                    )

                self._pending_rx = None
            else:
                # RSSI arrived without a matching RX line
                self.logger.warning(
                    "Received RSSI line without pending RX payload."
                )

        # Status / error lines
        elif line in (
            "TXDONE",
            "RXTIMEOUT",
            "RXERROR",
            "TXTIMEOUT",
            "OnTxTimeout",
            "OnRxTimeout",
            "OnRxError",
        ):
            # Treat most of these as errors for health purposes
            if line in (
                "RXERROR",
                "TXTIMEOUT",
                "OnTxTimeout",
                "OnRxTimeout",
                "OnRxError",
                "RXTIMEOUT",
            ):
                self.health["error_count"] += 1
                self.health["last_error_ts"] = ts
                self.health["last_error_type"] = line
                # self.data_bus.log_error(self.name, "ERROR",
                #                         f"LoRa status: {line}")

            # For TXDONE we do not change tx_count here, because it is
            # already updated when we send the command successfully.

        else:
            # Any other debug / info lines from RP2040 are only logged;
            # health["last_status_line"] is already updated above.
            pass

    @staticmethod
    def _parse_rssi_snr(line: str):
        """
        Parse RSSI/SNR line of the form:
            "RSSI:<val> dBm, SNR:<snr_raw>/4 dB"
        Returns (rssi_dbm: Optional[float], snr_db: Optional[float]).
        """
        rssi_dbm = None
        snr_db = None

        try:
            parts = [p.strip() for p in line.split(",")]

            # First part: "RSSI:<val> dBm"
            if parts:
                if parts[0].startswith("RSSI:"):
                    rssi_str = parts[0][len("RSSI:") :].strip()
                    if rssi_str.endswith("dBm"):
                        rssi_str = rssi_str[:-3].strip()
                    rssi_dbm = float(rssi_str)

            # Second part: "SNR:<snr_raw>/4 dB"
            if len(parts) > 1 and "SNR:" in parts[1]:
                snr_part = parts[1]
                idx = snr_part.find("SNR:")
                if idx != -1:
                    snr_str = snr_part[idx + len("SNR:") :].strip()
                    if snr_str.endswith("dB"):
                        snr_str = snr_str[:-2].strip()
                    if "/4" in snr_str:
                        raw_str = snr_str.split("/4", 1)[0].strip()
                        raw_val = float(raw_str)
                        snr_db = raw_val / 4.0
                    else:
                        snr_db = float(snr_str)
        except Exception:
            # If parsing fails, just return None for both
            pass

        return rssi_dbm, snr_db

    # ------------------------------------------------------------------
    # TX towards RP2040
    # ------------------------------------------------------------------

    def _send_one_tx_if_available(self):
        """
        Pop at most one message from lora.tx_queue and send it to RP2040
        in the form "TX:<payload>\\n".
        Also updates the health summary when a TX is attempted.
        """
        if self.ser is None or not self.ser.is_open:
            return

        now = time.time()
        if now - self.last_tx_time < self.min_tx_interval:
            return

        # Non-blocking get from tx_queue
        msg = self.data_bus.queue_get(self.tx_queue, timeout=0)
        if msg is None:
            return

        payload_str = self._encode_payload(msg)
        line = f"TX:{payload_str}\n"
        data = line.encode("utf-8", errors="replace")

        try:
            self.ser.write(data)
            self.ser.flush()
        except (
            OSError,
            serial.SerialTimeoutException,
            serial.SerialException,
        ) as e:
            self.logger.error(f"Serial write error: {e}")
            # Consider this a communication error
            self.health["error_count"] += 1
            self.health["last_error_ts"] = now
            self.health["last_error_type"] = "SERIAL_WRITE_ERROR"
            # self.data_bus.log_error(self.name, "ERROR",
            #                         "LoRa serial write error", str(e))
            raise

        # Update TX-related health
        self.last_tx_time = now
        self.health["tx_count"] += 1
        self.health["last_tx_ts"] = now

        self.logger.debug(f"LoRa TX (to RP2040): {payload_str}")

    @staticmethod
    def _encode_payload(msg) -> str:
        """
        Convert an application-level message into a payload string.
        This will be sent as the <payload> part of "TX:<payload>\\n".

        Rules:
            - If msg is dict and contains key "payload", use that field.
            - Else if msg is bytes, decode as UTF-8 (replace errors).
            - Else convert to str().
            - Newlines are replaced with spaces to avoid breaking the protocol.
        """
        # Extract payload field if dict
        if isinstance(msg, dict) and "payload" in msg:
            payload = msg["payload"]
        else:
            payload = msg

        # Decode bytes to str
        if isinstance(payload, bytes):
            try:
                payload_str = payload.decode("utf-8", errors="replace")
            except Exception:
                payload_str = payload.decode("latin1", errors="replace")
        else:
            payload_str = str(payload)

        # Do not allow embedded newlines; they would break the line protocol
        payload_str = payload_str.replace("\r", " ").replace("\n", " ")

        return payload_str
