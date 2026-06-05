import sys, os  
import logging
import glob
import asyncio
import threading
import random
import csv
import re
import json
import struct
from concurrent.futures import ThreadPoolExecutor
from functools import reduce
from collections import deque

from datetime import datetime

# Optionally import numpy for fast array operations in plotting
try:
    import numpy as np
    HAVE_NP = True
except Exception:
    HAVE_NP = False

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt
MIME_MOVESENSE = "application/x-movesense"

# Custom GATT UUIDs (sensor firmware)
WRITE_CHARACTERISTIC_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"
NOTIFY_CHARACTERISTIC_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"
PACKET_TYPE_DATA = 2
PACKET_TYPE_DATA_PART2 = 3

# Optional plotting (falls back gracefully if not installed yet)
try:
    import pyqtgraph as pg
    HAVE_PG = True
    logging.info("pyqtgraph detected: live plotting ENABLED")
except Exception as e:
    HAVE_PG = False
    logging.warning(f"pyqtgraph not available ({e}); live plotting DISABLED — install with: pip install pyqtgraph")

if sys.platform.startswith("win"):
    try:
        from platform_support import windows as plat_win
        plat_win.init()
    except Exception:
        # stay silent if platform module is missing in dev
        pass

def resource_path(relpath: str) -> str:
    """Get absolute path to resource, works in dev and PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, relpath)

# Import SENSOR_LIST and the asynchronous extraction function.
from extraction.extractor import extract_sensor, send_stop_logging

# Import the conversion function.
import conversion.converter as conv


# --- ScannerThread definition (self-contained) ---
from bleak import discover, BleakClient, BleakScanner
async def _reset_sensor(end_of_serial: str):
    """Discover and send STOP_LOGGING to the sensor matching end_of_serial."""
    devices = await discover()
    for d in devices:
        if d.name and d.name.endswith(end_of_serial):
            async with BleakClient(d.address) as client:
                await send_stop_logging(client)
            return
    logging.error(f"Sensor {end_of_serial} not found for reset.")

class ScannerThread(QtCore.QThread):
    devicesFound = QtCore.pyqtSignal(list)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while self._running:
            try:
                devices = loop.run_until_complete(discover())
                # Filter for Movesense devices (adjust filter if needed)
                movesense_devices = [d for d in devices if d.name and d.name.startswith("Movesense")]
                self.devicesFound.emit(movesense_devices)
            except Exception as e:
                print("Error during scanning:", e)
            self.msleep(1000)

    def stop(self):
        self._running = False

# --- Minimal binary helpers (based on your sample) ---
class DataView:
    def __init__(self, array, bytes_per_element=1):
        self.array = array
        self.bytes_per_element = 1
    def __get_binary(self, start_index, byte_count, signed=False):
        integers = [self.array[start_index + x] for x in range(byte_count)]
        _bytes = [integer.to_bytes(self.bytes_per_element, byteorder='little', signed=signed) for integer in integers]
        return reduce(lambda a, b: a + b, _bytes)
    def get_uint_16(self, start_index):
        return int.from_bytes(self.__get_binary(start_index, 2), byteorder='little')
    def get_uint_8(self, start_index):
        return int.from_bytes(self.__get_binary(start_index, 1), byteorder='little')
    def get_uint_32(self, start_index):
        binary = self.__get_binary(start_index, 4)
        return struct.unpack('<I', binary)[0]
    def get_int_32(self, start_index):
        binary = self.__get_binary(start_index, 4)
        return struct.unpack('<i', binary)[0]

class ECGSignals(QtCore.QObject):
    status = QtCore.pyqtSignal(str, str)     # last6, message
    subscribed = QtCore.pyqtSignal(str, int) # last6, sample_rate
    samples = QtCore.pyqtSignal(str, int)    # last6, count_increment
    ecg = QtCore.pyqtSignal(str, object)     # last6, list[float] (mV)

class MovesenseECGSession(QtCore.QObject):
    """One live ECG subscription session for a sensor (last6). Connects and subscribes at 200 Hz."""
    def __init__(self, last6: str, parent=None, addr: str = ""):
        super().__init__(parent)
        self.last6 = last6
        self.sample_rate = 200
        self.signals = ECGSignals()
        self._loop = None
        self._thread = None
        self._stop_event = None
        self._client = None
        self._addr = addr or None
        self._pkt_counter = 0
        self._first_dump_done = False
        self._stopping = False
        self._watchdog_task = None

    # ---- lifecycle ----
    def start(self):
        if self._thread:
            return
        self._thread = QtCore.QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run)
        self._thread.start()

    def stop(self):
        if not self._thread:
            return
        self._stopping = True
        # Try to force unsubscribe & disconnect immediately on the session loop
        if self._loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._force_unsubscribe_disconnect(), self._loop)
                try:
                    fut.result(timeout=1.5)
                except Exception:
                    pass
            except Exception:
                pass
        # Cancel watchdog if any
        try:
            if self._watchdog_task:
                self._watchdog_task.cancel()
        except Exception:
            pass
        # Finally signal the main stop event to unwind the thread
        QtCore.QMetaObject.invokeMethod(self, "_request_stop", Qt.QueuedConnection)
    async def _force_unsubscribe_disconnect(self):
        try:
            if self._client and self._client.is_connected:
                # Send UNSUBSCRIBE for ref=100 (ECG)
                try:
                    await self._client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([2, 100]), response=True)
                except Exception:
                    pass
                # Stop notify
                try:
                    await self._client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
                except Exception:
                    pass
                # Fully disconnect
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
        except Exception:
            pass

    @QtCore.pyqtSlot()
    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()

        async def runner():
            try:
                await self._connect_and_subscribe()
                await self._stop_event.wait()
            finally:
                await self._cleanup()

        try:
            self._loop.run_until_complete(runner())
        finally:
            try:
                self._loop.stop(); self._loop.close()
            except Exception:
                pass
            self._thread.quit(); self._thread.wait()
            self._thread = None
            self._loop = None
            self._stop_event = None

    @QtCore.pyqtSlot()
    def _request_stop(self):
        if self._loop and self._stop_event and not self._stop_event.is_set():
            self._loop.call_soon_threadsafe(self._stop_event.set)

    # ---- core ----
    async def _connect_and_subscribe(self):
        if not self._addr:
            self._addr = await self._discover_addr(self.last6)
        if not self._addr:
            self.signals.status.emit(self.last6, f"Sensor ...{self.last6} not found")
            return
        self.signals.status.emit(self.last6, f"Connecting to {self._addr} …")
        self._client = BleakClient(self._addr, disconnected_callback=self._on_disconnect)
        try:
            await self._client.__aenter__()
            if not self._client.is_connected:
                self.signals.status.emit(self.last6, "Failed to connect")
                return
            self.signals.status.emit(self.last6, "Connected; enabling notifications")
            await self._client.start_notify(NOTIFY_CHARACTERISTIC_UUID, self._on_notify)

            # Subscribe directly to the float/mV ECG path; the parser supports float32 too.
            ref = 100
            path = "/Meas/ECG/200/mV"
            try:
                payload = bytearray([1, ref]) + bytearray(path, "utf-8")
                await self._client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, payload, response=True)
                self.signals.subscribed.emit(self.last6, 200)
                self.signals.status.emit(self.last6, f"Subscribed ECG 200 Hz {path}")
            except Exception as e:
                self.signals.status.emit(self.last6, f"Subscribe failed: {e}")
                logging.error(f"[ECG {self.last6}] subscribe to {path} failed: {e}")
                return
        except Exception as e:
            logging.error(f"[ECG {self.last6}] setup error: {e}")
            self.signals.status.emit(self.last6, f"Error: {e}")

    async def _cleanup(self):
        # Cancel watchdog
        try:
            if self._watchdog_task:
                self._watchdog_task.cancel()
                self._watchdog_task = None
        except Exception:
            pass
        try:
            if self._client and self._client.is_connected:
                try:
                    await self._client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([2, 100]), response=True)
                except Exception:
                    pass
                try:
                    await self._client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
                except Exception:
                    pass
            if self._client:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
        finally:
            self._client = None

    def _on_disconnect(self, _client):
        self._stopping = True
        self.signals.status.emit(self.last6, "Disconnected")
        if self._loop and self._stop_event and not self._stop_event.is_set():
            self._loop.call_soon_threadsafe(self._stop_event.set)

    # ---- notifications ----
    def _on_notify(self, _sender: int, data: bytearray):
        try:
            if self._stopping:
                return
            self._pkt_counter += 1
            if not self._first_dump_done:
                try:
                    preview = " ".join(f"{b:02X}" for b in data[:16])
                    logging.info(f"[ECG {self.last6}] first notify len={len(data)} bytes: {preview}")
                except Exception:
                    pass
                self._first_dump_done = True
            values = []
            d = DataView(data)
            parsed = False
            # Attempt RAW format: [2,type_ref][4B timestamp][16×int32]
            try:
                base = 6
                vals = [d.get_int_32(base + i*4) * 0.00038 for i in range(16)]  # mV (0.38 µV/LSB)
                vmax = max(abs(v) for v in vals)
                if vmax < 50_000:  # if we accidentally parsed floats as ints, this would explode
                    values = vals
                    parsed = True
            except Exception:
                parsed = False
            # If RAW failed or looks insane, try float32 array (no timestamp): [2,type_ref][16×float32 mV]
            if not parsed:
                try:
                    base = 2
                    vals = [struct.unpack_from('<f', bytes(d.array), base + i*4)[0] for i in range(16)]
                    values = vals
                    parsed = True
                except Exception:
                    parsed = False
            if not parsed:
                self.signals.samples.emit(self.last6, 16)
                return
            self.signals.samples.emit(self.last6, 16)
            self.signals.ecg.emit(self.last6, values)
        except Exception as e:
            logging.debug(f"[ECG {self.last6}] parse error: {e}")

    # ---- helpers ----
    async def _discover_addr(self, last6: str):
        try:
            devices = await BleakScanner.discover()
        except Exception:
            devices = await discover()
        for d in devices:
            name = (d.name or "").strip()
            if not name:
                continue
            # Match last6 anywhere in the advertised name
            if self.last6 in name:
                return d.address
            m = re.findall(r"(\d{6,})", name)
            if m and m[-1][-6:] == last6:
                return d.address
        return None

class DiscoveredList(QtWidgets.QListWidget):
    """QListWidget that supports dragging a Movesense device item with custom MIME payload."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if not item:
            return
        data = item.data(Qt.UserRole) or {}
        payload = json.dumps({
            "name": data.get("name", item.text()),
            "address": data.get("address", ""),
            "last6": data.get("last6", "")
        }).encode("utf-8")
        mime = QtCore.QMimeData()
        mime.setData(MIME_MOVESENSE, payload)

        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.MoveAction)

