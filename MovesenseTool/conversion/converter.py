#!/usr/bin/env python3
"""
converter.py

This module provides a function convert_sbem() that parses an SBEM file
and writes its data as a CSV file using the new IMU, ECG, and HR parsing logic.
"""

from __future__ import print_function
import os
import glob
import logging
import struct
import pandas as pd

# --- Global Configuration ---
VERBOSE_CHUNK_COUNT = 10      # How many chunks to print detailed info for.
PROGRESS_INTERVAL = 1000      # Print progress message every N chunks.

# --- Constants and Globals ---
ReservedSbemId_e_Escape = b"\xff"  # 0xFF in binary.
ReservedSbemId_e_Descriptor = 0     # Chunk ID 0 indicates a descriptor chunk.

# Global lists to hold parsed output for one file.
descriptor_definitions = []  # Will remain empty if no descriptor chunk is present.
group_definitions = []       # List of parsed <GRP> lines.
data_chunks = []             # List to hold parsed data chunk dictionaries.

# --- (Optional) Group Parsing Mapping ---
# (Not used for IMU/ECG/HR, but kept for compatibility with descriptor parsing.)
enum_mapping = {
    10: "MEASHR_AVERAGE",
    26: "MEASHR_RRDATA",
    54: "ARRAY_BEGIN",
    55: "ARRAY_END",
    6:  "MEASACC_TIMESTAMP",
    18: "MEASACC_ARRAYACC_X",
    19: "MEASACC_ARRAYACC_Y",
    20: "MEASACC_ARRAYACC_Z",
    57: "MEASACC_ARRAYACC_",  # Group representing the 3-axis accelerometer vector.
}

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

def parseDescriptorChunk(data_bytes):
    global descriptor_definitions
    print("\n=== Parsing Descriptor Chunk ===")
    try:
        data_str = data_bytes.decode("utf-8", errors="replace")
        print("Descriptor chunk (decoded):")
        print(data_str)
    except Exception as e:
        print("Error decoding descriptor chunk:", e)
        return
    lines = data_str.splitlines()
    for line in lines:
        if line.startswith("<GRP>"):
            parseGroupLine(line)
    print("=== End Descriptor Chunk ===\n")

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
            name = enum_mapping.get(val, f"(unknown:{val})")
            decoded.append(f"{val}: {name}")
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

# --- Parsing Functions for IMU, ECG, and HR ---

def parse_IMU6_new(data_bytes, chunk_index):
    """
    Parse a new IMU6 packet assumed to be 52 bytes long.
    Format:
      - 4-byte timestamp (uint32)
      - 2 accelerometer samples, each with 3 float32 values (12 bytes each, total 24 bytes)
      - 2 gyroscope samples, each with 3 float32 values (12 bytes each, total 24 bytes)
    Total = 4 + 24 + 24 = 52 bytes.
    """
    if len(data_bytes) < 52:
        print(f"IMU packet too short (len={len(data_bytes)})")
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
        "GYRO": gyro_samples
    }
    data_chunks.append(chunk_data)
    print(f"Parsed IMU packet: TIMESTAMP={timestamp}, 2 accel samples, 2 gyro samples")

def parse_ECG_mV(data_bytes, chunk_index):
    """
    Parse an ECG mV packet assumed to be 68 bytes long.
    Expected format:
      - 4-byte timestamp (uint32)
      - 16 float32 samples (16 x 4 = 64 bytes)
    Total = 4 + 64 = 68 bytes.
    """
    if len(data_bytes) != 68:
        print(f"ECG mV packet unexpected length (len={len(data_bytes)}), expected 68 bytes.")
        return
    timestamp = struct.unpack("<I", data_bytes[0:4])[0]
    samples = []
    offset = 4
    for i in range(16):
        sample = struct.unpack("<f", data_bytes[offset:offset+4])[0]
        samples.append(sample)
        offset += 4
    chunk_data = {
        "chunk_index": chunk_index,
        "group": "ECGmV",
        "TIMESTAMP": timestamp,
        "SAMPLES": samples
    }
    data_chunks.append(chunk_data)
    print(f"Parsed ECG mV packet: TIMESTAMP={timestamp}, 16 samples")

