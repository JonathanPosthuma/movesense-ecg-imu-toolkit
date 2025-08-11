import asyncio
import logging
from bleak import BleakClient, BleakScanner
import signal

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVICE_UUID = "34802252-7185-4d5d-b431-630e7050e8f0"
COMMAND_CHAR_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"  # Command characteristic (Write)
DATA_CHAR_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"  # Data characteristic (Notify)

# GATT Commands
GATT_CMD_FETCH_OFFLINE_DATA = 3
GATT_CMD_EXTRA_REQUEST = 1  # New command to add

async def run_ble_client(device_name_end):
    """
    Connects to a Movesense BLE device, sends FETCH_OFFLINE_DATA and EXTRA_REQUEST commands, 
    and handles data notifications with a reconnection mechanism.
    """
    data_buffer = bytearray()
    received_notifications = 0
    disconnected_event = asyncio.Event()

    def handle_disconnect(client):
        logger.info("Device disconnected.")
        disconnected_event.set()

    async def incoming_data_handler(sender, data):
        nonlocal received_notifications
        received_notifications += 1
        logger.info(f"Notification received from {sender}: {data}")
        data_buffer.extend(data)  # Accumulate data in the buffer

    async def send_gatt_command(client, command_code, reference=1):
        command = bytes([command_code, reference])
        await client.write_gatt_char(COMMAND_CHAR_UUID, command, response=True)
        logger.info(f"Sent GATT command with code {command_code} and reference {reference}")

    async def connect_and_fetch_data():
        nonlocal received_notifications
        devices = await BleakScanner.discover(timeout=5)
        address = next((d.address for d in devices if d.name and d.name.endswith(device_name_end)), None)

        if not address:
            logger.error(f"Device with name ending '{device_name_end}' not found.")
            return

        logger.info(f"Device found with address: {address}")

        async with BleakClient(address, disconnected_callback=handle_disconnect) as client:
            if not client.is_connected:
                logger.error("Failed to connect.")
                return

            logger.info("Connected to device.")
            try:
                # Start notifications
                await client.start_notify(DATA_CHAR_UUID, incoming_data_handler)
                logger.info("Notifications enabled")

                # Send EXTRA_REQUEST command
                await send_gatt_command(client, GATT_CMD_EXTRA_REQUEST, 1)
                logger.info("Sent EXTRA_REQUEST command")

                # Send FETCH_OFFLINE_DATA command
                await send_gatt_command(client, GATT_CMD_FETCH_OFFLINE_DATA, 1)
                logger.info("Sent FETCH_OFFLINE_DATA command")

                # Wait for notifications to accumulate
                await asyncio.wait_for(disconnected_event.wait(), timeout=15)

                # Only stop notifications if still connected
                if client.is_connected:
                    await client.stop_notify(DATA_CHAR_UUID)
                    logger.info("Stopped notifications")

            except asyncio.TimeoutError:
                logger.warning("No notifications received within timeout period.")
            except Exception as e:
                logger.error(f"Error during data fetch: {e}")

    # Run the data fetch with a retry mechanism
    retries = 3
    for attempt in range(1, retries + 1):
        logger.info(f"Attempt {attempt} to fetch offline data...")
        disconnected_event.clear()
        await connect_and_fetch_data()
        
        if received_notifications > 0:
            logger.info(f"Data successfully received with {received_notifications} notifications.")
            break
        elif attempt < retries:
            logger.warning("No notifications received. Retrying...")
            await asyncio.sleep(2)  # Wait before retrying
        else:
            logger.error("Failed to receive any notifications after multiple attempts.")

    # Final report on received data
    if data_buffer:
        logger.info(f"Total data received: {len(data_buffer)} bytes")
        print("Data received:", data_buffer)
    else:
        logger.info("No data received from device.")

async def main():
    device_name_end = "000077"  # Update this to match the last digits of your device's name
    await run_ble_client(device_name_end)

if __name__ == "__main__":
    asyncio.run(main())