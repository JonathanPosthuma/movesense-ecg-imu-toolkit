# Movesense ECG Data Extractor

![Movesense ECG Data Extractor](./assets/ICON_program.png)

## Overview

The **Movesense ECG Data Extractor** is a Python-based application designed for seamless extraction and management of ECG data from Movesense sensors. The application provides a user-friendly graphical interface that allows data extraction from multiple sensors, and save the results in an organized manner. Below there is an explanation on how to compile the script yourself or alter source code where needed, for less tech-savy users I provide an executable which can be used straight away.


## Features

- **Simple Interface**: Easy-to-use graphical user interface (GUI) built with PyQt5.
- **Batch Processing**: Ability to import a list of sensor serial numbers and extract data from each sensor in a single operation.
- **Customizable Output**: Users can set their desired output directory for saving extracted data files.
- **Built-in data preprocessing**: Application processes raw data and provides users with HRV measures.
- **Easy to operate with sensorsoftware**: This repo contains the sensor software used on the movesense sensors.

## Installation

### Prerequisites

- Python 3.6 or later
- Pip (Python package installer)
- Movesense sensor(s)


## Usage

## Usage

1. **Start the Application**: Launch the application by running the `main_gui.py` script.
2. **Enter Sensor Serial Suffix**: Input the last digits of the sensor serial number in the provided field.
3. **Select Output Directory**: Choose a directory where the extracted data will be saved.
4. **Import Serial Suffixes File**: Optionally, import a text file containing multiple sensor serial suffixes for batch processing.
5. **Extract Data**: Click the 'Extract Data' button to begin the extraction process. The application will handle the rest, saving the data in the specified directory.



### Setup

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/JonathanPosthuma/Movesense_win_ecg_datalogger.git
   cd Movesense_win_ecg_datalogger

2. **Clone the Repository**:
    ```bash
    python -m venv venv
    source venv/Scripts/activate  # On Windows
    # or
    source venv/bin/activate  # On macOS/Linux

3. **Install dependencies**:
    ```bash
    pip install -r requirements.txt

4. **Run the Application**:
    ```bash
    python gui/main_gui.py


## Acknowledgments

- **Radboud University**: Special thanks to Movesense for support in development of this tool.
- **Jonathan Posthuma**: Lead developer and maintainer of the project.

## Contact

For any questions, suggestions, or issues, please contact Jonathan Posthuma at Jonathan.posthuma@ru.nl