class SensorTile(QtWidgets.QWidget):
    sensorDropped = QtCore.pyqtSignal(str, str, int)  # last6, address, tile_index
    stopRequested = QtCore.pyqtSignal(str, int)   # last6, tile_index
    clearRequested = QtCore.pyqtSignal(str, int)  # last6, tile_index
    """One tile: participant name, info line, and an ECG plot area."""
    def __init__(self, sensor_last6: str, participant_name: str = "", parent=None, tile_index: int = -1):
        super().__init__(parent)
        self.sensor_last6 = sensor_last6
        self.participant_name = participant_name or sensor_last6
        self.tile_index = tile_index
        self.assigned_last6 = None  # None = empty placeholder
        self.setAcceptDrops(True)

        self.setMinimumHeight(140)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Top: big participant label
        self.name_label = QtWidgets.QLabel(self.participant_name)
        font = self.name_label.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.name_label.setFont(font)
        self.name_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.name_label)

        # Info line: sensor id & simplified info (no Batt/RSSI placeholders)
        self.info_label = QtWidgets.QLabel(f"Sensor {self.sensor_last6} · ECG 200 Hz · Samples 0")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet("color: #555;")
        outer.addWidget(self.info_label)

        # Start as empty placeholder until assigned via DnD
        self.set_placeholder(True)

        # Plot area (or a placeholder frame if pyqtgraph missing)
        if HAVE_PG:
            self.plot = pg.PlotWidget()
            self.plot.setBackground('w')  # solid white to avoid transparent theme issues
            self.plot.setDownsampling(auto=True, mode='peak')
            self.plot.setClipToView(True)
            # We'll control ranges manually in _refresh_plots
            self.curve = self.plot.plot([], pen=pg.mkPen('k', width=1))  # black line
            self.plot.setMenuEnabled(False)
            self.plot.hideButtons()
            self.plot.setMouseEnabled(x=False, y=False)
            self.plot.showGrid(x=True, y=True, alpha=0.2)
            # Fix y-axis range for ECG signal (mV)
            self.plot.setYRange(-2, 2)
            self.plot.disableAutoRange('y')
            outer.addWidget(self.plot, 1)
        else:
            placeholder = QtWidgets.QFrame()
            placeholder.setFrameShape(QtWidgets.QFrame.StyledPanel)
            placeholder.setStyleSheet("background: #f4f4f4; border: 1px solid #ddd;")
            ph_layout = QtWidgets.QVBoxLayout(placeholder)
            msg = QtWidgets.QLabel("Live plot disabled — install pyqtgraph:\n`pip install pyqtgraph`")
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet("color:#666;")
            ph_layout.addWidget(msg)
            outer.addWidget(placeholder, 1)

    def set_participant_name(self, name: str):
        self.participant_name = name or self.sensor_last6
        self.name_label.setText(self.participant_name)

    def set_info(self, batt: str = "--", rssi: str = "--", drops: str = "0.0"):
        self.info_label.setText(
            f"Sensor {self.sensor_last6} · ECG 125 Hz · Batt {batt}% · RSSI {rssi} dBm · Drops {drops}%"
        )

    # For later: fast update of ECG trace
    def set_trace(self, y):
        if HAVE_PG:
            self.curve.setData(y)

    def set_placeholder(self, is_placeholder: bool):
        """Switch visual between empty placeholder and active tile."""
        if is_placeholder:
            self.assigned_last6 = None
            self.name_label.setText("Drop sensor here")
            self.name_label.setStyleSheet("color:#777;")
            self.info_label.setText("No sensor assigned")
        else:
            self.name_label.setStyleSheet("")

    def assign_sensor(self, last6: str, display_name: str):
        self.assigned_last6 = last6
        self.set_placeholder(False)
        self.participant_name = display_name or last6
        self.name_label.setText(self.participant_name)
        self.info_label.setText(f"Sensor {last6} · ECG -- Hz · Batt --% · RSSI -- dBm · Drops 0.0%")

    # --- Drag & Drop handlers ---
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasFormat(MIME_MOVESENSE):
            event.acceptProposedAction()
            self.setStyleSheet("border: 2px dashed #4a90e2;")
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if event.mimeData().hasFormat(MIME_MOVESENSE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent):
        self.setStyleSheet("")

    def dropEvent(self, event: QtGui.QDropEvent):
        self.setStyleSheet("")
        if not event.mimeData().hasFormat(MIME_MOVESENSE):
            event.ignore()
            return
        try:
            data = json.loads(bytes(event.mimeData().data(MIME_MOVESENSE)).decode("utf-8"))
            last6 = (data.get("last6") or "").strip()
            address = data.get("address", "")
            if not last6:
                name = data.get("name", "")
                m = re.findall(r"(\d{6,})", name)
                if m:
                    last6 = m[-1][-6:]
            if not last6:
                QtWidgets.QToolTip.showText(event.pos(), "Not a Movesense sensor", self)
                event.ignore()
                return
            self.sensorDropped.emit(last6, address, self.tile_index)
            event.acceptProposedAction()
        except Exception:
            event.ignore()

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        menu = QtWidgets.QMenu(self)
        has_sensor = bool(self.assigned_last6)

        act_stop = menu.addAction("Stop live (unsubscribe)")
        act_stop.setEnabled(has_sensor)
        act_clear = menu.addAction("Clear tile")
        act_clear.setEnabled(has_sensor)

        chosen = menu.exec_(event.globalPos())
        if chosen is None:
            return
        if chosen is act_stop and has_sensor:
            # Ask main window to stop the session but keep assignment visible
            self.stopRequested.emit(self.assigned_last6, self.tile_index)
        elif chosen is act_clear and has_sensor:
            # Ask main window to clear this tile entirely (also stops session)
            self.clearRequested.emit(self.assigned_last6, self.tile_index)

    def mark_stopped(self):
        if self.assigned_last6:
            self.info_label.setText(f"Sensor {self.assigned_last6} · Stopped")

