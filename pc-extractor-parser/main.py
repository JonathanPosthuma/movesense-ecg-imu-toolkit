#!/usr/bin/env python3
import sys
import logging
from gui.main_window import MainWindow

def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename='logs/app.log',
        filemode='a'
    )
    # Also log to console
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

def main():
    configure_logging()
    logging.info("Starting Movesense Tool")
    
    # Start the GUI application.
    # For example, if using PyQt5:
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)
    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()