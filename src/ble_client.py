import logging
import asyncio
from bleak import BleakClient, BleakScanner
import struct
import argparse

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# UUIDs for the Movesense device
WRITE_CHARACTERISTIC_UUID = "6b200001-ff4e-4979-8186-fb7ba486fcd7"
NOTIFY_CHARACTERISTIC_UUID = "6b200002-ff4e-4979-8186-fb7ba486fcd7"

# Constants for GATT commands
GATT_CMD_FETCH_OFFLINE_DATA = 3
GATT_CMD_INIT_OFFLINE = 4

# Define Responses based on server's enum
class Responses:
    COMMAND_RESULT = 1
    DATA = 2
    DATA_PART2 = 3
    DATA_PART3 = 4

# Define a data structure to accumulate data parts
class DataAccumulator:
    def __init__(self):
        self.data_parts = {}
        self.expected_parts = 1  # Adjust based on protocol
        self.received_parts = 0

    def add_part(self, part_type, data):
        self.data_parts[part_type] = data
        self.received_parts += 1

    def is_complete(self):
        return self.received_parts >= self.expected_parts

    def get_complete_data(self):
        # Implement reassembly logic based on part types
        complete_data = b''
        for part in sorted(self.data_parts.keys()):
            complete_data += self.data_parts[part]
        return complete_data

async def run_queue_consumer(queue: asyncio.Queue, f_output):
    """Consumer task to process the received ECG data."""
    first_sample = True
    data_accumulator = DataAccumulator()

    while True:
        data = await queue.get()
        if data is None:
            logger.info("Exiting consumer loop...")
            break

        if isinstance(data, dict):
            if data["type"] == "ECG":
                if first_sample:
                    print("timestamp;ECG", file=f_output)
                    first_sample = False

                timestamp = data["timestamp"]
                dt = int(1000 / SAMPLE_RATE)
                for sample in data["samples"]:
                    print(f"{timestamp};{sample}", file=f_output)
                    timestamp += dt

            elif data["type"] in ["DATA_PART2", "DATA_PART3"]:
                # Accumulate data parts
                data_accumulator.add_part(data["type"], data["payload"])

                if data_accumulator.is_complete():
                    complete_data = data_accumulator.get_complete_data()
                    # Process complete_data as needed
                    # For example, write to file or further processing
                    data_accumulator = DataAccumulator()  # Reset for next file

            else:
                logger.info(f"Unhandled data type: {data['type']}")

        else:
            logger.info(f"received: {data}")

async def incoming_data_handler(sender, data, queue: asyncio.Queue):
    """Handle incoming BLE data notifications."""
    d = DataView(data)
    response_code = d.get_uint_8(0)
    reference_id = d.get_uint_8(1)
    payload = DataView(d.array[2:])

    logger.info(f"Received data: response_code={response_code}, reference_id={reference_id}")

    if response_code == Responses.DATA:
        # Main data packet
        ts = payload.get_uint_32(0)
        samples = [payload.get_int_32(4 + i * 4) for i in range(16)]
        await queue.put({"type": "ECG", "timestamp": ts, "samples": samples})

    elif response_code in [Responses.DATA_PART2, Responses.DATA_PART3]:
        # Additional data parts
        await queue.put({"type": f"DATA_PART{response_code - Responses.DATA}", "payload": payload.array})

    elif response_code == Responses.COMMAND_RESULT:
        # Handle command results if necessary
        logger.info(f"Command Result received with reference ID: {reference_id}")

    else:
        logger.info(f"Unhandled response code: {response_code}")

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

    async with BleakClient(address, disconnected_callback=disconnect_callback) as client:
        logger.info("Connected to the device")

        # Start notifications
        await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, lambda sender, data: asyncio.create_task(incoming_data_handler(sender, data, queue)))
        logger.info("Notifications enabled")

        # Send FETCH_OFFLINE_DATA command if required
        await send_gatt_command(client, GATT_CMD_FETCH_OFFLINE_DATA, 99)
        # Implement MEAS_RESOURCE_TO_SUBSCRIBE if needed or remove

        # Wait for disconnection event or Ctrl-C
        await disconnected_event.wait()

        # Stop notifications before disconnecting
        await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)

async def main(args):
    global f_output

    if args.output:
        f_output = open(args.output, "wt")
    else:
        f_output = None

    try:
        queue = asyncio.Queue()
        client_task = run_ble_client(args.end_of_serial, queue)
        consumer_task = run_queue_consumer(queue, f_output)
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