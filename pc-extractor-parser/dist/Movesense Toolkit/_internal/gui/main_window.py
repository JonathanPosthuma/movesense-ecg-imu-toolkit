import os
import glob
import logging
import asyncio
import threading
from PyQt5 import QtWidgets, QtCore, QtGui
import random
import csv
import re
from datetime import datetime

# Import SENSOR_LIST and the asynchronous extraction function.
from extraction.extractor import SENSOR_LIST, extract_sensor, send_stop_logging
# Import the conversion function.
import conversion.converter as conv

# --- ScannerThread definition (self-contained) ---
from bleak import discover, BleakClient
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

from concurrent.futures import ThreadPoolExecutor

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

    def _build_target_name(self, sensor_last6: str) -> str:
        pid = self.sensor_map.get(sensor_last6)
        if not pid:
            return ""
        date_str = datetime.now().strftime("%d%m%y")
        return f"{pid}_{date_str}_{self.day_number}.csv"

    def _safe_rename(self, src: str, dst: str) -> str:
        base, ext = os.path.splitext(dst)
        candidate = dst
        i = 1
        while os.path.exists(candidate):
            candidate = f"{base}_{i}{ext}"
            i += 1
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
                    pattern = os.path.join(self.raw_folder, f"*{sensor_id}_*.sbem")
                    matching_files = glob.glob(pattern)
                    if matching_files:
                        for file_path in matching_files:
                            logging.info(f"Converting file {file_path} for sensor {sensor_id}...")
                            try:
                                csv_path = await loop.run_in_executor(executor, conv.convert_sbem, file_path, self.conv_folder)
                                if csv_path and os.path.exists(csv_path):
                                    target_name = self._build_target_name(sensor_id)
                                    if target_name:
                                        target_path = os.path.join(self.conv_folder, target_name)
                                        final_path = self._safe_rename(csv_path, target_path)
                                        logging.info(f"Converted and renamed to {final_path}")
                                    else:
                                        logging.info(f"Converted (no mapping for {sensor_id}); kept {csv_path}")
                                else:
                                    logging.error(f"Conversion did not produce a CSV for {file_path}")
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
        self.setWindowIcon(QtGui.QIcon("icons/my_icon.png"))
        self.setWindowTitle("Movesense Data Tool")
        self.setGeometry(100, 100, 800, 600)
        self.found_sensor_ids = []
        self.sensor_map = {}
        self.day_number = None
        self.sensor_list = []  # dynamic list from CSV (last 6 digits)
        self._setup_ui()
        self._create_menu()  # Create menu including About
        self._start_scanner()
        self.statusBar().showMessage("Software signed by Jonathan Posthuma, Radboud University")
        
        # Timers will be created dynamically after loading CSV

    def _setup_ui(self):
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QVBoxLayout(central_widget)
        
        # --- Bluetooth Device List Section ---
        self.device_list = QtWidgets.QListWidget()
        main_layout.addWidget(QtWidgets.QLabel("Discovered Movesense Devices:"))
        main_layout.addWidget(self.device_list)
        
        # --- Sensor List Section (dynamic grid based on loaded CSV) ---
        num_sensors = len(self.sensor_list)
        rows = (num_sensors + 1) // 2 if num_sensors else 0
        columns = 4  # 2 sensors per row, each sensor uses 2 columns (name, status)
        self.sensor_table = QtWidgets.QTableWidget(rows, columns)
        self.sensor_table.horizontalHeader().setVisible(False)
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_entries = []
        for i, sensor_name in enumerate(self.sensor_list):
            row = i // 2
            col_offset = (i % 2) * 2
            display_name = sensor_name
            if self.sensor_map.get(sensor_name):
                display_name = f"{sensor_name} ({self.sensor_map[sensor_name]})"
            name_item = QtWidgets.QTableWidgetItem(display_name)
            status_item = QtWidgets.QTableWidgetItem("Pending")
            name_item.setTextAlignment(QtCore.Qt.AlignCenter)
            status_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.sensor_table.setItem(row, col_offset, name_item)
            self.sensor_table.setItem(row, col_offset + 1, status_item)
            self.sensor_entries.append((name_item, status_item))
        # Widen columns so sensor + participant text fits
        header = self.sensor_table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        self.sensor_table.setColumnWidth(0, 290)  # name (left)
        self.sensor_table.setColumnWidth(1, 110)  # status (left)
        self.sensor_table.setColumnWidth(2, 290)  # name (right)
        self.sensor_table.setColumnWidth(3, 110)  # status (right)
        self.sensor_table.setWordWrap(False)
        main_layout.addWidget(QtWidgets.QLabel("Sensor Extraction Status:"))
        main_layout.addWidget(self.sensor_table)

        # --- Control Buttons Section ---
        button_layout = QtWidgets.QHBoxLayout()
        self.extract_button = QtWidgets.QPushButton("Extract Data")
        self.extract_button.clicked.connect(self.on_extract)
        button_layout.addWidget(self.extract_button)
        self.convert_button = QtWidgets.QPushButton("Convert Data")
        self.convert_button.clicked.connect(self.on_convert)
        button_layout.addWidget(self.convert_button)
        main_layout.addLayout(button_layout)
        
        # --- MODE Toggle Button Section (for future expansion) ---
        mode_layout = QtWidgets.QHBoxLayout()
        self.mode_toggle = QtWidgets.QPushButton("Mode: Extract")
        self.mode_toggle.setCheckable(True)
        self.mode_toggle.clicked.connect(self.toggle_mode)
        mode_layout.addWidget(self.mode_toggle)
        main_layout.addLayout(mode_layout)
        self.mode = "Extract"  # default mode
        
        # --- Settings Section with Folder Browsing ---
        raw_layout = QtWidgets.QHBoxLayout()
        self.raw_output_edit = QtWidgets.QLineEdit()
        self.raw_output_edit.setPlaceholderText("Raw logs output folder")
        raw_browse_button = QtWidgets.QPushButton("Browse")
        raw_browse_button.clicked.connect(self.select_raw_folder)
        raw_layout.addWidget(self.raw_output_edit)
        raw_layout.addWidget(raw_browse_button)
        main_layout.addLayout(raw_layout)
        csv_layout = QtWidgets.QHBoxLayout()
        self.csv_output_edit = QtWidgets.QLineEdit()
        self.csv_output_edit.setPlaceholderText("CSV output folder")
        csv_browse_button = QtWidgets.QPushButton("Browse")
        csv_browse_button.clicked.connect(self.select_csv_folder)
        csv_layout.addWidget(self.csv_output_edit)
        csv_layout.addWidget(csv_browse_button)
        main_layout.addLayout(csv_layout)

        # --- Mapping CSV Section ---
        mapping_layout = QtWidgets.QHBoxLayout()
        self.mapping_label = QtWidgets.QLabel("Mapping: not loaded")
        load_mapping_button = QtWidgets.QPushButton("Load Sensor→Participant CSV")
        load_mapping_button.clicked.connect(self.load_mapping_csv)
        mapping_layout.addWidget(self.mapping_label)
        mapping_layout.addWidget(load_mapping_button)
        main_layout.addLayout(mapping_layout)
                
        # --- Log/Status Output Area ---
        self.status_text = QtWidgets.QTextEdit()
        self.status_text.setReadOnly(True)
        main_layout.addWidget(self.status_text)
        
    def rebuild_sensor_table(self):
        # Rebuild the table and timers using self.sensor_list and self.sensor_map
        num_sensors = len(self.sensor_list)
        rows = (num_sensors + 1) // 2 if num_sensors else 0
        self.sensor_table.clear()
        self.sensor_table.setRowCount(rows)
        self.sensor_table.setColumnCount(4)
        self.sensor_table.horizontalHeader().setVisible(False)
        self.sensor_table.verticalHeader().setVisible(False)
        # Widen columns so sensor + participant text fits (again after rebuild)
        header = self.sensor_table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        self.sensor_table.setColumnWidth(0, 290)
        self.sensor_table.setColumnWidth(1, 110)
        self.sensor_table.setColumnWidth(2, 290)
        self.sensor_table.setColumnWidth(3, 110)
        self.sensor_table.setWordWrap(False)
        self.sensor_entries = []
        for i, sensor_name in enumerate(self.sensor_list):
            row = i // 2
            col_offset = (i % 2) * 2
            display_name = sensor_name
            if self.sensor_map.get(sensor_name):
                display_name = f"{sensor_name} ({self.sensor_map[sensor_name]})"
            name_item = QtWidgets.QTableWidgetItem(display_name)
            status_item = QtWidgets.QTableWidgetItem("Pending")
            name_item.setTextAlignment(QtCore.Qt.AlignCenter)
            status_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.sensor_table.setItem(row, col_offset, name_item)
            self.sensor_table.setItem(row, col_offset + 1, status_item)
            self.sensor_entries.append((name_item, status_item))
        # Recreate timers to match the new sensor list
        self.found_timers = {}
        for i in range(len(self.sensor_list)):
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda idx=i: self.handle_found_timeout(idx))
            self.found_timers[i] = timer
        self.log_message(f"Sensor table rebuilt with {num_sensors} sensors.")
    
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
        try:
            order = []
            with open(path, newline="") as f:
                rdr = csv.reader(f)
                for row in rdr:
                    if not row or len(row) < 2:
                        continue
                    s, p = row[0].strip(), row[1].strip()
                    s_digits = re.sub(r"\D", "", s)
                    if len(s_digits) >= 6 and p:
                        key = s_digits[-6:]
                        if key not in mapping:
                            order.append(key)
                        mapping[key] = p
            if mapping:
                self.sensor_map = mapping
                self.sensor_list = order  # dynamic list in CSV order
                self.mapping_label.setText(f"Mapping loaded: {len(mapping)} entries")
                self.log_message(f"Loaded mapping CSV: {path} with {len(mapping)} entries")
                self.rebuild_sensor_table()
            else:
                QtWidgets.QMessageBox.warning(
                    self, "Mapping CSV", "No valid 'sensor_last6,participantID' rows found.")
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

    def build_target_name(self, sensor_last6: str) -> str:
        """Return desired CSV filename using mapping and current date. Example: PID_040625_3.csv"""
        pid = self.sensor_map.get(sensor_last6)
        if not pid:
            return ""
        date_str = datetime.now().strftime("%d%m%y")  # European DDMMYY
        day_str = str(self.day_number or 1)
        return f"{pid}_{date_str}_{day_str}.csv"

    def safe_rename(self, src: str, dst: str) -> str:
        """Rename src to dst; if dst exists, append _1, _2, ... Returns final path."""
        base, ext = os.path.splitext(dst)
        candidate = dst
        i = 1
        while os.path.exists(candidate):
            candidate = f"{base}_{i}{ext}"
            i += 1
        os.replace(src, candidate)
        return candidate

    def guess_sensor_from_filename(self, filename: str) -> str:
        """Try to find a 6-digit sensor suffix in the filename that exists in the mapping."""
        for m in re.finditer(r"(\d{6,})", filename):
            last6 = m.group(1)[-6:]
            if last6 in self.sensor_map:
                return last6
        return ""
    
    def _start_scanner(self):
        self.scanner_thread = ScannerThread()
        self.scanner_thread.devicesFound.connect(self.update_device_list)
        self.scanner_thread.start()
        self.log_message("Started Bluetooth scanning...")
    
    def update_device_list(self, devices):
        self.device_list.clear()
        # Clear the current list in-place
        self.found_sensor_ids.clear()
        for device in devices:
            item_text = f"{device.name} ({device.address})"
            self.device_list.addItem(item_text)
            if device.name and device.name.startswith("Movesense"):
                parts = device.name.split(" ")
                if len(parts) >= 2:
                    full_id = parts[1].strip()  # e.g., "243330000071"
                    sensor_id = full_id[-6:]     # e.g., "000071"
                    self.found_sensor_ids.append(sensor_id)
        self.log_message(f"Found {len(devices)} Movesense device(s): {self.found_sensor_ids}")
        
        # For each sensor, if it is found and is not in a blocked state,
        # toggle it to display "Found" and restart its timeout.
        for i, (name_item, status_item) in enumerate(self.sensor_entries):
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
            sensor_map=self.sensor_map, day_number=self.day_number)
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
                            converted_count += 1
                        else:
                            self.log_message(f"Conversion failed to produce CSV for {file_path}")
                    except Exception as e:
                        self.log_message(f"Conversion failed for {file_path}: {e}")
            self.log_message(f"Conversion completed for {converted_count} file(s) in {folder}.")
    
    def closeEvent(self, event):
        if hasattr(self, "scanner_thread"):
            self.scanner_thread.stop()
            self.scanner_thread.wait()
        event.accept()

def closeEvent(self, event):
    if hasattr(self, "scanner_thread"):
        self.scanner_thread.stop()
        self.scanner_thread.wait()
    event.accept()

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())