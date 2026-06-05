#!/usr/bin/env python3
"""
parse_new_sbem_to_csv_with_length.py

This script searches a target folder for all .sbem files, parses each file,
and saves the parsed data as CSV.

It handles the following chunk types based solely on their length:
  - 52 bytes: IMU packet (new IMU6 format)
  - 76 bytes: ECG packet (4-byte timestamp, 4-byte ARRAY_BEGIN, 16 x int32 samples, 4-byte ARRAY_END)

Usage:
    python parse_new_sbem_to_csv_with_length.py <target_folder>
"""

import os
import glob
import struct
import argparse
import pandas as pd

# --- Global Configuration ---
VERBOSE_CHUNK_COUNT = 10      # How many chunks to print detailed info for.
PROGRESS_INTERVAL = 1000      # Print progress message every N chunks.

# --- Globals to hold parsed output for one file ---
descriptor_definitions = []  # Stores descriptor chunk info.
group_definitions = []       # Stores parsed <GRP> lines.
data_chunks = []             # List to hold parsed data chunk dictionaries.
unique_chunk_ids = set()     # Set to hold unique chunk IDs encountered.
unique_chunk_lengths = set() # Set to hold unique chunk lengths encountered.

# --- Constants ---
ReservedSbemId_e_Escape = b"\xff"  # 0xFF in binary.
ReservedSbemId_e_Descriptor = 0    # Chunk id 0 indicates a descriptor chunk.

# --- Low-Level Reading Functions ---
def readId(f):
    pos_before = f.tell()
    byte1 = f.read(1)
    if not byte1:
        print("EOF reached while trying to read an ID at position", pos_before)
        return None
    id_val = int.from_bytes(byte1, byteorder="little")
    if id_val >= ReservedSbemId_e_Escape[0]:
        extra = f.read(2)
        if len(extra) != 2:
            print("Unexpected EOF when reading extended ID at pos", f.tell())
            return None
        id_val = int.from_bytes(extra, byteorder="little")
        print(f"Read extended ID: {id_val} (starting at pos {pos_before})")
    else:
        print(f"Read ID: {id_val} (at pos {pos_before})")
    return id_val

def readLen(f):
    pos_before = f.tell()
    byte1 = f.read(1)
    if not byte1:
        print("EOF reached while trying to read a length at position", pos_before)
        return None
    first_val = byte1[0]
    if first_val < ReservedSbemId_e_Escape[0]:
        length_val = first_val
        print(f"Read one-byte length: {length_val} (at pos {pos_before})")
    else:
        extra = f.read(4)
        if len(extra) != 4:
            print("Unexpected EOF when reading extended length at pos", f.tell())
            return None
        length_val = int.from_bytes(extra, byteorder="little")
        print(f"Read extended length: {length_val} (at pos {pos_before})")
    return length_val

def readHeader(f):
    header_bytes = f.read(8)
    print("SBEM Header:", header_bytes)

# --- Helper to Parse a Group Line (if present) ---
def parseGroupLine(line):
    clean_line = line.strip()[len("<GRP>"):]
    tokens = clean_line.split(",")
    token_values = []
    decoded = []
    for tok in tokens:
        tok_clean = "".join(ch for ch in tok if ch.isdigit())
        if not tok_clean:
            continue
        try:
            val = int(tok_clean)
            token_values.append(val)
            decoded.append(str(val))
        except Exception as e:
            decoded.append(f"(error parsing token '{tok}')")
    group_line_dict = {
        "raw_line": line.strip(),
        "tokens": token_values,
        "decoded": ", ".join(decoded)
    }
    group_definitions.append(group_line_dict)
    print("  [GROUP] " + ", ".join(decoded))
    return token_values

# --- Parsing Functions ---

def parse_MEASIMU6_new(data_bytes, chunk_index):
    """
    New IMU6 packet assumed to be 52 bytes long.
    Format:
      - 4-byte timestamp (uint32)
      - 2 accelerometer samples, each with 3 float32 values (12 bytes per sample, total 24 bytes)
      - 2 gyroscope samples, each with 3 float32 values (12 bytes per sample, total 24 bytes)
    Total = 4 + 24 + 24 = 52 bytes.
    """
    if len(data_bytes) < 52:
        print(f"IMU chunk too short (len={len(data_bytes)})")
        return
    timestamp = struct.unpack("<I", data_bytes[0:4])[0]
    offset = 4
    accel_samples = []
    for i in range(2):
        sample = struct.unpack("<fff", data_bytes[offset:offset+12])
        accel_samples.append({"x": sample[0], "y": sample[1], "z": sample[2]})
        offset += 12
    gyro_samples = []
    for i in range(2):
        sample = struct.unpack("<fff", data_bytes[offset:offset+12])
        gyro_samples.append({"x": sample[0], "y": sample[1], "z": sample[2]})
        offset += 12
    chunk_data = {
        "chunk_index": chunk_index,
        "group": "IMU",
        "TIMESTAMP": timestamp,
        "ACCEL": accel_samples,
        "GYRO": gyro_samples,
    }
    data_chunks.append(chunk_data)
    print(f"Parsed IMU chunk: TIMESTAMP={timestamp}, {len(accel_samples)} accel samples, {len(gyro_samples)} gyro samples")

