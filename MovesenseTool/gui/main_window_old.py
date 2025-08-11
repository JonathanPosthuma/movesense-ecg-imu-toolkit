import os
import glob
import logging
import asyncio
from PyQt5 import QtWidgets, QtCore, QtGui
import random


# Import SENSOR_LIST and the asynchronous extraction function.
from extraction.extractor import SENSOR_LIST, extract_sensor
# Import the conversion function.
import conversion.converter as conv

# --- ScannerThread definition (self-contained) ---
from bleak import discover

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

# Import SENSOR_LIST and the asynchronous extraction function.
from extraction.extractor import SENSOR_LIST, extract_sensor
# Import the conversion function.
import conversion.converter as conv

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

# --- Updated ExtractionThread definition with concurrent workers ---
class ExtractionThread(QtCore.QThread):
    # Signal sends sensor_index (int), final extraction result (bool), and log_attempt_sent (bool)
    extractionStarted = QtCore.pyqtSignal(int)
    extractionResult = QtCore.pyqtSignal(int, bool, bool)
    
    def __init__(self, sensor_list, raw_folder, conv_folder, found_sensor_ids, parent=None):
        super().__init__(parent)
        # sensor_list: list of 6-digit sensor IDs from SENSOR_LIST.
        self.sensor_list = sensor_list[:]  # copy of list (each element is a sensor id string)
        self.raw_folder = raw_folder
        self.conv_folder = conv_folder
        self.found_sensor_ids = found_sensor_ids  # shared, mutable list updated externally
        # Track sensor completion status. True means extraction (and conversion) succeeded.
        self.completed = [False] * len(self.sensor_list)
        # A set to mark sensors currently being processed (to avoid duplicate work)
        self.busy = set()
        # Use an asyncio Lock to protect access to shared pending list and busy set.
        self.selection_lock = asyncio.Lock()
        # Define the maximum number of concurrent extraction tasks.
        self.concurrency_limit = 4

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
                                await loop.run_in_executor(executor, conv.convert_sbem, file_path, self.conv_folder)
                                logging.info(f"Conversion succeeded for {file_path}")
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
        self._setup_ui()
        self._create_menu()  # Create menu including About
        self._start_scanner()
        self.statusBar().showMessage("Software signed by Jonathan Posthuma, Radboud University")
        self.found_sensor_ids = []
    
    def _setup_ui(self):
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QVBoxLayout(central_widget)
        
        # --- Bluetooth Device List Section ---
        self.device_list = QtWidgets.QListWidget()
        main_layout.addWidget(QtWidgets.QLabel("Discovered Movesense Devices:"))
        main_layout.addWidget(self.device_list)
        
        # --- Sensor List Section as a 2x8 grid ---
        num_sensors = len(SENSOR_LIST)
        rows = num_sensors // 2   # 8 rows total.
        columns = 4               # 2 sensors per row * 2 columns each.
        self.sensor_table = QtWidgets.QTableWidget(rows, columns)
        self.sensor_table.horizontalHeader().setVisible(False)
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_entries = []
        for i, sensor_name in enumerate(SENSOR_LIST):
            row = i // 2
            col_offset = (i % 2) * 2
            name_item = QtWidgets.QTableWidgetItem(sensor_name)
            status_item = QtWidgets.QTableWidgetItem("Pending")
            name_item.setTextAlignment(QtCore.Qt.AlignCenter)
            status_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.sensor_table.setItem(row, col_offset, name_item)
            self.sensor_table.setItem(row, col_offset + 1, status_item)
            self.sensor_entries.append((name_item, status_item))
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
        
        # --- Log/Status Output Area ---
        self.status_text = QtWidgets.QTextEdit()
        self.status_text.setReadOnly(True)
        main_layout.addWidget(self.status_text)
    
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
        # Update sensor table rows if a sensor was previously "Reset" but is now discovered.
        for i, (name_item, status_item) in enumerate(self.sensor_entries):
            sensor_id = SENSOR_LIST[i]
            if status_item.text().strip().lower() in ("not found", "reset") and sensor_id in self.found_sensor_ids:
                self.update_sensor_status(i, "Pending")
                self.log_message(f"Sensor {sensor_id} found again, status updated to Pending.")
    
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
            self.log_message(f"Sensor {name_item.text()} status updated to: {status}")

    def handle_extraction_started(self, sensor_index):
    # Update UI to show the sensor is being extracted
    # You can use a different color or status text.
        self.update_sensor_status(sensor_index, "Extracting")
        self.log_message(f"Sensor {SENSOR_LIST[sensor_index]} is now Extracting...")
    
    def handle_extraction_result(self, sensor_index, success, log_attempt_sent):
        current_status = self.sensor_entries[sensor_index][1].text().strip().lower()
        # If sensor is already Completed or Reset, do not update it further.
        if current_status in ("completed", "reset"):
            self.log_message(f"Sensor {SENSOR_LIST[sensor_index]} remains {current_status.title()}.")
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
        raw_folder = self.raw_output_edit.text()
        conv_folder = self.csv_output_edit.text()
        if not raw_folder or not conv_folder:
            QtWidgets.QMessageBox.warning(self, "Missing Folder", 
                                        "Please select both raw logs and CSV output folders.")
            return

        sensors_to_extract = []
        for i, sensor_id in enumerate(SENSOR_LIST):
            status = self.sensor_entries[i][1].text().strip().lower()
            if status == "pending":
                sensors_to_extract.append(sensor_id)
            else:
                self.log_message(f"Sensor {sensor_id} already {status.title()}; skipping extraction.")
        
        if not sensors_to_extract:
            self.log_message("No pending sensors. Nothing to extract.")
            return

        # Pass the discovered sensor list to the extraction thread.
        self.log_message("Starting extraction for sensors: " + ", ".join(sensors_to_extract))
        self.extraction_thread = ExtractionThread(sensors_to_extract, raw_folder, conv_folder, self.found_sensor_ids)
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
            for file in os.listdir(folder):
                if file.endswith(".sbem"):
                    file_path = os.path.join(folder, file)
                    try:
                        conv.convert_sbem(file_path, conv_folder)
                        converted_count += 1
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