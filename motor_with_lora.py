import time
import serial
import RPi.GPIO as GPIO
from typing import Any, Optional

from module_base import BaseModule
from data_bus import DataBus

# ==========================================
# HARDWARE CONFIGURATION
# ==========================================
DIR_PIN  = 22
STEP_PIN = 27
EN_PIN   = 17

# ---- Mechanical + Microstepping Settings ----
STEPS_PER_REV = 200          
MICROSTEPS    = 16           # High resolution for smoothness
STEPS_FULL_TURN = STEPS_PER_REV * MICROSTEPS

# ---- Sweep Parameters ----
SWEEP_ANGLE_TOTAL = 120      
STEPS_TO_SWEEP = int((SWEEP_ANGLE_TOTAL / 360) * STEPS_FULL_TURN)

# ---- Timing ----
STEP_DELAY = 0.005           # 5ms delay per pulse phase
DIR_DELAY  = 0.2             # Pause duration when changing direction

# ---- LoRa Config ----
SERIAL_PORT = '/dev/serial0'
BAUD_RATE   = 9600

class MotorWithLora(BaseModule):
    
    CONFIG_PREFIX = "motor"

    def __init__(self, name: str, data_bus: DataBus, daemon: bool = True, **kwargs: Any):
        super().__init__(name=name, data_bus=data_bus, daemon=daemon, **kwargs)
        
        # --- Serial / LoRa Initialization ---
        self.ser: Optional[serial.Serial] = None
        try:
            self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            self.ser.flush()
            print(f"[{self.name}] [INIT] Serial connected: {SERIAL_PORT}")
        except Exception as e:
            print(f"[{self.name}] [WARN] Serial init failed: {e}")
            self.ser = None

        # --- Motion State ---
        self.current_step_index = 0
        self.scan_direction = 1  # 1: RIGHT, -1: LEFT
        
        # [BATCH PROCESSING]
        # Moves 20 steps at a time to ensure smooth, jitter-free movement.
        self.BATCH_SIZE = 20 

        # --- Deterrent/Pause State ---
        self.last_deter_flag = False
        self.deter_active = False
        self.deter_start_time = 0.0
        self.DETER_DURATION = 2.0  # Pause for 2 seconds upon detection

    def setup(self) -> None:
        """Initialize GPIO pins and calibrate."""
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(DIR_PIN, GPIO.OUT)
        GPIO.setup(STEP_PIN, GPIO.OUT)
        GPIO.setup(EN_PIN, GPIO.OUT)
        
        # Enable Motor (Active LOW)
        GPIO.output(EN_PIN, GPIO.LOW) 
        print(f"[{self.name}] GPIO setup complete.")
        
        self._startup_calibration()

    def teardown(self) -> None:
        """Cleanup resources."""
        GPIO.output(EN_PIN, GPIO.HIGH) # Disable motor
        GPIO.cleanup()
        if self.ser:
            self.ser.close()
        print(f"[{self.name}] Cleanup complete.")

    def _startup_calibration(self):
        """
        Performs a small 'wiggle' at startup.
        """
        print(f"[{self.name}] [INIT] Calibration (Wiggle)...")
        self._manual_move(steps=50, direction=1)
        time.sleep(0.2)
        self._manual_move(steps=50, direction=-1)
        time.sleep(0.5)
        print(f"[{self.name}] [INIT] Calibration OK.")

    def _manual_move(self, steps: int, direction: int):
        """Blocking helper for calibration only."""
        pin_lvl = GPIO.HIGH if direction == 1 else GPIO.LOW
        GPIO.output(DIR_PIN, pin_lvl)
        
        for _ in range(steps):
            GPIO.output(STEP_PIN, GPIO.HIGH)
            time.sleep(0.002) 
            GPIO.output(STEP_PIN, GPIO.LOW)
            time.sleep(0.002)

    def _calculate_angle(self) -> float:
        """Helper to get current angle based on step count."""
        progress = self.current_step_index / STEPS_TO_SWEEP
        return progress * SWEEP_ANGLE_TOTAL

    def _send_lora_alert(self, angle: float):
        """Sends the alert via LoRa with robust buffer handling."""
        if self.ser and self.ser.is_open:
            msg = f"LOUP_ANGLE:{angle:.1f}\n"
            try:
                self.ser.reset_input_buffer() 
                self.ser.write(msg.encode('utf-8'))
                self.ser.flush() 
                print(f"[{self.name}] >>> [LORA SENT] {msg.strip()} <<<")
            except Exception as e:
                print(f"[{self.name}] [LORA FAIL] {e}")

    def _update_bus_status(self):
        """
        Updates DataBus with current status once per batch.
        """
        state = {
            "angle": round(self._calculate_angle(), 1),
            "direction": "RIGHT" if self.scan_direction == 1 else "LEFT",
            "is_moving": not self.deter_active,
            "timestamp": time.time()
        }
        self.data_bus.set_state("motor_location", state)

    def step(self):
        """
        Main logic loop unit.
        """
        
        # --- 1. Deterrent / Pause Logic ---
        if self.deter_active:
            # Check if pause duration is over
            if time.time() - self.deter_start_time >= self.DETER_DURATION:
                print(f"[{self.name}] Resume scanning...")
                self.deter_active = False
            else:
                # Still paused
                time.sleep(0.05)
                return 

        # --- 2. Check for Trigger (Rising Edge) ---
        if not self.deter_active:
            current_deter_flag = self.data_bus.get_state("deter_flag", default=False)
            
            # Check for rising edge (False -> True)
            if current_deter_flag and not self.last_deter_flag:
                angle = self._calculate_angle()
                print(f"[{self.name}] !!! ALERT: Deter flag detected at {angle:.1f}° !!!")
                
                # A. Send LoRa Alert
                self._send_lora_alert(angle)
                
                # [CHANGE] Direction reversal removed. 
                # Motor will continue in current direction after pause.

                # B. Start Pause Timer
                self.deter_active = True
                self.deter_start_time = time.time()
                
                self.last_deter_flag = current_deter_flag
                return 

            self.last_deter_flag = current_deter_flag

        # --- 3. Motion Logic (BATCH PROCESSING) ---
        
        target_dir_pin = GPIO.HIGH if self.scan_direction == 1 else GPIO.LOW
        GPIO.output(DIR_PIN, target_dir_pin)
        
        # Inner loop: Execute BATCH_SIZE steps in one go for smoothness
        for _ in range(self.BATCH_SIZE):
            
            # 1. Generate Pulse
            GPIO.output(STEP_PIN, GPIO.HIGH)
            time.sleep(STEP_DELAY)
            GPIO.output(STEP_PIN, GPIO.LOW)
            time.sleep(STEP_DELAY)
            
            # 2. Update Position Index
            if self.scan_direction == 1:
                self.current_step_index += 1
                # Check Right Boundary
                if self.current_step_index >= STEPS_TO_SWEEP:
                    self.scan_direction = -1
                    time.sleep(DIR_DELAY)
                    break 
            else:
                self.current_step_index -= 1
                # Check Left Boundary
                if self.current_step_index <= 0:
                    self.scan_direction = 1
                    time.sleep(DIR_DELAY)
                    break 

        # Update DataBus status once per batch
        self._update_bus_status()
        
        if self.should_stop():
            return