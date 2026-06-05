# Changelog

All notable changes to WinLogger will be documented in this file.

## [1.7.0]

### Added
- Live battery status monitoring via the custom WinLogger BLE service.
- Battery level can now be queried directly from the desktop toolkit.

### Improved
- Reliability improvements for BLE communication and device status reporting.

---

## [1.6.0]

### Added
- Real-time ECG streaming.
- Live ECG monitoring without interrupting recording sessions.
- Support for configurable ECG packet sizes to improve streaming performance.

### Improved
- BLE data handling and streaming stability.

---

## [1.5.0]

### Added
- Custom WinLogger BLE service for communication with the desktop toolkit.
- Wireless log retrieval and data transfer functionality.
- Support for transferring recorded data directly to the desktop application.

### Improved
- BLE connection management and reconnection behavior.
- Data transfer robustness and reliability.

---

## [1.4.0] - 2025-04-23

### Added
- Simultaneous multi-sensor recording:
  - ECG at 200 Hz
  - IMU (accelerometer + gyroscope) at 26 Hz
- Contact-triggered recording start.
- Automatic recording stop after prolonged loss of electrode contact.
- Manual shutdown and data extraction through the toolkit.

### Notes
- Supported on Movesense devices running firmware 2.2.0 or later.
- Recording starts automatically when electrode contact is detected.
- Recording stops after 9 hours without electrode contact to conserve battery and storage.