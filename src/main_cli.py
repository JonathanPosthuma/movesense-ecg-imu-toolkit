import asyncio
import sys
from ble_client import main  

async def run_ble_extraction():
    if len(sys.argv) < 3:
        print("Usage: python main_cli.py <serial_suffix> <output_directory>")
        sys.exit(1)

    serial_suffix = sys.argv[1]
    output_directory = sys.argv[2]

    print(f"Starting BLE data extraction for sensor with serial suffix: {serial_suffix}")
    print(f"Output will be saved to: {output_directory}")

    # Run the BLE extraction task
    await main(serial_suffix, output_directory)

if __name__ == "__main__":
    asyncio.run(run_ble_extraction())
