import logging
import asyncio
import platform
import signal
from bleak import BleakClient, BleakScanner
import struct
import argparse
from functools import reduce

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# UUIDs for the Movesense device
WRITE_CHARACTERISTIC_UUID = "6b200001-ff4e-4979-8186-fb7ba486fcd7"
NOTIFY_CHARACTERISTIC_UUID = "6b200002-ff4e-4979-8186-fb7ba486fcd7"

# Constants for GATT commands
GATT_CMD_SUBSCRIBE = 1
GATT_CMD_UNSUBSCRIBE = 2
GATT_CMD_FETCH_OFFLINE_DATA = 3
GATT_CMD_INIT_OFFLINE = 4

# ECG sampling
MEAS_RESOURCE_TO_SUBSCRIBE = "/Meas/ECG/200"
SAMPLE_RATE = 200

f_output = None

class DataView:
    """Helper class to manage binary data interpretation."""
    def __init__(self, array, bytes_per_element=1):
        self.array = array
        self.bytes_per_element = bytes_per_element

    def __get_binary(self, start_index, byte_count, signed=False):
        integers = [self.array[start_index + x] for x in range(byte_count)]
        bytes = [integer.to_bytes(self.bytes_per_element, byteorder='little', signed=signed) for integer in integers]
        return reduce(lambda a, b: a + b, bytes)

    def get_uint_16(self, start_index):
        return int.from_bytes(self.__get_binary(start_index, 2), byteorder='little')

    def get_uint_8(self, start_index):
        return int.from_bytes(self.__get_binary(start_index, 1), byteorder='little')

    def get_uint_32(self, start_index):
        return struct.unpack('<I', self.__get_binary(start_index, 4))[0]

    def get_int_32(self, start_index):
        return struct.unpack('<i', self.__get_binary(start_index, 4))[0]

    def length(self):
        return len(self.array)

async def run_queue_consumer(queue: asyncio.Queue):
    """Consumer task to process the received ECG data."""
    first_sample = True
    while True:
        data = await queue.get()
        if data is None:
            logger.info("Exiting consumer loop...")
            break
        
        if isinstance(data, dict) and data["type"] == "ECG":
            if first_sample:
                print("timestamp;ECG", file=f_output)
                first_sample = False

            timestamp = data["timestamp"]
            dt = int(1000 / SAMPLE_RATE)
            for sample in data["samples"]:
                print(f"{timestamp};{sample}", file=f_output)
                timestamp += dt

        else:
            logger.info(f"received: {data}")

async def send_gatt_command(client, command_code, client_reference, payload=bytearray()):
    """Send GATT command to the device."""
    await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([command_code, client_reference]) + payload, response=True)

async def run_ble_client(end_of_serial: str, queue: asyncio.Queue):
    """Main BLE client logic, connecting to the sensor and handling data notifications."""
    # Discover devices
    devices = await BleakScanner.discover(timeout=5)
    address = None
    for d in devices:
        if d.name and d.name.endswith(end_of_serial):
            logger.info(f"Found device: {d.name} at {d.address}")
            address = d.address
            break
    
    if not address:
        logger.error(f"Sensor with serial {end_of_serial} not found!")
        await queue.put(None)
        return

    disconnected_event = asyncio.Event()

    def disconnect_callback(client):
        logger.info("Disconnected callback called!")
        disconnected_event.set()

    async def incoming_data_handler(sender, data):
        d = DataView(data)
        response_code = d.get_uint_8(0)
        reference_id = d.get_uint_8(1)
        payload = DataView(d.array[2:])

        logger.info(f"Received data: response_code={response_code}, reference_id={reference_id}")
        if response_code == 2 and payload.length() == 68:
            # ECG Data
            ts = payload.get_uint_32(0)
            samples = [payload.get_int_32(4 + i * 4) for i in range(16)]
            await queue.put({"type": "ECG", "timestamp": ts, "samples": samples})

    async with BleakClient(address, disconnected_callback=disconnect_callback) as client:
        logger.info("Connected to the device")

        # Start notifications
        await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, incoming_data_handler)
        logger.info("Notifications enabled")

        # Subscribe to ECG data
        await send_gatt_command(client, GATT_CMD_SUBSCRIBE, 99, bytearray(MEAS_RESOURCE_TO_SUBSCRIBE, 'utf-8'))

        # Wait for disconnection event or Ctrl-C
        await disconnected_event.wait()

        # Unsubscribe and stop notifications before disconnecting
        await send_gatt_command(client, GATT_CMD_UNSUBSCRIBE, 99)
        await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)

async def main(args):
    global f_output

    if args.output:
        f_output = open(args.output, "wt")

    try:
        queue = asyncio.Queue()
        client_task = run_ble_client(args.end_of_serial, queue)
        consumer_task = run_queue_consumer(queue)
        await asyncio.gather(client_task, consumer_task)
    finally:
        if f_output:
            f_output.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("end_of_serial", help="End of serial number of the sensor")
    parser.add_argument("-o", "--output", help="Output filename or path")
    args = parser.parse_args()

    asyncio.run(main(args))