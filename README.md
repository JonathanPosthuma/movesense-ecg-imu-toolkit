# Movesense ECG & IMU Toolkit 



## Overview

The **Movesense ECG & IMU Toolkit** is a Python-based GUI application for extracting long offline recordings from Movesense sensors and converting them into CSV. It supports both **ECG** and **IMU** streams, handles multiple sensors, and names output files using dynamic **ParticipantID + date + day** convention.
For non-technical users, you can ship a one-click macOS app; for developers, you can run from source or customize.

## Features

- **Simple GUI (PyQt5):** Start/stop extraction and monitor sensor status at a glance.
- **Dynamic sensor list:** Load a CSV mapping of `sensor_last6,participantID` to drive the grid and file naming.
- **Batch extraction:** Extract from multiple sensors in one go; conversion runs automatically after extraction.
- **Consistent file naming:** Output CSVs named `ParticipantID_DDMMYY_day.csv` (e.g., `3VSAN2PR_040625_3.csv`).
- **Built-in conversion:** Parses SBEM logs (ECG mV packets & new IMU6 format) into tidy CSV.
- **Sensor software included:** Repo also contains the Movesense sensor-side software you use.

## Requirements

- **macOS** with Bluetooth (tested on Apple Silicon).
- **Python 3.12** recommended (3.11 also works).
- Movesense sensors with logging enabled. Software can be found in sensor-software.

> Windows builds are possible but require different BLE backends; this README focuses on **macOS**.


## Quick Start 
1. **Clone the repository**

```bash
git clone https://github.com/JonathanPosthuma/movesense-ecg-imu-toolkit.git
```

2. **Launch virtual environment**

```bash
cd /movesense-ecg-imu-toolkit
source venv/bin/activate
```

2a. **Launch application**

```bash
python pc-extractor-parser/main.py
```

## OR

2b. **Build the application**

```bash
pyinstaller --noconfirm --windowed \                                                           
--name "Movesense Toolkit" \
--icon assets/app.icns \
--add-data "icons:icons" \
--add-data "gui:gui" \
--collect-submodules bleak.backends.corebluetooth \
gui/main_window.py
```

3. **Find and run the application**

```text
├── pc-extractor-parser
│   ├── dist
│   │   ├── Movesense Toolkit
│   │   └── Movesense Toolkit.app
```

## Software Usage

1. **Load sensorID and ParticipantID's list**
Example CSV can be found (movesense-ecg-imu-toolkit/pc-extractor-parser/test_list.csv)


2. **Select output folders**
Raw data folder will store the raw .SBEM folder whilst the converted folder will contain the converted CSV files

3. **(Reset sensors)**
If sensors are still running you can reset them by pressing extract whilst in reset mode.

4. **Extract data**
Click on extract in extract mode. Activate sensors four at a time by touching both pins. After extraction and conversion is completed sensors will be reset and data will be deleted of the sensors.

5. **Sensors are ready for use**
Sensors are ready to be activated again by connecting both pins; these are very sensitive so try not to let them connect in the mean time. 

## Example Output Naming

Files are saved as:

```text
ParticipantID_DDMMYY_day.csv
e.g., 3VSAN2PR_040625_3.csv
```

## Folder Structure

```text
movesense-ecg-imu-toolkit/
├─ README.md
├─ requirements.txt
├─ fetcher-parser/
│  ├─ fetch_logbook_data.py
│  └─ parser_imu_ecg.py
├─ pc-extractor-parser/
│  ├─ DATA/
│  │  ├─ Raw/
│  │  └─ Converted/
│  ├─ assets/
│  │  └─ app.icns
│  ├─ conversion/
│  │  └─ converter.py
│  ├─ extraction/
│  │  └─ extractor.py
│  ├─ gui/
│  │  └─ main_window.py
│  ├─ icons/
│  │  └─ my_icon.png
│  └─ main.py
└─ sensor-software/
   └─ win_ecglogger_app/
```

## Contact

Questions or ideas? Jonathan Posthuma - jonathan.posthuma@ru.nl