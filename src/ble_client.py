 # src/ble_client.py
import logging
import asyncio
import platform
import signal
from bleak import BleakClient
from bleak import _logger as logger
from bleak import discover
from functools import reduce
import struct
import sys

WRITE_CHARACTERISTIC_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"
NOTIFY_CHARACTERISTIC_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"

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
            logger.info(
                "Got message from client about disconnection. Exiting consumer loop..."
            )
            break
        if isinstance(data, bytearray):
            logger.info( "Got bytearray. assume logbook data" )
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
    # Check the device is available
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

    # This event is set if device disconnects or ctrl+c is pressed
    disconnected_event = asyncio.Event()

    def raise_graceful_exit(*args):
        disconnected_event.set()

    def disconnect_callback(client):
        logger.info("Disconnected callback called!")
        disconnected_event.set()

    async def notification_handler(sender, data):
        """Simple notification handler which prints the data received."""
        d = DataView(data)
        response = d.get_uint_8(0)
        reference = d.get_uint_8(1)

        # Data or Data_part2
        if (response == 2 or response == 3):
            # send data part to processor    
            await queue.put(d.array[2:])
        else:
            msg = "Data: offset: {}, len: {}".format(d.get_uint_32(2), len(d.array))
            await queue.put(msg)

    if found:
        async with BleakClient(address, disconnected_callback=disconnect_callback) as client:
            loop = asyncio.get_event_loop()
            signal.signal(signal.SIGINT, raise_graceful_exit)
            signal.signal(signal.SIGTERM, raise_graceful_exit)
           
            logger.info("Enabling notifications")
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, notification_handler)

            f_log = open('log_1_' + name + '.sbem', 'wb')

            logger.info("Fetching log id 1")
            await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([3, 101, 1, 0, 0, 0]), response=True)

            await disconnected_event.wait()
            logger.info("Disconnect set by ctrl+c or real disconnect event. Check Status:")

            status = client.is_connected
            logger.info("Connected: {}".format(status))

            if status:
                logger.info("Unsubscribe")
                await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, bytearray([2, 99]), response=True)
                logger.info("Stop notifications")
                await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
            
            await queue.put(None)
            await asyncio.sleep(1.0)
    else:
        await queue.put(None)
        print("Sensor  ******" + end_of_serial + " not found!")

async def main(end_of_serial: str):
    queue = asyncio.Queue()
    client_task = run_ble_client(end_of_serial, queue)
    consumer_task = run_queue_consumer(queue)
    await asyncio.gather(client_task, consumer_task)
    logging.info("Main method done.")