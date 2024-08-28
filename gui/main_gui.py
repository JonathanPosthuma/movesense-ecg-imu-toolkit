import sys
import os

# Ensure the src directory is recognized as a module
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.ble_client import main


import tkinter as tk
from tkinter import messagebox
import asyncio
from src.ble_client import main

class ECGDataExtractorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ECG Data Extractor")
        self.geometry("400x200")

        self.label = tk.Label(self, text="Enter Sensor Serial Suffix:")
        self.label.pack(pady=10)

        self.serial_entry = tk.Entry(self, width=30)
        self.serial_entry.pack(pady=10)

        self.extract_button = tk.Button(self, text="Extract Data", command=self.extract_data)
        self.extract_button.pack(pady=20)

    def extract_data(self):
        serial_suffix = self.serial_entry.get().strip()
        if serial_suffix:
            asyncio.run(main(serial_suffix))
            messagebox.showinfo("Info", "Data extraction started. Check the logs for details.")
        else:
            messagebox.showerror("Error", "Please enter a valid sensor serial suffix.")

if __name__ == "__main__":
    print("Starting the application...")  # Should print this line first
    app = ECGDataExtractorApp()
    print("App created successfully")  # Check if this line is printed
    app.mainloop()
    print("Exited main loop")  # This line should print when the GUI closes