def parse_HR_chunk_new(data_bytes, chunk_index):
    """
    Parse an HR packet assumed to be 6 bytes long.
    Format:
      - 4-byte average heart rate (float32)
      - 2-byte RR interval (uint16)
    Total = 4 + 2 = 6 bytes.
    """
    if len(data_bytes) != 6:
        print(f"HR packet unexpected length (len={len(data_bytes)}), expected 6 bytes.")
        return
    average = struct.unpack("<f", data_bytes[0:4])[0]
    rr, = struct.unpack("<H", data_bytes[4:6])
    chunk_data = {
        "chunk_index": chunk_index,
        "group": "HR",
        "AVERAGE": average,
        "RR": rr
    }
    data_chunks.append(chunk_data)
    print(f"Parsed HR packet: AVERAGE={average}, RR={rr}")

# --- Modified Parsing Function ---
def parseDataChunk(chunk_id, data_bytes, data_chunk_index):
    global data_chunks
    length = len(data_bytes)
    if length == 52:
        print(f"Chunk length {length} bytes: assuming new IMU6 packet")
        parse_IMU6_new(data_bytes, data_chunk_index)
    elif length == 68:
        print(f"Chunk length {length} bytes: assuming ECG mV packet")
        parse_ECG_mV(data_bytes, data_chunk_index)
    elif length == 6:
        print(f"Chunk length {length} bytes: assuming HR packet")
        parse_HR_chunk_new(data_bytes, data_chunk_index)
    else:
        # Fallback parser: read a 4-byte field.
        if length < 4:
            print(f"Chunk length {length} too short for fallback parsing")
            return
        value = struct.unpack("<I", data_bytes[0:4])[0]
        chunk_data = {
            "chunk_index": data_chunk_index,
            "chunk_id": chunk_id,
            "value": value
        }
        data_chunks.append(chunk_data)
        print(f"Parsed fallback chunk id {chunk_id}: value {value}")

def processSBEM(file_path):
    """
    Process an SBEM file and return a list of data rows.
    For this parser, each data row is a dictionary.
    """
    global data_chunks, descriptor_definitions, group_definitions
    # Clear globals for this file.
    data_chunks = []
    descriptor_definitions = []
    group_definitions = []
    
    print(">>> Processing file:", file_path)
    try:
        with open(file_path, "rb") as f:
            print(">>> Reading SBEM header...")
            readHeader(f)
            chunk_index = 0
            while True:
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
                    parseDescriptorChunk(chunk_bytes)
                else:
                    print(f"\n>>> Processing a Data Chunk (ID = {chunk_id}):")
                    parseDataChunk(chunk_id, chunk_bytes, chunk_index)
                chunk_index += 1
    except Exception as e:
        print("Error processing SBEM file:", e)
    print(">>> Finished processing file:", file_path)
    return data_chunks

def convert_sbem(file_path: str, output_dir: str):
    """
    Convert an SBEM file to CSV.
    :param file_path: Path to the input SBEM file.
    :param output_dir: Folder where the converted CSV file will be saved.
    """
    logging.info(f"Converting {file_path} to CSV in {output_dir}")
    rows = processSBEM(file_path)
    if not rows:
        logging.warning("No data rows parsed from SBEM file.")
        return
    try:
        df = pd.json_normalize(rows)
    except Exception as e:
        logging.error("Error creating DataFrame: " + str(e))
        return
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    csv_filename = os.path.join(output_dir, base_name + ".csv")
    try:
        df.to_csv(csv_filename, index=False)
        logging.info(f"Saved CSV: {csv_filename}")
    except Exception as e:
        logging.error("Error saving CSV: " + str(e))

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Convert SBEM files in a folder to CSV.")
    parser.add_argument("folder", help="Target folder containing SBEM files.")
    args = parser.parse_args()
    
    target_folder = args.folder
    sbem_files = glob.glob(os.path.join(target_folder, "*.sbem"))
    if not sbem_files:
        print("No SBEM files found in folder:", target_folder)
    else:
        for sbem_file in sbem_files:
            print("\n==============================")
            print("Processing file:", sbem_file)
            convert_sbem(sbem_file, target_folder)