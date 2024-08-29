import sys
import os

# Ensure the src directory is recognized as a module
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFileDialog, QMessageBox, QHBoxLayout, QApplication
)
from PyQt5.QtGui import QFont
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
import asyncio
from src.ble_client import main

class ECGDataExtractorApp(QWidget):
    def __init__(self):
        super().__init__()

        # Set window properties
        self.setWindowTitle("Movesense ECG Data Extractor")
        self.setGeometry(100, 100, 600, 400)
        self.setWindowIcon(QIcon('assets/windows_icon.ico'))  # Set the window icon

        # Main layout
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        # Title Label
        self.title_label = QLabel("Movesense ECG Data Extractor")
        self.title_label.setFont(QFont("Segoe UI", 16, QFont.Bold))
        self.title_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.title_label)

        # Input layout for the Serial Suffix
        self.input_layout = QHBoxLayout()
        self.layout.addLayout(self.input_layout)

        self.label = QLabel("Enter Sensor Serial Suffix:")
        self.label.setFont(QFont("Segoe UI", 11))
        self.input_layout.addWidget(self.label)

        self.serial_entry = QLineEdit()
        self.serial_entry.setFont(QFont("Segoe UI", 11))
        self.input_layout.addWidget(self.serial_entry)

        # Output Directory Button
        self.directory_button = QPushButton("Select Output Directory")
        self.directory_button.setFont(QFont("Segoe UI", 11))
        self.directory_button.clicked.connect(self.select_directory)
        self.layout.addWidget(self.directory_button)

        # Import File Button
        self.file_button = QPushButton("Import Serial Suffixes File")
        self.file_button.setFont(QFont("Segoe UI", 11))
        self.file_button.clicked.connect(self.import_serial_file)
        self.layout.addWidget(self.file_button)

        # Extract Button
        self.extract_button = QPushButton("Extract Data")
        self.extract_button.setFont(QFont("Segoe UI", 11))
        self.extract_button.clicked.connect(self.extract_data)
        self.layout.addWidget(self.extract_button)

        # Footer Label
        self.footer_label = QLabel("Jonathan Posthuma - Radboud University")
        self.footer_label.setFont(QFont("Segoe UI", 10))
        self.footer_label.setAlignment(Qt.AlignRight)
        self.layout.addWidget(self.footer_label)

        # Internal variables
        self.serial_suffixes = []
        self.output_directory = None

    def select_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory")
        if directory:
            self.output_directory = directory
            self.directory_button.setText(f"Output Directory: {directory}")

    def import_serial_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Serial Suffixes File", "", "Text files (*.txt)")
        if file_path:
            with open(file_path, 'r') as file:
                self.serial_suffixes = [line.strip() for line in file.readlines()]
            QMessageBox.information(self, "Info", f"Loaded {len(self.serial_suffixes)} serial suffixes from file.")
        
    def extract_data(self):
        if not self.output_directory:
            QMessageBox.critical(self, "Error", "Please select an output directory.")
            return

        if self.serial_suffixes:
            for suffix in self.serial_suffixes:
                asyncio.run(main(suffix, self.output_directory))
            QMessageBox.information(self, "Info", "Data extraction completed for all serial suffixes.")
        else:
            serial_suffix = self.serial_entry.text().strip()
            if serial_suffix:
                asyncio.run(main(serial_suffix, self.output_directory))
                QMessageBox.information(self, "Info", "Data extraction started. Check the logs for details.")
            else:
                QMessageBox.critical(self, "Error", "Please enter a valid sensor serial suffix or import a file.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon('assets/windows_icon.ico'))  # Set the application icon
    window = ECGDataExtractorApp()
    window.show()
    sys.exit(app.exec_())