# -*- coding: utf-8 -*-
"""
Python gatt_sensordata_app client example using the Bleak GATT client.

This example is based on the examples in the Bleak repo: https://github.com/hbldh/bleak
"""

import logging
import asyncio
import platform
import signal
from bleak import BleakClient
from bleak import _logger as logger
from bleak import BleakScanner
from functools import reduce
import struct
import sys
import argparse

f_output = None

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO)

WRITE_CHARACTERISTIC_UUID = (
    "34800001-7185-4d5d-b431-630e7050e8f0"
)

NOTIFY_CHARACTERISTIC_UUID = (
    "34800002-7185-4d5d-b431-630e7050e8f0"
)

# MEAS_RESOURCE_TO_SUBSCRIBE = "/Meas/Acc/13"
MEAS_RESOURCE_TO_SUBSCRIBE = "/Meas/ECG/200"
SAMPLE_RATE = 200

GATT_CMD_HELLO = 0
GATT_CMD_SUBSCRIBE = 1
GATT_CMD_UNSUBSCRIBE = 2
GATT_CMD_FETCH_OFFLINE_DATA = 3
GATT_CMD_INIT_OFFLINE = 4

# https://stackoverflow.com/a/56243296
class DataView:
    def __init__(self, array, bytes_per_element=1):
        """
        bytes_per_element is the size of each element in bytes.
        By default we are assume the array is one byte per element.
        """
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
        return struct.unpack('<I', binary)[0]  # < for little endian

    def get_int_32(self, start_index):
        bytes_to_read = 4
        binary = self.__get_binary(start_index, bytes_to_read)
        return struct.unpack('<i', binary)[0]  # < for little endian

    def get_float_32(self, start_index):
        bytes_to_read = 4
        binary = self.__get_binary(start_index, bytes_to_read)
        return struct.unpack('<f', binary)[0]  # <f for little endian

    def length(self):
        return len(self.array)


async def run_queue_consumer(queue: asyncio.Queue):
    
    first_sample = True
    while True:
        data = await queue.get()
        if data is None:
            logger.info("Got message from client about disconnection. Exiting consumer loop...")
            break
        
        if isinstance(data, dict):
            # Data notification from stream or offline. unpack multisamples
            if first_sample:
                print("timestamp;ECG", file=f_output)
                first_sample = False

            if data["type"] == "ECG":
                timestamp = data["timestamp"]
                logger.info("received ECG. ts: " + str(timestamp) +", " + str(data["samples"][0]))

                dt = int(1000/SAMPLE_RATE)
                for sample in data["samples"]:
                    print("%d;%d" % (timestamp, sample), file=f_output)
                    timestamp += dt

        else:
            logger.info("received: " + str(data))