# --- FlagHandler remains unchanged ---
class FlagHandler(logging.Handler):
    def __init__(self, flag_container):
        super().__init__()
        self.flag_container = flag_container
        self.setFormatter(logging.Formatter("%(message)s"))
    
    def emit(self, record):
        msg = self.format(record)
        if "Sending FETCH_LOG command for log" in msg:
            self.flag_container['log_attempt'] = True

class ExtractionThread(QtCore.QThread):
    # Signal sends sensor_index (int), final extraction result (bool), and log_attempt_sent (bool)
    extractionStarted = QtCore.pyqtSignal(int)
    extractionResult = QtCore.pyqtSignal(int, bool, bool)

    def __init__(self, sensor_list, raw_folder, conv_folder, found_sensor_ids, parent=None, sensor_map=None, day_number=None):
        super().__init__(parent)
        self.sensor_list = sensor_list[:]  
        self.raw_folder = raw_folder
        self.conv_folder = conv_folder
        self.found_sensor_ids = found_sensor_ids  

        self.completed = [False] * len(self.sensor_list)
        self.busy = set()
        self.selection_lock = asyncio.Lock()

        # Concurrency for extraction tasks
        self.concurrency_limit = 4

        # NEW: mapping and day inputs
        self.sensor_map = sensor_map or {}
        self.day_number = day_number or 1
        # NEW: global conversion lock for serialization
        self.convert_lock = asyncio.Lock()

    def build_target_stem(self, sensor_last6):
        """Return base filename without extension, e.g., PID_040625_3"""
        pid = self.sensor_map.get(sensor_last6)
        if not pid:
            logging.debug(f"[target] no pid for last6={sensor_last6}")
            return ""
        date_str = datetime.now().strftime("%d%m%y")
        day_str = str(self.day_number or 1)
        return f"{pid}_{date_str}_{day_str}"

    def build_target_name(self, sensor_last6):
        stem = self.build_target_stem(sensor_last6)
        target = f"{stem}.csv" if stem else ""
        logging.debug(f"[target] {sensor_last6} -> {target}")
        return target

    def _safe_rename(self, src: str, dst: str) -> str:
        base, ext = os.path.splitext(dst)
        candidate = dst
        i = 1
        while os.path.exists(candidate):
            candidate = f"{base}_{i}{ext}"
            i += 1
        logging.debug(f"[os.replace] {src} -> {candidate}")
        os.replace(src, candidate)
        return candidate
    
    def run(self):
        # Run the async extraction loop in a new event loop
        asyncio.run(self.run_extraction())

    async def run_extraction(self):
        # Create a semaphore to limit concurrent extraction attempts.
        semaphore = asyncio.Semaphore(self.concurrency_limit)
        # A list of pending sensor indices (initially, all sensors are pending).
        pending = set(i for i in range(len(self.sensor_list)))
        # We'll use a thread pool executor for the synchronous conversion.
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=4)
        
        async def worker():
            # Each worker runs until no pending sensor remains.
            last_sensor = None
            nonlocal pending
            while True:
                sensor_index = None
                async with self.selection_lock:
                    # Build eligible list: sensor not completed, not busy, and currently discovered.
                    eligible = [i for i in range(len(self.sensor_list))
                                if not self.completed[i] 
                                and i not in self.busy 
                                and (self.sensor_list[i] in self.found_sensor_ids)]
                    # If more than one sensor is eligible, exclude the sensor we just tried.
                    if last_sensor is not None and len(eligible) > 1 and last_sensor in eligible:
                        eligible.remove(last_sensor)
                    if eligible:
                        sensor_index = random.choice(eligible)
                        self.busy.add(sensor_index)
                if sensor_index is None:
                    async with self.selection_lock:
                        if not pending:
                            break
                    await asyncio.sleep(1)
                    continue

                last_sensor = sensor_index  # Remember the sensor we are about to process.

                # Emit a signal indicating that extraction is starting for this sensor.
                self.extractionStarted.emit(sensor_index)

                # Process the chosen sensor.
                sensor_id = self.sensor_list[sensor_index]
                logging.info(f"Worker starting extraction for sensor {sensor_id}")
                attempt = 1  # one attempt per worker cycle
                sensor_extracted = False
                log_attempt_sent = False

                if sensor_id not in self.found_sensor_ids:
                    logging.info(f"Sensor {sensor_id} not found at extraction time.")
                    self.extractionResult.emit(sensor_index, False, False)
                    async with self.selection_lock:
                        self.busy.discard(sensor_index)
                    await asyncio.sleep(1)
                    continue

                async with semaphore:
                    logging.info(f"Attempt {attempt} for sensor {sensor_id}")
                    flag_container = {'log_attempt': False}
                    flag_handler = FlagHandler(flag_container)
                    logger = logging.getLogger()
                    logger.addHandler(flag_handler)
                    try:
                        result = await extract_sensor(sensor_id, self.raw_folder)
                        extraction_success = result  # expecting Boolean result.
                    except Exception as e:
                        logging.error(f"Extraction failed for sensor {sensor_id}: {e}")
                        extraction_success = False
                    finally:
                        logger.removeHandler(flag_handler)
                    log_attempt_sent = flag_container['log_attempt']

                    if extraction_success:
                        sensor_extracted = True
                        logging.info(f"Extraction succeeded for sensor {sensor_id}")
                    else:
                        logging.info(f"Extraction failed for sensor {sensor_id}")

                if sensor_extracted:
                    pattern = os.path.join(self.raw_folder, f"*{sensor_id}*.sbem")
                    matching_files = glob.glob(pattern)
                    if matching_files:
                        for file_path in matching_files:
                            logging.info(f"Converting file {file_path} for sensor {sensor_id}...")
                            # Ensure only one conversion happens at a time across all workers
                            async with self.convert_lock:
                                try:
                                    csv_path = await loop.run_in_executor(executor, conv.convert_sbem, file_path, self.conv_folder)
                                    logging.debug(f"[convert] converter returned: {csv_path} for {file_path}")

                                    if not csv_path or not os.path.exists(csv_path):
                                        logging.warning(f"[skip] No CSV produced for {file_path}; skipping rename.")
                                        # proceed to next extracted file without failing the sensor run
                                        continue

                                    # Use the known last6 sensor_id from this extraction
                                    target_name = self.build_target_name(sensor_id)
                                    logging.debug(f"[rename] target_name={target_name} for sensor_id={sensor_id}")
                                    if not target_name:
                                        logging.info(f"[keep] No mapping for {sensor_id}; keeping {csv_path}")
                                    else:
                                        target_path = os.path.join(self.conv_folder, target_name)
                                        logging.debug(f"[rename] {csv_path} -> {target_path}")
                                        final_path = self._safe_rename(csv_path, target_path)
                                        logging.info(f"[ok] Converted and renamed CSV to {final_path}")

                                    # --- NEW: also rename the SBEM in the raw folder to the same stem ---
                                    target_stem = self.build_target_stem(sensor_id)
                                    if target_stem:
                                        sbem_target = os.path.join(self.raw_folder, f"{target_stem}.sbem")
                                        try:
                                            final_sbem = self._safe_rename(file_path, sbem_target)
                                            logging.info(f"[ok] Raw SBEM renamed to {final_sbem}")
                                        except Exception as e:
                                            logging.error(f"[rename] Failed to rename SBEM {file_path} → {sbem_target}: {e}")
                                    else:
                                        logging.info(f"[keep] No mapping for {sensor_id}; keeping SBEM name {file_path}")
                                except Exception as e:
                                    logging.error(f"Conversion failed for {file_path}: {e}")
                    else:
                        logging.info(f"No extracted log files found for sensor {sensor_id}.")

                    self.extractionResult.emit(sensor_index, True, log_attempt_sent)
                    async with self.selection_lock:
                        self.completed[sensor_index] = True
                        pending.discard(sensor_index)
                        self.busy.discard(sensor_index)
                else:
                    self.extractionResult.emit(sensor_index, False, log_attempt_sent)
                    async with self.selection_lock:
                        self.busy.discard(sensor_index)
                await asyncio.sleep(0.5)
  
        # Launch a group of worker tasks.
        workers = [asyncio.create_task(worker()) for _ in range(self.concurrency_limit)]
        # Wait until all workers complete.
        await asyncio.gather(*workers)
        executor.shutdown()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        # Set the application icon
        if sys.platform.startswith("win"):
            icon_file = resource_path("icons/app.ico")
        elif sys.platform == "darwin":
            icon_file = resource_path("icons/app.icns")
        else:
            icon_file = resource_path("icons/my_icon.png")  # fallback (Linux/dev)
        self.setWindowIcon(QtGui.QIcon(icon_file))
        self.setWindowTitle("Movesense Data Tool")
        self.setGeometry(100, 100, 800, 600)
        self.found_sensor_ids = []
        # Mapping from sensor_last6 -> display label (NAME or PID) used in UI
        self.sensor_map = {}
        # Separate mapping from sensor_last6 -> PID (2nd column) used only for filenames
        self.sensor_pid_map = {}
        self.day_number = None
        self.sensor_list = []  # dynamic list from CSV (last 6 digits)
        # DnD selection state
        self.selected_slots = {}  # tile_index -> last6
        self.last6_to_tile = {}   # last6 -> tile_index
        # Live ECG sessions keyed by last6
        self.live_sessions = {}
        self.live_sample_counts = {}
        # Per-sensor auto-restart helpers for the "16 samples then stuck" behaviour
        self.live_restart_timers = {}
        self.live_auto_restart_done = {}
        # Fast lookup for tiles by index
        self.tile_index_map = {}
        # Per-sensor ring buffers for plotting (≈10s @ 200 Hz)
        self.live_buffers = {}
        self.max_buffer = 2000

        # Plot refresh timer (~20 FPS)
        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.timeout.connect(self._refresh_plots)
        self.plot_timer.start(50)
        
        self._setup_ui()
        self._create_menu()  # Create menu including About
        self._start_scanner()
        self.statusBar().showMessage("Software signed by Jonathan Posthuma, Radboud University")
        
        # Timers will be created dynamically after loading CSV

    def _setup_ui(self):
        # Central widget with a horizontal splitter
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        root = QtWidgets.QHBoxLayout(central_widget)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        splitter = QtWidgets.QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter)

        # ---------------- Left panel (existing extractor UI) ----------------
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.setSpacing(6)

        # --- Discovered Sensors (live) ---
        discovered_label = QtWidgets.QLabel("Discovered Sensors (live):")
        discovered_label.setStyleSheet("color:#333;")
        self.discovered_list = DiscoveredList()
        self.discovered_list.setUniformItemSizes(True)
        self.discovered_list.itemDoubleClicked.connect(self.on_discovered_double_clicked)

        left_layout.addWidget(discovered_label)
        left_layout.addWidget(self.discovered_list)

        # --- Sensor Extraction Status (1 sensor per row, small font) ---
        num_sensors = len(self.sensor_list)
        self.sensor_table = QtWidgets.QTableWidget(num_sensors, 2)
        self.sensor_table.horizontalHeader().setVisible(False)
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_table.setAlternatingRowColors(True)

        small_font = self.font()
        small_font.setPointSize(max(8, small_font.pointSize() - 2))
        self.sensor_table.setFont(small_font)

        self.sensor_entries = []
        for i, sensor_name in enumerate(self.sensor_list):
            display_name = sensor_name
            if self.sensor_map.get(sensor_name):
                display_name = f"{sensor_name} ({self.sensor_map[sensor_name]})"
            name_item = QtWidgets.QTableWidgetItem(display_name)
            status_item = QtWidgets.QTableWidgetItem("Pending")
            name_item.setTextAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            status_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.sensor_table.setItem(i, 0, name_item)
            self.sensor_table.setItem(i, 1, status_item)
            self.sensor_entries.append((name_item, status_item))

        header = self.sensor_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Fixed)
        self.sensor_table.setColumnWidth(1, 90)
        self.sensor_table.setWordWrap(False)
        # Compact rows and ensure full height for all sensors
        vh = self.sensor_table.verticalHeader()
        vh.setDefaultSectionSize(20)
        self.sensor_table.setMinimumHeight(
            vh.defaultSectionSize() * max(1, num_sensors)
            + self.sensor_table.horizontalHeader().height()
            + 6
        )

        left_layout.addWidget(QtWidgets.QLabel("Sensor Extraction Status:"))
        left_layout.addWidget(self.sensor_table)

        # --- Control Buttons Section ---
        button_layout = QtWidgets.QHBoxLayout()
        self.extract_button = QtWidgets.QPushButton("Extract Data")
        self.extract_button.clicked.connect(self.on_extract)
        button_layout.addWidget(self.extract_button)
        self.convert_button = QtWidgets.QPushButton("Convert Data")
        self.convert_button.clicked.connect(self.on_convert)
        button_layout.addWidget(self.convert_button)
        left_layout.addLayout(button_layout)

        # --- MODE Toggle Button Section ---
        mode_layout = QtWidgets.QHBoxLayout()
        self.mode_toggle = QtWidgets.QPushButton("Mode: Extract")
        self.mode_toggle.setCheckable(True)
        self.mode_toggle.clicked.connect(self.toggle_mode)
        mode_layout.addWidget(self.mode_toggle)
        left_layout.addLayout(mode_layout)
        self.mode = "Extract"

        # --- Settings: folders & mapping ---
        raw_layout = QtWidgets.QHBoxLayout()
        self.raw_output_edit = QtWidgets.QLineEdit()
        self.raw_output_edit.setPlaceholderText("Raw logs output folder")
        raw_browse_button = QtWidgets.QPushButton("Browse")
        raw_browse_button.clicked.connect(self.select_raw_folder)
        raw_layout.addWidget(self.raw_output_edit)
        raw_layout.addWidget(raw_browse_button)
        left_layout.addLayout(raw_layout)

        csv_layout = QtWidgets.QHBoxLayout()
        self.csv_output_edit = QtWidgets.QLineEdit()
        self.csv_output_edit.setPlaceholderText("CSV output folder")
        csv_browse_button = QtWidgets.QPushButton("Browse")
        csv_browse_button.clicked.connect(self.select_csv_folder)
        csv_layout.addWidget(self.csv_output_edit)
        csv_layout.addWidget(csv_browse_button)
        left_layout.addLayout(csv_layout)

        mapping_layout = QtWidgets.QHBoxLayout()
        self.mapping_label = QtWidgets.QLabel("Mapping: not loaded")
        load_mapping_button = QtWidgets.QPushButton("Load Sensor→Participant CSV")
        load_mapping_button.clicked.connect(self.load_mapping_csv)
        mapping_layout.addWidget(self.mapping_label)
        mapping_layout.addWidget(load_mapping_button)
        left_layout.addLayout(mapping_layout)

        # --- Log/Status Output Area ---
        self.status_text = QtWidgets.QTextEdit()
        self.status_text.setReadOnly(True)
        left_layout.addWidget(self.status_text)

        splitter.addWidget(left)

        # ---------------- Right panel (Live QA tiles) ----------------
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 6, 6)
        right_layout.setSpacing(6)

        title = QtWidgets.QLabel("Live QA — ECG Preview")
        tfont = title.font(); tfont.setBold(True)
        title.setFont(tfont)
        sfont = title.font()
        sfont.setPointSize(max(9, sfont.pointSize()))
        title.setFont(sfont)
        title.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(title)

        self.qa_grid = QtWidgets.QGridLayout()
        self.qa_grid.setSpacing(6)
        right_layout.addLayout(self.qa_grid, 1)

        # Build tiles now (will refresh when mapping loads)
        self.tiles = {}  # last6 -> SensorTile
        self.rebuild_qa_tiles()

        splitter.addWidget(right)

        # Give left more width for tables; right will hold 4×4 plots
        splitter.setSizes([int(self.width() * 0.62), int(self.width() * 0.38)])
    def on_sensor_dropped(self, last6: str, address: str, tile_index: int):
        """Handle a sensor being dropped onto a tile: assign or swap as needed."""
        # Find the target tile widget by index
        target_tile = None
        for t in self.tiles.values():
            if t.tile_index == tile_index:
                target_tile = t
                break
        if target_tile is None:
            return

        display_name = self.sensor_map.get(last6, last6)

        # If tile occupied by another sensor, handle swap/move
        current_on_target = self.selected_slots.get(tile_index)
        old_tile = self.last6_to_tile.get(last6)

        if current_on_target and current_on_target != last6:
            if old_tile is not None:
                # swap: move existing target sensor to old_tile
                self.selected_slots[old_tile] = current_on_target
                self.last6_to_tile[current_on_target] = old_tile
                # update old_tile UI
                for tile in self.tiles.values():
                    if tile.tile_index == old_tile:
                        tile.assign_sensor(current_on_target, self.sensor_map.get(current_on_target, current_on_target))
                        break
            else:
                # unassign existing sensor on target
                self.last6_to_tile.pop(current_on_target, None)

        # Assign dropped sensor to target tile
        self.selected_slots[tile_index] = last6
        self.last6_to_tile[last6] = tile_index
        target_tile.assign_sensor(last6, display_name)
        self._ensure_session_running(last6, address)

    def clear_tile_assignment(self, tile_index: int):
        """Unassign a tile (placeholder only)."""
        last6 = self.selected_slots.pop(tile_index, None)
        if last6:
            self.last6_to_tile.pop(last6, None)
            self._stop_session(last6)
            self.live_buffers.pop(last6, None)
        for t in self.tiles.values():
            if t.tile_index == tile_index:
                t.set_placeholder(True)
                break


    def _ensure_session_running(self, last6: str, address: str = ""):
        """Create and start a live ECG session for the given sensor if not already running."""
        if last6 in self.live_sessions:
            return
        sess = MovesenseECGSession(last6, addr=address)
        self.live_sessions[last6] = sess
        self.live_sample_counts[last6] = 0
        # Prepare a per-sensor timer to detect "stuck at 16 samples" on first connect
        if last6 not in self.live_restart_timers:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda l=last6: self._check_and_restart_live(l))
            self.live_restart_timers[last6] = timer
        # Reset the auto-restart flag whenever we start a fresh session
        self.live_auto_restart_done[last6] = False
        sess.signals.status.connect(self._on_ecg_status)
        sess.signals.subscribed.connect(self._on_ecg_subscribed)
        sess.signals.samples.connect(self._on_ecg_samples)
        sess.signals.ecg.connect(self._on_ecg_chunk)
        sess.start()

    def _stop_session(self, last6: str):
        sess = self.live_sessions.pop(last6, None)
        if sess:
            try:
                sess.stop()
            except Exception:
                pass
        # Drop counters, buffers, and restart helpers so nothing continues updating visually
        self.live_sample_counts.pop(last6, None)
        self.live_buffers.pop(last6, None)
        t = self.live_restart_timers.pop(last6, None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        self.live_auto_restart_done.pop(last6, None)

    @QtCore.pyqtSlot(str, str)
    def _on_ecg_status(self, last6: str, message: str):
        tile_idx = self.last6_to_tile.get(last6)
        tile = self.tile_index_map.get(tile_idx) if tile_idx is not None else None
        if tile:
            tile.info_label.setText(f"Sensor {last6} · ECG -- Hz · {message}")
        self.log_message(f"[ECG {last6}] {message}")

    @QtCore.pyqtSlot(str, int)
    def _on_ecg_subscribed(self, last6: str, sr: int):
        tile_idx = self.last6_to_tile.get(last6)
        tile = self.tile_index_map.get(tile_idx) if tile_idx is not None else None
        if tile:
            addr = getattr(self.live_sessions.get(last6), "_addr", None)
            if addr:
                tile.info_label.setText(f"Sensor {last6} · ECG {sr} Hz · {addr} · Live")
            else:
                tile.info_label.setText(f"Sensor {last6} · ECG {sr} Hz · Live")

    @QtCore.pyqtSlot(str, int)
    def _on_ecg_samples(self, last6: str, inc: int):
        self.live_sample_counts[last6] = self.live_sample_counts.get(last6, 0) + inc
        count = self.live_sample_counts[last6]
        # If we see exactly 16 samples on a fresh session, arm a timer to auto-restart
        # if no further samples arrive (the "stuck at 16" behaviour).
        if count == 16 and not self.live_auto_restart_done.get(last6, False):
            timer = self.live_restart_timers.get(last6)
            if timer is not None and not timer.isActive():
                # Check again in 2 seconds whether more samples came in
                timer.start(2000)
        tile_idx = self.last6_to_tile.get(last6)
        tile = self.tile_index_map.get(tile_idx) if tile_idx is not None else None
        if tile:
            tile.info_label.setText(f"Sensor {last6} · ECG 200 Hz · Samples {count}")

    def _check_and_restart_live(self, last6: str):
        """If a sensor is still stuck at 16 samples after a short delay, restart its live session."""
        # Mark that we've attempted one automatic restart to avoid infinite loops.
        self.live_auto_restart_done[last6] = True
        count = self.live_sample_counts.get(last6, 0)
        sess = self.live_sessions.get(last6)
        if sess is None:
            return
        if count > 16:
            # Stream has progressed; nothing to do.
            return
        # We are still at or below 16 samples → emulate the manual disconnect+reconnect.
        addr = getattr(sess, "_addr", "")
        self.log_message(f"[ECG {last6}] Auto-restarting live session (stuck at {count} samples).")
        # Stop the current session cleanly.
        self._stop_session(last6)
        # Recreate the session if the tile is still assigned.
        tile_idx = self.last6_to_tile.get(last6)
        if tile_idx is not None:
            self._ensure_session_running(last6, addr)

    @QtCore.pyqtSlot(str, object)
    def _on_ecg_chunk(self, last6: str, values):
        # Initialize deque if needed
        if last6 not in self.live_buffers:
            self.live_buffers[last6] = deque(maxlen=self.max_buffer)
        try:
            self.live_buffers[last6].extend(values)
        except Exception:
            # If 'values' isn't iterable for some reason, ignore
            pass
        try:
            if len(self.live_buffers[last6]) % 160 == 0:
                buf = self.live_buffers[last6]
                try:
                    ymin = min(buf)
                    ymax = max(buf)
                except Exception:
                    ymin = ymax = 0.0
                logging.info(f"[plot {last6}] buffer={len(buf)} min={ymin:.6f} max={ymax:.6f}")
        except Exception:
            pass

    def _refresh_plots(self):
        if not HAVE_PG:
            return
        # Iterate over tiles by index for stable layout mapping
        for tile in self.tile_index_map.values():
            if not hasattr(tile, 'curve'):
                continue
            last6 = getattr(tile, 'assigned_last6', None)
            if not last6:
                tile.curve.setData([])
                continue
            buf = self.live_buffers.get(last6)
            if not buf or len(buf) == 0:
                continue
            y = list(buf)
            if not y:
                tile.curve.setData([])
                continue
            if HAVE_NP:
                y_arr = np.asarray(y, dtype=float)
            else:
                y_arr = y
            x = list(range(len(y)))
            tile.curve.setData(x=x, y=y_arr)
            # Maintain a sliding x-window
            try:
                tile.plot.setXRange(max(0, len(y) - self.max_buffer), max(1, len(y)))
            except Exception:
                pass
            # Y range is fixed in SensorTile.__init__; no need to reapply each frame.
            try:
                tile.plot.repaint()
            except Exception:
                pass

    @QtCore.pyqtSlot(QtWidgets.QListWidgetItem)
    def on_discovered_double_clicked(self, item):
        """Double-click on a discovered sensor to start live view without drag-and-drop."""
        data = item.data(Qt.UserRole) or {}
        last6 = (data.get("last6") or "").strip()
        address = data.get("address", "")
        if not last6:
            name = data.get("name", "")
            m = re.findall(r"(\d{6,})", name)
            if m:
                last6 = m[-1][-6:]
        if not last6:
            self.log_message("Double-clicked device without valid sensor ID; ignoring.")
            return

        # Decide which tile to use:
        tile_index = None
        # 1) If already assigned to a tile, reuse that tile.
        if last6 in self.last6_to_tile:
            tile_index = self.last6_to_tile[last6]
        # 2) If in sensor_list, use its "home" tile from the mapping.
        elif last6 in self.sensor_list:
            tile = self.tiles.get(last6)
            if tile is not None:
                tile_index = tile.tile_index
        # 3) Otherwise, pick the first empty tile.
        else:
            for idx, tile in self.tile_index_map.items():
                if tile.assigned_last6 is None:
                    tile_index = idx
                    break

        if tile_index is None:
            self.log_message(f"No available tile for sensor {last6}; cannot start live view.")
            return

        # Reuse the existing logic for assigning and starting live.
        self.on_sensor_dropped(last6, address, tile_index)
    def rebuild_sensor_table(self):
        # Rebuild the table and timers using self.sensor_list and self.sensor_map
        num_sensors = len(self.sensor_list)
        self.sensor_table.clear()
        self.sensor_table.setRowCount(num_sensors)
        self.sensor_table.setColumnCount(2)
        self.sensor_table.horizontalHeader().setVisible(False)
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_table.setAlternatingRowColors(True)

        small_font = self.font()
        small_font.setPointSize(max(8, small_font.pointSize() - 2))
        self.sensor_table.setFont(small_font)

        header = self.sensor_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Fixed)
        self.sensor_table.setColumnWidth(1, 90)
        self.sensor_table.setWordWrap(False)
        # Ensure table shows all rows without scroll for up to 16 sensors
        vh = self.sensor_table.verticalHeader()
        vh.setDefaultSectionSize(20)
        self.sensor_table.setMinimumHeight(
            vh.defaultSectionSize() * max(1, num_sensors)
            + self.sensor_table.horizontalHeader().height()
            + 6
        )

        self.sensor_entries = []
        for i, sensor_name in enumerate(self.sensor_list):
            display_name = sensor_name
            if self.sensor_map.get(sensor_name):
                display_name = f"{sensor_name} ({self.sensor_map[sensor_name]})"
            name_item = QtWidgets.QTableWidgetItem(display_name)
            status_item = QtWidgets.QTableWidgetItem("Pending")
            name_item.setTextAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            status_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.sensor_table.setItem(i, 0, name_item)
            self.sensor_table.setItem(i, 1, status_item)
            self.sensor_entries.append((name_item, status_item))

        # Recreate timers to match the new sensor list
        self.found_timers = {}
        for i in range(len(self.sensor_list)):
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda idx=i: self.handle_found_timeout(idx))
            self.found_timers[i] = timer
        self.rebuild_qa_tiles()
        self.log_message(f"Sensor table rebuilt with {num_sensors} sensors.")

    def rebuild_qa_tiles(self):
        """Build or refresh the 4x4 grid of SensorTile widgets with participant names."""
        # Clear current grid
        if hasattr(self, "qa_grid"):
            while self.qa_grid.count():
                item = self.qa_grid.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)

        if not hasattr(self, "tiles"):
            self.tiles = {}
        else:
            self.tiles.clear()

        if not self.sensor_list:
            return

        cols = 4  # fixed 4 columns; shows up to 16 sensors nicely
        for idx, last6 in enumerate(self.sensor_list[:16]):
            row = idx // cols
            col = idx % cols
            pname = self.sensor_map.get(last6, last6)
            tile = SensorTile(sensor_last6=last6, participant_name=pname, parent=self, tile_index=idx)
            tile.sensorDropped.connect(self.on_sensor_dropped)
            tile.stopRequested.connect(self.on_tile_stop_requested)
            tile.clearRequested.connect(self.on_tile_clear_requested)
            tile.set_placeholder(True)
            self.qa_grid.addWidget(tile, row, col)
            self.tile_index_map[idx] = tile       # lookup by tile index (used by drop handlers)
            self.tiles[last6] = tile


    @QtCore.pyqtSlot(str, int)
    def on_tile_stop_requested(self, last6: str, tile_index: int):
        # Unsubscribe/stop live session for this sensor but keep the tile assigned
        self._stop_session(last6)
        tile = self.tile_index_map.get(tile_index)
        if tile:
            try:
                tile.mark_stopped()
            except Exception:
                tile.info_label.setText(f"Sensor {last6} · Stopped")
        self.log_message(f"[ECG {last6}] stopped by user.")

    @QtCore.pyqtSlot(str, int)
    def on_tile_clear_requested(self, last6: str, tile_index: int):
        # Stop and clear the tile back to placeholder
        self._stop_session(last6)
        self.clear_tile_assignment(tile_index)
        # Small delay to allow BLE to fully disconnect so advertising resumes
        QtCore.QTimer.singleShot(300, lambda: None)
        self.log_message(f"[ECG {last6}] cleared from tile by user.")

    def toggle_mode(self):
        # (Mode toggle retained for potential future behavior changes.)
        if self.mode_toggle.isChecked():
            self.mode = "Reset"
            self.mode_toggle.setText("Mode: Reset")
            self.log_message("Switched to Reset mode.")
        else:
            self.mode = "Extract"
            self.mode_toggle.setText("Mode: Extract")
            self.log_message("Switched to Extract mode.")

    def _create_menu(self):
        menubar = self.menuBar()
        help_menu = menubar.addMenu("Help")
        about_action = QtWidgets.QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def show_about(self):
        about_text = ("Movesense Data Tool\n\n"
                      "Software signed by Jonathan Posthuma\n"
                      "Radboud University")
        QtWidgets.QMessageBox.about(self, "About", about_text)

    def select_raw_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Raw Logs Output Folder")
        if folder:
            self.raw_output_edit.setText(folder)
            self.log_message(f"Selected raw logs output folder: {folder}")

    def select_csv_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select CSV Output Folder")
        if folder:
            self.csv_output_edit.setText(folder)
            self.log_message(f"Selected CSV output folder: {folder}")

    def load_mapping_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select mapping CSV", filter="CSV Files (*.csv)")
        if not path:
            return
        mapping = {}
        pid_map = {}
        try:
            order = []
            with open(path, newline="") as f:
                rdr = csv.reader(f)
                for row in rdr:
                    if not row or len(row) < 2:
                        continue
                    s = row[0].strip()
                    # Column 1: PID (always used for file naming)
                    pid = row[1].strip() if len(row) >= 2 else ""
                    # Column 2: NAME (preferred for display in the program)
                    name = ""
                    if len(row) >= 3 and row[2].strip():
                        name = row[2].strip()
                    # For UI display, prefer NAME (3rd col) and fall back to PID (2nd col)
                    p = name or pid
                    s_digits = re.sub(r"\D", "", s)
                    if len(s_digits) >= 6 and pid:
                        key = s_digits[-6:]
                        if key not in mapping:
                            order.append(key)
                        # What the UI shows
                        mapping[key] = p
                        # What filenames use (always PID from col 2)
                        pid_map[key] = pid
            if mapping:
                self.sensor_map = mapping              # last6 -> NAME (or PID) for display
                self.sensor_pid_map = pid_map          # last6 -> PID for filenames
                self.sensor_list = order  # dynamic list in CSV order
                self.mapping_label.setText(f"Mapping loaded: {len(mapping)} entries")
                self.log_message(f"Loaded mapping CSV: {path} with {len(mapping)} entries")
                self.rebuild_sensor_table()
                self.rebuild_qa_tiles()
            else:
                QtWidgets.QMessageBox.warning(
                    self, "Mapping CSV", "No valid 'sensor_last6,PID,NAME' rows found.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Mapping CSV", f"Failed to load mapping: {e}")

    def prompt_day_number(self):
        # Ask for the day number if not yet set in this session
        day, ok = QtWidgets.QInputDialog.getInt(
            self, "Recording day", "Enter day number:",
            value=(self.day_number or 1), min=1, max=365)
        if ok:
            self.day_number = day
        return ok

    def build_target_stem(self, sensor_last6: str) -> str:
        """Return desired base filename without extension, e.g., PID_040625_3"""
        pid = self.sensor_pid_map.get(sensor_last6) or self.sensor_map.get(sensor_last6)
        if not pid:
            logging.debug(f"[target] no pid for last6={sensor_last6}")
            return ""
        date_str = datetime.now().strftime("%d%m%y")  # European DDMMYY
        day_str = str(self.day_number or 1)
        return f"{pid}_{date_str}_{day_str}"

    def build_target_name(self, sensor_last6: str) -> str:
        """Return desired CSV filename using mapping and current date. Example: PID_040625_3.csv"""
        stem = self.build_target_stem(sensor_last6)
        target = f"{stem}.csv" if stem else ""
        logging.debug(f"[target] {sensor_last6} -> {target}")
        return target

    def safe_rename(self, src: str, dst: str) -> str:
        """Rename src to dst; if dst exists, append _1, _2, ... Returns final path."""
        base, ext = os.path.splitext(dst)
        candidate = dst
        i = 1
        while os.path.exists(candidate):
            candidate = f"{base}_{i}{ext}"
            i += 1
        logging.debug(f"[os.replace] {src} -> {candidate}")
        os.replace(src, candidate)
        return candidate

    def guess_sensor_from_filename(self, filename):
        """Try to guess sensor last6 digits from the filename."""
        digits = re.findall(r'\d+', filename)
        logging.debug(f"[DEBUG] Filename: {filename}")
        logging.debug(f"[DEBUG] Digit runs found: {digits}")
        logging.debug(f"[DEBUG] Mapping keys available: {list(self.sensor_map.keys())}")

        for d in digits:
            last6 = d[-6:]
            logging.debug(f"[DEBUG] Checking digits {d} → last6 = {last6}")
            if last6 in self.sensor_map:
                logging.debug(f"[DEBUG] Match found: {last6} → {self.sensor_map[last6]}")
                return last6

        logging.debug(f"[DEBUG] No match found for {filename}")
        return None

    def _start_scanner(self):
        self.scanner_thread = ScannerThread()
        self.scanner_thread.devicesFound.connect(self.update_device_list)
        self.scanner_thread.start()
        self.log_message("Started Bluetooth scanning...")

    def update_device_list(self, devices):
        # Live discovered list
        self.discovered_list.clear()
        self.found_sensor_ids.clear()

        for d in devices:
            name = d.name or "(unknown)"
            addr = getattr(d, "address", "")

            # Build list item with payload for DnD
            item = QtWidgets.QListWidgetItem(f"{name} ({addr})")

            last6_val = ""
            if d.name and d.name.startswith("Movesense"):
                parts = d.name.split(" ")
                if len(parts) >= 2:
                    full_id = parts[1].strip()          # e.g., "243330000071"
                    last6_val = full_id[-6:]            # -> "000071"
                    self.found_sensor_ids.append(last6_val)

            # Attach metadata for drag payload
            item.setData(Qt.UserRole, {"name": name, "address": addr, "last6": last6_val})
            self.discovered_list.addItem(item)

        self.log_message(
            f"Found {len(self.found_sensor_ids)} of {len(self.sensor_list) if self.sensor_list else 0} expected sensors: {self.found_sensor_ids}"
        )

        # Update table statuses/timers the same way
        for i, (name_item, status_item) in enumerate(self.sensor_entries):
            if i >= len(self.sensor_list):
                break
            sensor_id = self.sensor_list[i]
            if sensor_id in self.found_sensor_ids:
                current_status = status_item.text().strip().lower()
                if current_status not in ("reset", "completed", "extracting"):
                    self.toggle_sensor_found(i)
    
    def toggle_sensor_found(self, sensor_index):
        # Only allow toggling if the sensor is not in Reset, Completed, or Extracting state.
        current_status = self.sensor_entries[sensor_index][1].text().strip().lower()
        if current_status in ("reset", "completed", "extracting"):
            return
        # Set status to "Found" and log the change.
        self.update_sensor_status(sensor_index, "Found")
        self.log_message(f"Sensor {self.sensor_list[sensor_index]} toggled to Found.")
        # Restart the timer (30 seconds) for reverting the status.
        self.found_timers[sensor_index].start(10000)
    
    def handle_found_timeout(self, sensor_index):
        # When the timer expires, if the sensor is still "Found" revert it to "Pending"
        current_status = self.sensor_entries[sensor_index][1].text().strip().lower()
        if current_status == "found":
            self.update_sensor_status(sensor_index, "Pending")
            self.log_message(f"Sensor {self.sensor_list[sensor_index]} timed out; reverting to Pending.")
    
    def log_message(self, message: str):
        self.status_text.append(message)
        logging.info(message)
    
    def update_sensor_status(self, sensor_index: int, status: str):
        if 0 <= sensor_index < len(self.sensor_entries):
            name_item, status_item = self.sensor_entries[sensor_index]
            status_item.setText(status)
            if status.lower() == "completed":
                status_item.setBackground(QtGui.QColor(144, 238, 144))  # light green
            elif status.lower() == "extracting":
                status_item.setBackground(QtGui.QColor(173, 216, 230))  # light blue
            elif status.lower() in ("failed", "reset"):
                status_item.setBackground(QtGui.QColor(255, 182, 193))  # light red
            elif status.lower() == "not found":
                status_item.setBackground(QtGui.QColor(255, 215, 0))    # gold/orange
            elif status.lower() == "pending":
                status_item.setBackground(QtGui.QColor(240, 240, 240))  # default light gray
            elif status.lower() == "found":
                status_item.setBackground(QtGui.QColor(255, 215, 0))
            self.log_message(f"Sensor {name_item.text()} status updated to: {status}")

    def handle_extraction_started(self, sensor_index):
        # Update UI to show the sensor is being extracted
        self.update_sensor_status(sensor_index, "Extracting")
        self.log_message(f"Sensor {self.sensor_list[sensor_index]} is now Extracting...")
    
    def handle_extraction_result(self, sensor_index, success, log_attempt_sent):
        current_status = self.sensor_entries[sensor_index][1].text().strip().lower()
        # If sensor is already Completed or Reset, do not update it further.
        if current_status in ("completed", "reset"):
            self.log_message(f"Sensor {self.sensor_list[sensor_index]} remains {current_status.title()}.")
            return

        if success:
            self.update_sensor_status(sensor_index, "Completed")
        else:
            # If a log attempt was made, update to "Reset"; otherwise "Not Found"
            if log_attempt_sent:
                self.update_sensor_status(sensor_index, "Reset")
            else:
                self.update_sensor_status(sensor_index, "Not Found")
    
    def on_extract(self):
        # If in Reset mode, send STOP_LOGGING to each discovered sensor.
        if self.mode == "Reset":
            for sensor_id in self.found_sensor_ids:
                self.log_message(f"Resetting sensor {sensor_id}…")
                threading.Thread(target=lambda sid=sensor_id: asyncio.run(_reset_sensor(sid))).start()
            return

        raw_folder = self.raw_output_edit.text()
        conv_folder = self.csv_output_edit.text()
        if not raw_folder or not conv_folder:
            QtWidgets.QMessageBox.warning(self, "Missing Folder",
                                        "Please select both raw logs and CSV output folders.")
            return

        if not self.sensor_map:
            QtWidgets.QMessageBox.warning(self, "Mapping not loaded",
                                        "Load a CSV mapping (sensor_last6,participantID) before extracting.")
            return
        if not self.prompt_day_number():
            return

        sensors_to_extract = []
        for i, sensor_id in enumerate(self.sensor_list):
            status = self.sensor_entries[i][1].text().strip().lower()
            if status in ("pending", "found"):
                sensors_to_extract.append(sensor_id)
            else:
                self.log_message(f"Sensor {sensor_id} already {status.title()}; skipping extraction.")

        if not sensors_to_extract:
            self.log_message("No pending sensors. Nothing to extract.")
            return

        self.log_message("Starting extraction for sensors: " + ", ".join(sensors_to_extract))
        self.extraction_thread = ExtractionThread(
            sensors_to_extract, raw_folder, conv_folder, self.found_sensor_ids,
            sensor_map=(self.sensor_pid_map or self.sensor_map), day_number=self.day_number)
        self.extraction_thread.extractionResult.connect(self.handle_extraction_result)
        self.extraction_thread.extractionStarted.connect(self.handle_extraction_started)
        self.extraction_thread.start()

    def on_convert(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Folder with Raw SBEM Files")
        if folder:
            conv_folder = self.csv_output_edit.text()
            if not conv_folder:
                QtWidgets.QMessageBox.warning(self, "Missing CSV Folder", "Please select a CSV output folder.")
                return
            converted_count = 0
            if not self.sensor_map:
                QtWidgets.QMessageBox.warning(self, "Mapping not loaded", "Load a CSV mapping first.")
                return
            if not self.prompt_day_number():
                return
            for file in os.listdir(folder):
                if file.endswith(".sbem"):
                    file_path = os.path.join(folder, file)
                    try:
                        csv_path = conv.convert_sbem(file_path, conv_folder)
                        if csv_path and os.path.exists(csv_path):
                            sensor_last6 = self.guess_sensor_from_filename(file)
                            if not sensor_last6:
                                self.log_message(f"Converted (sensor unknown for mapping): {csv_path}")
                            else:
                                target_name = self.build_target_name(sensor_last6)
                                if target_name:
                                    target_path = os.path.join(conv_folder, target_name)
                                    final_path = self.safe_rename(csv_path, target_path)
                                    self.log_message(f"Converted and renamed to {final_path}")
                                else:
                                    self.log_message(f"Converted (no mapping for {sensor_last6}); kept {csv_path}")
                            # --- NEW: rename the original SBEM in place to the same stem ---
                            if sensor_last6:
                                target_stem = self.build_target_stem(sensor_last6)
                                if target_stem:
                                    sbem_target = os.path.join(folder, f"{target_stem}.sbem")
                                    try:
                                        final_sbem = self.safe_rename(file_path, sbem_target)
                                        self.log_message(f"Raw SBEM renamed to {final_sbem}")
                                    except Exception as e:
                                        self.log_message(f"Failed to rename SBEM {file_path} → {sbem_target}: {e}")
                            converted_count += 1
                        else:
                            self.log_message(f"Conversion failed to produce CSV for {file_path}")
                    except Exception as e:
                        self.log_message(f"Conversion failed for {file_path}: {e}")
            self.log_message(f"Conversion completed for {converted_count} file(s) in {folder}.")
    
    def closeEvent(self, event):
        if hasattr(self, "scanner_thread"):
            try:
                self.scanner_thread.stop()
                self.scanner_thread.wait()
            except Exception:
                pass
        # Stop ECG sessions
        try:
            for last6, sess in list(self.live_sessions.items()):
                sess.stop()
            QtCore.QThread.msleep(200)
        except Exception:
            pass
        event.accept()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())