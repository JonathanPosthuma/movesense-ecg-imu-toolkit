import logging
import asyncio
import platform
import signal
from bleak import BleakClient, BleakScanner
from bleak import _logger as logger
from functools import reduce
import struct
import json

# UUIDs for the Movesense device
WRITE_CHARACTERISTIC_UUID = "6b200001-ff4e-4979-8186-fb7ba486fcd7"
NOTIFY_CHARACTERISTIC_UUID = "6b200002-ff4e-4979-8186-fb7ba486fcd7"
MOVESENSE_SERVICE_UUID = "0000fdf3-0000-1000-8000-00805f9b34fb"

class DataView:
    def __init__(self, array, bytes_per_element=1):
        self.array = array
        self.bytes_per_element = 1

    def __get_binary(self, start_index, byte_count, signed=False):
        integers = [self.array[start_index + x] for x in range(byte_count)]
        bytes = [integer.to_bytes(
            self.bytes_per_element, byteorder='little', signed=signed) for integer in integers]
        return reduce(lambda a, b: a + b, bytes)

    def get_uint_16(self, start_index):
        bytes_to_read = 2
        return int.from_bytes(self.__get_binary(start_index, bytes_to_read), byteorder='little')

    def get_uint_8(self, start_index):
        bytes_to_read = 1
        return int.from_bytes(self.__get_binary(start_index, bytes_to_read), byteorder='little')

    def get_uint_32(self, start_index):
        bytes_to_read = 4
        binary = self.__get_binary(start_index, bytes_to_read)
        return struct.unpack('<I', binary)[0]  # <f for little endian

    def get_float_32(self, start_index):
        bytes_to_read = 4
        binary = self.__get_binary(start_index, bytes_to_read)
        return struct.unpack('<f', binary)[0]  # <f for little endian

f_log = None

async def run_queue_consumer(queue: asyncio.Queue):
    global f_log
    while True:
        data = await queue.get()
        logger.info("consumer for data: " + str(type(data)))
        if data is None:
            logger.info("Got message from client about disconnection. Exiting consumer loop...")
            break
        if isinstance(data, bytearray):
            logger.info("Got bytearray. Assume logbook data")
            dv = DataView(data)
            offset = dv.get_uint_32(0)
            bytes_to_write = bytearray(data[4:])
            if len(bytes_to_write) > 0:
                f_log.seek(offset)
                f_log.write(bytes_to_write)
            else:
                logger.info("File end marker received, closing file.")
                f_log.close()
        else:
            logger.info("received: " + data)

async def run_ble_client(end_of_serial: str, queue: asyncio.Queue):
    global f_log

    print(f"Starting scan for device with serial suffix: {end_of_serial}")
    devices = await BleakScanner.discover()
    found = False
    address = None
    name = None
    for d in devices:
        print(f"Discovered device: {d.address} - {d.name}")
        if d.name and d.name.endswith(end_of_serial):
            print(f"Found device with matching serial suffix: {d.name} at {d.address}")
            address = d.address
            name = d.name
            found = True
            break

    if not found:
        print(f"Sensor with serial {end_of_serial} not found!")
        await queue.put(None)
        return

    # Timeout configuration for connection
    timeout_seconds = 10

    # This event is set if the device disconnects or ctrl+c is pressed
    disconnected_event = asyncio.Event()

    def raise_graceful_exit(*args):
        disconnected_event.set()

    def disconnect_callback(client):
        logger.info("Disconnected callback called!")
        disconnected_event.set()

    async def notification_handler(sender, data):
        """Simple notification handler which processes data received."""
        print(f"Notification received from {sender}: {data}")
        d = DataView(data)
        response = d.get_uint_8(0)
        reference = d.get_uint_8(1)

        # Data or Data_part2
        if response == 2 or response == 3:
            await queue.put(d.array[2:])
        else:
            msg = f"Data: offset: {d.get_uint_32(2)}, len: {len(d.array)}"
            await queue.put(msg)

    try:
        print(f"Attempting to connect to {name} at {address} (timeout: {timeout_seconds} seconds)")
        async with BleakClient(address, disconnected_callback=disconnect_callback, timeout=timeout_seconds) as client:
            print(f"Connected to {name} at {address}")
            logger.info(f"Connected to {name} ({address})")

            # Avoid signal issues on Windows
            if platform.system() != "Windows":
                signal.signal(signal.SIGINT, raise_graceful_exit)
                signal.signal(signal.SIGTERM, raise_graceful_exit)

            print("Enabling notifications...")
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, notification_handler)

            f_log = open(f'log_1_{name}.sbem', 'wb')

            # Construct the JSON-like command payload as a string
            payload = {"newState": 3}
            payload_bytes = json.dumps(payload).encode('utf-8')

            print(f"Sending start logging command: {payload}")
            await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, payload_bytes, response=True)

            print("Waiting for notifications...")
            await disconnected_event.wait()
            print("Disconnect event detected, checking status...")

            if client.is_connected:
                print("Unsubscribing from notifications...")
                await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([2, 99]), response=True)
                print("Stopping notifications...")
                await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)

            await queue.put(None)
            await asyncio.sleep(1.0)

    except asyncio.TimeoutError:
        print(f"Connection to {name} timed out after {timeout_seconds} seconds.")
        await queue.put(None)

    except Exception as e:
        print(f"An error occurred: {e}")
        await queue.put(None)

async def main(end_of_serial: str, output_dir: str):
    queue = asyncio.Queue()
    print(f"Starting BLE data extraction for sensor with serial suffix: {end_of_serial}")
    print(f"Output will be saved to: {output_dir}")
    client_task = run_ble_client(end_of_serial, queue)
    consumer_task = run_queue_consumer(queue)
    await asyncio.gather(client_task, consumer_task)
    logging.info("Main method done.")