async def run_ble_client(end_of_serial: str, queue: asyncio.Queue):

    # Check the device is available
    devices = await BleakScanner.discover(timeout = 5)
    found = False
    address = None
    for d in devices:
        print("device:", d)
        if d.name and d.name.endswith(end_of_serial):
            print("device found")
            address = d.address
            found = True
            break

    # This event is set if device disconnects or ctrl+c is pressed
    disconnected_event = asyncio.Event()
    current_ble_client = None
    loop = asyncio.get_event_loop()
    
    def raise_graceful_exit(*args):
        disconnected_event.set()

    def disconnect_callback(client):
        logger.info("Disconnected callback called!")
        disconnected_event.set()

    async def call_disconnect():
        await current_ble_client.disconnect()

    def disconnect_ble(*args):
        logger.info("Disconnect BLE (SIGQUIT)")
        if current_ble_client:
            asyncio.create_task(call_disconnect())

    async def handle_ecg_data_notification(payload):
        # Dig data from the binary
        ts = payload.get_uint_32(0)
        
        # data is 16 samples of uint32
        data = {}
        data["type"] = "ECG"
        data["timestamp"] = ts
        data["samples"] = []
        for i in range(16):
            sample = payload.get_int_32(4+i*4)
            data["samples"].append(sample)
        # queue message for later consumption
        await queue.put(data)

    async def handle_default_data_notification(payload):
        msg = "Unknown data: {} bytes".format(payload.length())
        await queue.put(msg)

    async def handle_command_response(payload):
        print("Command response:", payload.array)

    async def send_gatt_command(command_code, client_reference, payload = bytearray([])):
        await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, 
            bytearray([command_code, client_reference])+payload, response=True)



    async def incoming_data_handler(sender, data):
        d = DataView(data)

        response_code = d.get_uint_8(0);
        reference_id =  d.get_uint_8(1);

        payload = DataView(d.array[2:])
        logger.info("incoming_data_handler: response: %d, reference_id: %d" % (response_code, reference_id))
        if response_code == 1:
            # COMMAND_RESPONSE
            await handle_command_response(payload)
        elif response_code == 2:
            # DATA
            if payload.length() == 68:
                await handle_ecg_data_notification(payload)
            else:
                await handle_default_data_notification(payload)
        else:
            print("unknown response code:", response_code)


    if not found:
        # Signal consumer to exit
        await queue.put(None)
        print("Sensor  ******" + end_of_serial, "not found!")

    else:
        #RECONNECT_DELAY = 4 # in 10s increments
        RECONNECT_DELAY = None # in 10s increments

        # Add signal handler for ctrl+c
        signal.signal(signal.SIGINT, raise_graceful_exit)
        signal.signal(signal.SIGTERM, raise_graceful_exit)
        
        exitProgram = False
        reconnect = False
        while not exitProgram:
            # Clear disconnect event
            disconnected_event.clear()

            # Create a new bleakclient (= connect to device)
            connect_timeout = 30
            if reconnect:
                if RECONNECT_DELAY:
                    for i in range(RECONNECT_DELAY):
                        logger.info("Sleeping. {} seconds left.".format((RECONNECT_DELAY-i)*10))
                        await asyncio.sleep(10)

                connect_timeout = 9999999999

            async with BleakClient(address, disconnected_callback=disconnect_callback, timeout = connect_timeout) as client:

                current_ble_client = client
                
                await client.connect()
                logger.info("BLE connected")

                # Start notifications and subscribe to acceleration @ 13Hz
                logger.info("Enabling notifications")
                await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, incoming_data_handler)
                logger.info("Subscribing datastream")
                await send_gatt_command(GATT_CMD_SUBSCRIBE, 99, bytearray(MEAS_RESOURCE_TO_SUBSCRIBE, "utf-8"))

                if reconnect:
                    # in case of reconnect, fetch the offline data after waiting awhile
                    await asyncio.sleep(2)
                    logger.info("Fetching offline data")
                    await send_gatt_command(GATT_CMD_FETCH_OFFLINE_DATA, 123)
                else:
                    # First round, clean logbook
                    await send_gatt_command(GATT_CMD_INIT_OFFLINE, 124)


                # Run until disconnect event is set
                await disconnected_event.wait()
                logger.info(
                    "Disconnect set by ctrl+c or real disconnect event. Check Status:")

                # Check the conection status to infer if the device disconnected or crtl+c was pressed
                status = client.is_connected
                logger.info("Connected: {}".format(status))

                # If status is connected, unsubscribe and stop notifications
                if status:
                    logger.info("Client still connected, exit was caused by Ctrl-C")                    
                    logger.info("Unsubscribe")
                    await send_gatt_command(GATT_CMD_UNSUBSCRIBE, 99)
                    await asyncio.sleep(1)
                    logger.info("Stop notifications")
                    await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
                    exitProgram = True
                
                    # Signal consumer to exit
                    await queue.put(None)
                else:
                    # This was caused by disconnect. next round in while is reconnecting
                    reconnect = True

                await asyncio.sleep(0.1)





async def main(args):
    global f_output

    if args.output:
        f_output = open(args.output, "wt")

    try:
        queue = asyncio.Queue()
        client_task = run_ble_client(args.end_of_serial, queue)
        consumer_task = run_queue_consumer(queue)
        await asyncio.gather(client_task, consumer_task)    
        logger.info("Main method done.")
    finally:
        if f_output:
            f_output.close()

if __name__ == "__main__":

    logging.basicConfig(level=logging.INFO)

 
    # Initialize parse
    parser = argparse.ArgumentParser()
    
    # Adding optional argument
    parser.add_argument("end_of_serial", help = "end of serialnumber of the sensor you want to connect")
    parser.add_argument("-o", "--output", help = "Output filename or path")
    
    # Read arguments from command line
    args = parser.parse_args()

    asyncio.run(main(args))