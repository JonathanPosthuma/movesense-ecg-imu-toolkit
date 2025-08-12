#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Movesense log extraction client (multiple sensors).

This client:
  1. For each sensor (identified by the end of its name) in a provided list,
     it discovers the sensor.
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


import os
import asyncio
import logging
import struct
import sys
from functools import reduce
from datetime import datetime

from bleak import BleakClient, discover

from enum import IntEnum

class Commands(IntEnum):
    HELLO         = 0
    SUBSCRIBE     = 1
    UNSUBSCRIBE   = 2
    FETCH_LOG     = 3
    INIT_OFFLINE  = 4
    GET_LOG_COUNT = 5
    STOP_LOGGING  = 6

# -----------------------------------------------------------------------------
# STOP_LOGGING Command Helper
# -----------------------------------------------------------------------------
async def send_stop_logging(client: BleakClient, reference: int = 101):
    """Send STOP_LOGGING to sensor."""
    cmd = bytearray([Commands.STOP_LOGGING, reference])
    logging.info(f"→ STOP_LOGGING ({cmd.hex()})")
    try:
        await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, cmd, response=True)
        logging.info("STOP_LOGGING acknowledged by sensor")
    except Exception as e:
        logging.error(f"Failed to send STOP_LOGGING: {e}")

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
    Strips off the first two bytes and puts the remaining bytes on the shared queue.
    """
    await queue.put(data[2:])

# -----------------------------------------------------------------------------
# Fetch a single log file (modified to use raw_folder and new naming format)
# -----------------------------------------------------------------------------
async def fetch_log(client: BleakClient, queue: asyncio.Queue, sensor_id: str,
                    log_id: int, disconnect_event: asyncio.Event, raw_folder: str) -> bool:
    # Generate the current timestamp in the desired format: HHMMSSDDMMYYYY
    timestamp = datetime.now().strftime("%H%M%S%d%m%Y")
    filename = os.path.join(raw_folder, f"{timestamp}_{sensor_id}_{log_id}.sbem")
    logging.info(f"Fetching log {log_id} -> '{filename}'")
    try:
        with open(filename, 'wb') as f:
            command = bytearray([3, 101, log_id, 0, 0, 0])
            logging.info(f"Sending FETCH_LOG command for log {log_id}: {command.hex()}")
            await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, command, response=True)
            
            while not disconnect_event.is_set():
                try:
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
                        #logging.info(f"Log {log_id}: wrote {len(payload)} bytes at offset {offset}")
                    else:
                        logging.info(f"Log {log_id} complete (EOF marker received).")
                        return True
                else:
                    logging.info(f"Received non-bytearray message: {item}")
    except Exception as e:
        logging.error(f"Error fetching log {log_id}: {e}")
        return False

# -----------------------------------------------------------------------------
# Main BLE client routine for a single sensor (modified to use raw_folder)
# -----------------------------------------------------------------------------
async def run_ble_client(end_of_serial: str, queue: asyncio.Queue, raw_folder: str) -> bool:
    devices = await discover()
    found = False
    address = None
    name = None
    for d in devices:
        logging.info(f"Found device: {d}")
        if d.name and d.name.endswith(end_of_serial):
            logging.info("Sensor found")
            address = d.address
            name = d.name
            found = True
            break

    disconnected_event = asyncio.Event()

    def disconnect_callback(client):
        logging.info("Disconnected callback called!")
        disconnected_event.set()

    if found:
        success_flag = False
        async with BleakClient(address, disconnected_callback=disconnect_callback) as client:
            logging.info("Enabling notifications")
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID,
                                      lambda s, d: asyncio.create_task(notification_handler(s, d, queue)))


            current_log_id = 1
            consecutive_misses = 0
            max_consecutive_misses = 1  # Adjust as needed

            while not disconnected_event.is_set() and consecutive_misses < max_consecutive_misses:
                success = await fetch_log(client, queue, name, current_log_id, disconnected_event, raw_folder)
                if success:
                    logging.info(f"Successfully fetched log {current_log_id}")
                    consecutive_misses = 0  # reset on success
                    success_flag = True  # Mark that at least one log was successfully extracted
          
                else:
                    logging.info(f"No data received for log {current_log_id}.")
                    consecutive_misses += 1
                current_log_id += 1
                await asyncio.sleep(0.5)

            # --- NEW POWER-OFF SEQUENCE ---
            # Instead of just resetting, send a full power-off command sequence.
            hello_cmd = bytearray([0, 101])
            logging.info(f"Sending HELLO command to reset sensor state: {hello_cmd.hex()}")
            try:
                await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, hello_cmd, response=True)
            except Exception as e:
                logging.error(f"Error sending wakeup command: {e}")
            await asyncio.sleep(2.0)

            # Optionally, unsubscribe from notifications.
            if client.is_connected:
                logging.info("Unsubscribing and stopping notifications")
            
            await queue.put(None)
            await asyncio.sleep(1.0)
        return success_flag
    else:
        await queue.put(None)
        logging.error(f"Sensor with ending '{end_of_serial}' not found!")
        return False

# -----------------------------------------------------------------------------
# Extract logs for a single sensor (wrapper, now accepts raw_folder)
# -----------------------------------------------------------------------------
async def extract_sensor(sensor_id: str, raw_folder: str) -> bool:
    queue = asyncio.Queue()
    logging.info(f"Starting extraction for sensor with ending '{sensor_id}'")
    result = await run_ble_client(sensor_id, queue, raw_folder)
    logging.info(f"Extraction finished for sensor with ending '{sensor_id}' with result: {result}")
    return result

# -----------------------------------------------------------------------------
# Extract logs for a list of sensors sequentially
# -----------------------------------------------------------------------------
async def extract_all_sensors(sensor_list, raw_folder: str):
    for sensor_id in sensor_list:
        logging.info(f"--- Processing sensor with ending '{sensor_id}' ---")
        await extract_sensor(sensor_id, raw_folder)
        await asyncio.sleep(1)

# -----------------------------------------------------------------------------
# Main entry point (for command-line testing)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("Usage: python extractor.py <end_of_sensor_name> <raw_folder>")
        sys.exit(1)
    end_of_serial = sys.argv[1]
    raw_folder = sys.argv[2]
    asyncio.run(extract_all_sensors([end_of_serial], raw_folder))