def parse_ECGmV_chunk(data_bytes, chunk_index):
    """
    ECG mV packet assumed to be 68 bytes long.
    Expected format:
      - 4-byte timestamp (uint32)
      - 16 samples, each 4-byte float32 (total 64 bytes)
    Total = 4 + 64 = 68 bytes.
    """
    if len(data_bytes) != 68:
        print(f"ECG mV chunk unexpected length (len={len(data_bytes)}), expected 68 bytes.")
        return

    # Parse 4-byte timestamp as uint32 (little-endian)
    timestamp = struct.unpack("<I", data_bytes[0:4])[0]

    samples = []
    offset = 4
    for i in range(16):
        sample_bytes = data_bytes[offset:offset+4]
        sample = struct.unpack("<f", sample_bytes)[0]
        samples.append(sample)
        offset += 4

    chunk_data = {
        "chunk_index": chunk_index,
        "group": "ECGmV",
        "TIMESTAMP": timestamp,
        "SAMPLES": samples,
    }
    data_chunks.append(chunk_data)
    print(f"Parsed ECG mV chunk: TIMESTAMP={timestamp}, {len(samples)} samples")

def parseDataChunk(chunk_id, data_bytes, data_chunk_index):
    global data_chunks, unique_chunk_ids, unique_chunk_lengths
    unique_chunk_ids.add(chunk_id)
    unique_chunk_lengths.add(len(data_bytes))
    
    if len(data_bytes) == 52:
        print(f"Chunk length {len(data_bytes)} bytes: assuming IMU packet")
        parse_MEASIMU6_new(data_bytes, data_chunk_index)
    elif len(data_bytes) == 68:
        print(f"Chunk length {len(data_bytes)} bytes: assuming ECG mV packet")
        parse_ECGmV_chunk(data_bytes, data_chunk_index)
    else:
        if len(data_bytes) < 4:
            print(f"Chunk length {len(data_bytes)} too short for fallback parsing")
            return
        value = struct.unpack("<I", data_bytes[0:4])[0]
        chunk_data = {
            "chunk_index": data_chunk_index,
            "chunk_id": chunk_id,
            "value": value
        }
        data_chunks.append(chunk_data)
        print(f"Parsed fallback chunk id {chunk_id}: value {value}")

# --- Main Processing Function for One SBEM File ---
def processSBEM(file_path):
    global data_chunks, descriptor_definitions, group_definitions, unique_chunk_ids, unique_chunk_lengths
    # Clear globals for this file.
    data_chunks = []
    descriptor_definitions = []
    group_definitions = []
    unique_chunk_ids = set()
    unique_chunk_lengths = set()
    
    print(">>> Processing file:", file_path)
    try:
        with open(file_path, "rb") as f:
            print(">>> Reading SBEM header...")
            readHeader(f)
            chunk_index = 0
            while True:
                current_offset = f.tell()
                if chunk_index % PROGRESS_INTERVAL == 0:
                    print(f"\n--- Processing chunk #{chunk_index} at offset {current_offset} ---")
                chunk_id = readId(f)
                if chunk_id is None:
                    print("No more chunk IDs found; finishing file processing.")
                    break
                datasize = readLen(f)
                if datasize is None:
                    print("No datasize available; finishing file processing.")
                    break
                chunk_bytes = f.read(datasize)
                if len(chunk_bytes) != datasize:
                    print(f"ERROR: Expected {datasize} bytes, but only read {len(chunk_bytes)} bytes.")
                    break
                if chunk_id == ReservedSbemId_e_Descriptor:
                    print("\n>>> Processing a Descriptor Chunk:")
                    try:
                        data_str = chunk_bytes.decode("utf-8", errors="replace")
                        print("Descriptor chunk (decoded):")
                        print(data_str)
                        for line in data_str.splitlines():
                            if line.startswith("<GRP>"):
                                parseGroupLine(line)
                    except Exception as e:
                        print("Error decoding descriptor chunk:", e)
                else:
                    if chunk_index < VERBOSE_CHUNK_COUNT or chunk_index % PROGRESS_INTERVAL == 0:
                        print(f"\n>>> Processing a Data Chunk (ID = {chunk_id}, length = {len(chunk_bytes)}):")
                    parseDataChunk(chunk_id, chunk_bytes, chunk_index)
                chunk_index += 1
    except Exception as e:
        print("Error processing SBEM file:", e)
    print("\n>>> Finished processing file:", file_path)
    print("Unique chunk IDs encountered:", unique_chunk_ids)
    print("Unique chunk lengths encountered:", unique_chunk_lengths)

# --- Main Entry Point ---
def main():
    parser = argparse.ArgumentParser(
        description="Parse new SBEM files (using length-based dispatch) in a folder and output CSV files for each."
    )
    parser.add_argument("folder", help="Target folder containing SBEM files.")
    args = parser.parse_args()
    
    target_folder = args.folder
    sbem_files = glob.glob(os.path.join(target_folder, "*.sbem"))
    if not sbem_files:
        print("No SBEM files found in folder:", target_folder)
        return
    
    for sbem_file in sbem_files:
        print("\n==============================")
        print("Processing file:", sbem_file)
        processSBEM(sbem_file)
        df_chunks = pd.json_normalize(data_chunks)
        base_name = os.path.splitext(os.path.basename(sbem_file))[0]
        csv_filename = os.path.join(target_folder, base_name + ".csv")
        df_chunks.to_csv(csv_filename, index=False)
        print(f"Saved CSV: {csv_filename}")

if __name__ == "__main__":
    main()