#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Movesense log extraction client (multiple logs).

This client:
  1. Discovers a Movesense sensor (matching by the end of its name).
  2. Connects and subscribes to notifications.
  3. Sequentially fetches log files—one file per log ID.
  
The FETCH_LOG command is sent with a six‐byte payload:
  • Byte 0: 3 (FETCH_LOG)
  • Byte 1: 101 (client reference)
  • Bytes 2–5: the log ID as a little‑endian 32‑bit unsigned int

Notifications from the sensor are expected in the format:
  • Byte 0: Data type (2 = DATA, 3 = DATA_PART2)
  • Byte 1: Client reference
  • Bytes 2–5: 32‑bit offset (little‑endian)
  • Bytes 6–end: data payload
  
An “end‐of‐file” is signaled by a notification whose payload (bytes after the offset) is empty.
  
If a log fetch times out (i.e. no notifications arrive) the client assumes there are no
more logs to extract.
"""

import asyncio
import logging
import signal
import struct
import sys
from functools import reduce

from bleak import BleakClient, discover

# UUIDs as used in your sensor firmware:
WRITE_CHARACTERISTIC_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"
NOTIFY_CHARACTERISTIC_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"

# -----------------------------------------------------------------------------
# Helper: DataView class for binary extraction
# -----------------------------------------------------------------------------
class DataView:
    def __init__(self, array, bytes_per_element=1):
        self.array = array
        self.bytes_per_element = bytes_per_element

    def __get_binary(self, start_index, byte_count, signed=False):
        integers = [self.array[start_index + x] for x in range(byte_count)]
        # Build a byte string from the integers.
        bytes_seq = [integer.to_bytes(self.bytes_per_element, byteorder='little', signed=signed)
                     for integer in integers]
        return reduce(lambda a, b: a + b, bytes_seq)

    def get_uint_16(self, start_index):
        return int.from_bytes(self.__get_binary(start_index, 2), byteorder='little')

    def get_uint_8(self, start_index):
        return int.from_bytes(self.__get_binary(start_index, 1), byteorder='little')

    def get_uint_32(self, start_index):
        binary = self.__get_binary(start_index, 4)
        return struct.unpack('<I', binary)[0]

    def get_float_32(self, start_index):
        binary = self.__get_binary(start_index, 4)
        return struct.unpack('<f', binary)[0]

# -----------------------------------------------------------------------------
# Notification handler
# -----------------------------------------------------------------------------
async def notification_handler(sender, data, queue: asyncio.Queue):
    """
    Notification handler for data characteristic.
    The sensor sends a 6+ byte notification where:
      - Byte 0: Data type (2 or 3 for log data)
      - Byte 1: Client reference (ignored here)
      - Bytes 2-5: 32-bit offset (little-endian)
      - Bytes 6-end: payload (empty payload indicates EOF)
      
    We strip off the first 2 bytes so that the trimmed data starts with the offset.
    """
    # Trim off the first two bytes so that the trimmed data starts with the offset.
    await queue.put(data[2:])

# -----------------------------------------------------------------------------
# Fetch a single log file
# -----------------------------------------------------------------------------
async def fetch_log(client: BleakClient, queue: asyncio.Queue, sensor_name: str,
                    log_id: int, disconnect_event: asyncio.Event) -> bool:
    """
    Fetch one log file (by log_id) and write it to disk.
    
    The function:
      - Opens a file named "log_<log_id>_<sensor_name>.sbem"
      - Sends a FETCH_LOG command (payload: [3, 101, log_id, 0, 0, 0])
      - Reads notifications from the shared queue.
      - Writes received payload chunks at the given offset.
      - When a notification with an empty payload is received, the log is complete.
      
    Returns True if log data was received; if a timeout occurs (no notifications),
    returns False (which the main loop uses to decide whether to continue).
    """
    filename = f"log_{log_id}_{sensor_name}.sbem"
    logging.info(f"Attempting to fetch log {log_id} -> '{filename}'")
    try:
        with open(filename, 'wb') as f:
            # Build and send the FETCH_LOG command.
            # Command format: [3, 101, <log_id>, 0, 0, 0]
            command = bytearray([3, 101, log_id, 0, 0, 0])
            logging.info(f"Sending FETCH_LOG command for log {log_id}: {command.hex()}")
            await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, command, response=True)
            
            while not disconnect_event.is_set():
                try:
                    # Wait for a notification (with a 10-second timeout).
                    item = await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    logging.warning(f"Timeout waiting for data for log {log_id}.")
                    return False

                if isinstance(item, bytearray):
                    dv = DataView(item)
                    offset = dv.get_uint_32(0)
                    payload = item[4:]
                    if len(payload) > 0:
                        f.seek(offset)
                        f.write(payload)
                        logging.info(f"Log {log_id}: wrote {len(payload)} bytes at offset {offset}")
                    else:
                        logging.info(f"Log {log_id} complete (EOF marker received).")
                        return True
                else:
                    logging.info(f"Received non-bytearray message: {item}")
    except Exception as e:
        logging.error(f"Error fetching log {log_id}: {e}")
        return False

# -----------------------------------------------------------------------------
# Main BLE client routine
# -----------------------------------------------------------------------------
async def run_ble_client(end_of_serial: str, queue: asyncio.Queue):
    # Discover the sensor.
    devices = await discover()
    found = False
    address = None
    name = None
    for d in devices:
        print("device:", d)
        if d.name and d.name.endswith(end_of_serial):
            print("device found")
            address = d.address
            name = d.name
            found = True
            break

    # Create an event that will be set on disconnect or SIGINT.
    disconnected_event = asyncio.Event()

    def raise_graceful_exit(*args):
        disconnected_event.set()

    def disconnect_callback(client):
        logging.info("Disconnected callback called!")
        disconnected_event.set()

    if found:
        async with BleakClient(address, disconnected_callback=disconnect_callback) as client:
            # Set signal handlers.
            signal.signal(signal.SIGINT, raise_graceful_exit)
            signal.signal(signal.SIGTERM, raise_graceful_exit)
           
            logging.info("Enabling notifications")
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID,
                                      lambda s, d: asyncio.create_task(notification_handler(s, d, queue)))

            # Instead of stopping at the first log missing,
            # we allow a few consecutive misses before quitting.
            current_log_id = 1
            consecutive_misses = 0
            max_consecutive_misses = 1  # Adjust as needed

            while not disconnected_event.is_set() and consecutive_misses < max_consecutive_misses:
                success = await fetch_log(client, queue, name, current_log_id, disconnected_event)
                if success:
                    logging.info(f"Successfully fetched log {current_log_id}")
                    consecutive_misses = 0  # reset on success
                else:
                    logging.info(f"No data received for log {current_log_id}.")
                    consecutive_misses += 1
                current_log_id += 1
                # Brief pause between log fetches.
                await asyncio.sleep(0.5)

            # --- NEW RESET SEQUENCE ---
            # Instead of immediately unsubscribing, send a reset command to the sensor.
            # We use the HELLO command (command code 0) with client reference 101.
            # This should prompt the sensor to reinitialize its internal state.
            hello_cmd = bytearray([0, 101])
            logging.info(f"Sending HELLO command to reset sensor state: {hello_cmd.hex()}")
            try:
                await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, hello_cmd, response=True)
            except Exception as e:
                logging.error(f"Error sending HELLO reset command: {e}")
            # Allow time for the sensor to process the reset.
            await asyncio.sleep(2.0)
            # --- END NEW RESET SEQUENCE ---

            if client.is_connected:
                logging.info("Unsubscribe and stopping notifications")
                # Optionally, you could comment these out if you want to maintain subscriptions.
                #await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([2, 99]), response=True)
                #await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
            
            # Signal termination.
            await queue.put(None)
            await asyncio.sleep(1.0)
    else:
        await queue.put(None)
        print("Sensor with ending", end_of_serial, "not found!")

# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------
async def main(end_of_serial: str):
    queue = asyncio.Queue()
    # Run only the BLE client task (which handles log extraction)
    await run_ble_client(end_of_serial, queue)
    logging.info("Main method done.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python fetch_logbook_data.py <end_of_sensor_name>")
        sys.exit(1)
    end_of_serial = sys.argv[1]
    asyncio.run(main(end_of_serial))