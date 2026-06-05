# Movesense ECG & IMU Toolkit

## Overview

The **Movesense ECG & IMU Toolkit** is a Python-based desktop application for interacting with custom WinLogger-enabled Movesense sensors.

The toolkit supports:

- Offline log extraction and conversion to CSV
- Real-time ECG streaming
- Live battery monitoring
- Multi-sensor workflows
- Automatic parsing of ECG and IMU recordings
- Participant-based file naming and organization

For non-technical users, the toolkit can be distributed as a standalone macOS application. Developers can run the software directly from source and customize both the desktop application and sensor software.

---

## Features

### Offline Data Extraction

- Extract long-duration recordings from multiple sensors
- Automatic conversion from SBEM logs to CSV
- Simultaneous ECG and IMU support
- Automatic sensor reset after successful extraction

### Live Monitoring

- Real-time ECG streaming
- Live battery status monitoring
- Direct communication with sensors through the custom WinLogger BLE service

### Data Management

- Dynamic sensor list using CSV mapping
- Automatic participant-based file naming
- Batch processing of multiple devices
- Automated conversion pipeline

### Sensor Software Included

This repository also contains the custom WinLogger sensor software built using the Movesense SDK.

---

## Compatibility

### Desktop Application

- macOS (Apple Silicon tested)
- Python 3.12 recommended

### Sensor Software

Live ECG streaming and battery monitoring require:

- WinLogger sensor software version **1.7 or newer**

Older versions support offline logging but may not support all live monitoring functionality.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/JonathanPosthuma/movesense-ecg-imu-toolkit.git
cd movesense-ecg-imu-toolkit
```

### 2. Activate the virtual environment

```bash
source .venv/bin/activate
```

### 3. Launch the application

```bash
python pc-extractor-parser/main.py
```

---

## Building the macOS Application

```bash
pyinstaller --noconfirm --windowed \
  --name "Movesense Toolkit" \
  --icon icons/app.icns \
  --add-data "icons:icons" \
  --add-data "gui:gui" \
  --collect-submodules bleak.backends.corebluetooth \
  gui/main_window.py
```

The generated application can be found in:

```text
pc-extractor-parser/dist/
└── Movesense Toolkit.app
```

---

## Software Usage

### 1. Load Sensor Mapping

Load a CSV containing:

```text
sensor_last6,participantID
```

Example:

```text
123ABC,PARTICIPANT01
456DEF,PARTICIPANT02
```

An example file is included:

```text
pc-extractor-parser/test_list.csv
```

---

### 2. Select Output Directories

- Raw Folder → stores downloaded SBEM files
- Converted Folder → stores processed CSV files

---

### 3. Reset Sensors (Optional)

If sensors are currently recording, switch to Reset Mode and press Extract to reset them.

---

### 4. Extract Offline Data

1. Switch to Extract Mode
2. Activate up to four sensors simultaneously by touching both electrode pins
3. Wait for extraction and conversion to complete

After successful extraction:

- Sensors are reset
- Log data is removed from device memory
- CSV files are generated automatically

---

### 5. Live ECG Streaming

1. Connect to a WinLogger 1.7 sensor
2. Start live monitoring
3. View incoming ECG data in real time

---

### 6. Battery Monitoring

Battery status can be queried directly from connected sensors running WinLogger 1.7 or newer.

---

## Output Naming Convention

Converted files are named:

```text
ParticipantID_DDMMYY_day.csv
```

Example:

```text
3VSAN2PR_040625_3.csv
```

---

## Repository Structure

```text
movesense-ecg-imu-toolkit/
├─ README.md
├─ CHANGELOG.md
├─ requirements.txt
│
├─ fetcher-parser/
│  ├─ fetch_logbook_data.py
│  └─ parser_imu_ecg.py
│
├─ pc-extractor-parser/
│  ├─ DATA/
│  │  ├─ Raw/
│  │  └─ Converted/
│  ├─ conversion/
│  ├─ extraction/
│  ├─ gui/
│  ├─ icons/
│  ├─ dist/
│  └─ main.py
│
└─ sensor-software/
   └─ win_ecglogger_app/
```

---

## Contact

Jonathan Posthuma  
Radboud University  
jonathan.posthuma@ru.nl
