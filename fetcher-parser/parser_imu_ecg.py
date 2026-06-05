#!/usr/bin/env python3
"""
parse_new_sbem_to_csv_with_length.py

This script searches a target folder for all .sbem files, parses each file,
and saves the parsed data as CSV.

It handles the following chunk types based solely on their length:
  - 52 bytes: IMU packet (new IMU6 format)
  - 68 bytes: ECG packet (4-byte timestamp, 16 x float32 samples)

IMPROVED RESYNC MECHANISM for interrupted/corrupted SBEM files:
  - Robust byte-by-byte scanning to find valid data chunks
  - Statistical validation of float values (NaN/Inf rejection, reasonable ranges)
  - Adaptive timestamp validation
  - Efficient zero-byte skipping
  - Multiple resync attempts throughout the file
"""

import os
import glob
import struct
import argparse
import pandas as pd
from io import BytesIO
import math

# --- Global Configuration ---
VERBOSE_CHUNK_COUNT = 10
PROGRESS_INTERVAL = 1000
MIN_CONSECUTIVE_VALID_CHUNKS = 3  # Require this many valid chunks to confirm resync
RESYNC_SEARCH_WINDOW = 500000  # bytes to scan forward when searching for sync

# --- Globals to hold parsed output for one file ---
descriptor_definitions = []
group_definitions = []
data_chunks = []
unique_chunk_ids = set()
unique_chunk_lengths = set()

# --- Constants ---
ReservedSbemId_e_Escape = b"\xff"
ReservedSbemId_e_Descriptor = 0


# --- Low-Level Reading Functions ---
def readId(f):
    pos_before = f.tell()
    byte1 = f.read(1)
    if not byte1:
        return None
    id_val = int.from_bytes(byte1, byteorder="little")
    if id_val >= ReservedSbemId_e_Escape[0]:
        extra = f.read(2)
        if len(extra) != 2:
            return None
        id_val = int.from_bytes(extra, byteorder="little")
    return id_val


def readLen(f):
    pos_before = f.tell()
    byte1 = f.read(1)
    if not byte1:
        return None
    first_val = byte1[0]
    if first_val < ReservedSbemId_e_Escape[0]:
        length_val = first_val
    else:
        extra = f.read(4)
        if len(extra) != 4:
            return None
        length_val = int.from_bytes(extra, byteorder="little")
    return length_val


def readHeader(f):
    header_bytes = f.read(8)
    print("SBEM Header:", header_bytes)
    return header_bytes


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
        except Exception:
            decoded.append(f"(error parsing token '{tok}')")
    group_line_dict = {
        "raw_line": line.strip(),
        "tokens": token_values,
        "decoded": ", ".join(decoded)
    }
    group_definitions.append(group_line_dict)
    return token_values


# --- Parsing Functions ---
def parse_MEASIMU6_new(data_bytes, chunk_index):
    if len(data_bytes) < 52:
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


def parse_ECGmV_chunk(data_bytes, chunk_index):
    if len(data_bytes) != 68:
        return

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


def parseDataChunk(chunk_id, data_bytes, data_chunk_index):
    global data_chunks, unique_chunk_ids, unique_chunk_lengths
    unique_chunk_ids.add(chunk_id)
    unique_chunk_lengths.add(len(data_bytes))

    if len(data_bytes) == 52:
        parse_MEASIMU6_new(data_bytes, data_chunk_index)
    elif len(data_bytes) == 68:
        parse_ECGmV_chunk(data_bytes, data_chunk_index)
    else:
        if len(data_bytes) < 4:
            return
        value = struct.unpack("<I", data_bytes[0:4])[0]
        chunk_data = {
            "chunk_index": data_chunk_index,
            "chunk_id": chunk_id,
            "value": value
        }
        data_chunks.append(chunk_data)


# --- IMPROVED VALIDATION FUNCTIONS ---

def is_valid_float(val):
    """Check if a float value is finite and reasonable for sensor data."""
    if not math.isfinite(val):
        return False
    # Allow wide range but reject extreme values
    if abs(val) > 1e6:
        return False
    return True


def plausible_timestamp(ts, prev_ts=None):
    """
    Timestamp validation:
    - Must be nonzero and not absurdly huge
    - If prev_ts provided, must increase but not jump too much
    """
    if ts == 0 or ts > 0xFFFFFFF0:  # Reject 0 and values near max uint32
        return False
    
    # Basic range check - allow any reasonable embedded system timestamp
    if ts < 100:  # Too small
        return False
    
    if prev_ts is not None:
        # Must increase
        if ts <= prev_ts:
            return False
        # Reject absurd jumps (more than 1 minute at typical rates)
        if (ts - prev_ts) > 60000:
            return False
    
    return True


def validate_ecg_payload(payload):
    """
    Strict validation for ECG payload:
    - Must be exactly 68 bytes
    - Timestamp must be plausible
    - All 16 float samples must be finite and reasonable
    """
    if len(payload) != 68:
        return False

    try:
        timestamp = struct.unpack("<I", payload[0:4])[0]
        if not plausible_timestamp(timestamp):
            return False
        
        samples = struct.unpack("<16f", payload[4:68])
        
        # All samples must be valid floats
        valid_count = sum(1 for s in samples if is_valid_float(s))
        if valid_count < 15:  # Allow 1 bad sample max
            return False
        
        # Statistical check: ECG values are typically in millivolts
        # Reasonable range: -10mV to +10mV for most ECG signals
        # But allow wider range to be safe
        reasonable_count = sum(1 for s in samples if abs(s) < 100.0)
        if reasonable_count < 12:  # At least 75% should be in reasonable range
            return False
            
        return True
    except Exception:
        return False


def validate_imu_payload(payload):
    """
    Strict validation for IMU payload:
    - Must be exactly 52 bytes
    - Timestamp must be plausible
    - All float samples must be finite and reasonable
    """
    if len(payload) != 52:
        return False

    try:
        timestamp = struct.unpack("<I", payload[0:4])[0]
        if not plausible_timestamp(timestamp):
            return False
        
        # Parse all 12 float values (6 accel + 6 gyro)
        floats = struct.unpack("<12f", payload[4:52])
        
        # All must be valid
        if not all(is_valid_float(f) for f in floats):
            return False
        
        # IMU accelerometer: typically ±16g max (±156.96 m/s²)
        # IMU gyroscope: typically ±2000 deg/s max (±34.9 rad/s)
        # Use generous bounds
        accel_vals = floats[0:6]
        gyro_vals = floats[6:12]
        
        # Accel should be reasonable (allow up to 200 m/s² to be safe)
        if any(abs(a) > 200.0 for a in accel_vals):
            return False
            
        # Gyro should be reasonable (allow up to 100 rad/s to be safe)
        if any(abs(g) > 100.0 for g in gyro_vals):
            return False
        
        return True
    except Exception:
        return False


def read_candidate_header_from_bytes(data, pos):
    """
    Read [chunk_id][length] from raw bytes starting at pos.
    Returns dict with parsed header info or None if invalid.
    """
    if pos >= len(data):
        return None

    start = pos

    # Read ID
    if pos >= len(data):
        return None
    first_id = data[pos]
    pos += 1
    
    if first_id >= ReservedSbemId_e_Escape[0]:
        if pos + 2 > len(data):
            return None
        chunk_id = int.from_bytes(data[pos:pos+2], byteorder="little")
        pos += 2
    else:
        chunk_id = first_id

    # Read length
    if pos >= len(data):
        return None

    first_len = data[pos]
    pos += 1
    
    if first_len < ReservedSbemId_e_Escape[0]:
        datasize = first_len
    else:
        if pos + 4 > len(data):
            return None
        datasize = int.from_bytes(data[pos:pos+4], byteorder="little")
        pos += 4

    return {
        "start_pos": start,
        "payload_pos": pos,
        "chunk_id": chunk_id,
        "datasize": datasize
    }


def try_parse_candidate(data, pos, prev_timestamp=None):
    """
    Try to parse a valid data chunk at position pos.
    Returns chunk info dict or None if invalid.
    """
    header = read_candidate_header_from_bytes(data, pos)
    if header is None:
        return None

    chunk_id = header["chunk_id"]
    datasize = header["datasize"]
    payload_pos = header["payload_pos"]

    # Skip descriptor chunks
    if chunk_id == ReservedSbemId_e_Descriptor:
        return None

    # Only accept known data sizes
    if datasize not in (52, 68):
        return None

    end_pos = payload_pos + datasize
    if end_pos > len(data):
        return None

    payload = data[payload_pos:end_pos]
    
    # Validate based on size
    if datasize == 68:
        if not validate_ecg_payload(payload):
            return None
        kind = "ECG"
    elif datasize == 52:
        if not validate_imu_payload(payload):
            return None
        kind = "IMU"
    else:
        return None

    timestamp = struct.unpack("<I", payload[0:4])[0]
    
    # Additional timestamp validation if we have previous
    if prev_timestamp is not None:
        if not plausible_timestamp(timestamp, prev_timestamp):
            return None

    return {
        "chunk_id": chunk_id,
        "datasize": datasize,
        "payload": payload,
        "timestamp": timestamp,
        "kind": kind,
        "start_pos": header["start_pos"],
        "payload_pos": payload_pos,
        "end_pos": end_pos
    }


def find_resync_start(data, start_pos, min_consecutive=3, search_window=200000):
    """
    Scan forward byte-by-byte until we find a plausible run of valid chunks.
    Uses strict validation to ensure we've found real data.
    """
    max_pos = min(len(data) - 100, start_pos + search_window)

    print(f">>> Starting resync scan from offset {start_pos} to {max_pos}")

    pos = start_pos
    scan_count = 0
    
    while pos < max_pos:
        scan_count += 1
        if scan_count % 10000 == 0:
            print(f"    Scanned {scan_count} positions, currently at {pos}")
        
        # Fast-forward through long runs of zeros or 0xFF
        if pos + 64 < len(data):
            chunk = data[pos:pos+64]
            if chunk.count(0) > 60 or chunk.count(0xFF) > 60:
                pos += 32
                continue

        # Try to parse a candidate chunk
        first = try_parse_candidate(data, pos)
        if first is None:
            pos += 1
            continue

        # Found a potential first chunk, now verify with consecutive chunks
        run = [first]
        current = first
        ok = True

        for i in range(min_consecutive - 1):
            nxt = try_parse_candidate(data, current["end_pos"], current["timestamp"])
            if nxt is None:
                ok = False
                break

            # Timestamps must increase monotonically
            if nxt["timestamp"] <= current["timestamp"]:
                ok = False
                break

            run.append(nxt)
            current = nxt

        if ok and len(run) >= min_consecutive:
            print(f">>> Found plausible resync run after scanning {scan_count} positions:")
            for i, r in enumerate(run):
                print(
                    f"    run[{i}] offset={r['start_pos']} id={r['chunk_id']} "
                    f"len={r['datasize']} kind={r['kind']} ts={r['timestamp']}"
                )
            return run[0]["start_pos"]

        pos += 1

    print(f">>> No resync point found after scanning {scan_count} positions")
    return None


# --- Main Processing Function for One SBEM File ---
def processSBEM(file_path):
    global data_chunks, descriptor_definitions, group_definitions, unique_chunk_ids, unique_chunk_lengths

    data_chunks = []
    descriptor_definitions = []
    group_definitions = []
    unique_chunk_ids = set()
    unique_chunk_lengths = set()

    print(">>> Processing file:", file_path)

    try:
        with open(file_path, "rb") as f:
            full_data = f.read()

        print(f">>> File size: {len(full_data)} bytes")
        bio = BytesIO(full_data)

        print(">>> Reading SBEM header...")
        header = readHeader(bio)
        
        # Validate header
        if not header.startswith(b"SBEM"):
            print("WARNING: Header doesn't start with 'SBEM', file may be corrupted")

        chunk_index = 0
        descriptor_end_pos = bio.tell()

        # --- Pass 1: Read descriptor chunks ---
        print("\n>>> PASS 1: Reading descriptor chunks...")
        while True:
            current_offset = bio.tell()

            chunk_id = readId(bio)
            if chunk_id is None:
                break

            datasize = readLen(bio)
            if datasize is None:
                break

            chunk_bytes = bio.read(datasize)
            if len(chunk_bytes) != datasize:
                print(f"Incomplete chunk read, expected {datasize} got {len(chunk_bytes)}")
                break

            if chunk_id == ReservedSbemId_e_Descriptor:
                print(f"\n>>> Descriptor chunk at offset {current_offset}, size {datasize}")
                try:
                    data_str = chunk_bytes.decode("utf-8", errors="replace")
                    descriptor_definitions.append(data_str)
                    for line in data_str.splitlines():
                        if line.startswith("<GRP>"):
                            parseGroupLine(line)
                except Exception as e:
                    print("Error decoding descriptor chunk:", e)
                descriptor_end_pos = bio.tell()
            else:
                # First non-descriptor chunk
                print(f"\n>>> First non-descriptor at offset {current_offset}")
                descriptor_end_pos = current_offset
                break

            chunk_index += 1

        print(f">>> Descriptor section ends at offset {descriptor_end_pos}")

        # --- Pass 2: Find resync point ---
        print("\n>>> PASS 2: Finding resync point...")
        resync_pos = find_resync_start(
            full_data,
            start_pos=descriptor_end_pos,
            min_consecutive=MIN_CONSECUTIVE_VALID_CHUNKS,
            search_window=RESYNC_SEARCH_WINDOW
        )

        if resync_pos is None:
            print(">>> ERROR: Could not find valid data chunks in file")
            print(">>> This file may be too corrupted to recover")
            return

        print(f">>> Successfully resynchronized at offset {resync_pos}")
        print(f">>> Skipped {resync_pos - descriptor_end_pos} bytes of corrupted data")

        # --- Pass 3: Parse data from resync point ---
        print("\n>>> PASS 3: Parsing synchronized data...")
        pos = resync_pos
        data_chunk_index = 0
        last_timestamp = None
        consecutive_failures = 0
        max_consecutive_failures = 100  # Allow some noise but not too much

        while pos < len(full_data):
            if data_chunk_index % PROGRESS_INTERVAL == 0:
                print(f"\n--- Parsed {data_chunk_index} chunks, at offset {pos}/{len(full_data)} ({100*pos//len(full_data)}%) ---")

            candidate = try_parse_candidate(full_data, pos, last_timestamp)
            
            if candidate is None:
                consecutive_failures += 1
                
                if consecutive_failures >= max_consecutive_failures:
                    print(f">>> Lost sync at offset {pos} after {consecutive_failures} failures")
                    print(">>> Attempting resync...")
                    
                    new_pos = find_resync_start(
                        full_data,
                        start_pos=pos + 1,
                        min_consecutive=MIN_CONSECUTIVE_VALID_CHUNKS,
                        search_window=min(100000, len(full_data) - pos)
                    )

                    if new_pos is None:
                        print(">>> No further valid data found. Stopping.")
                        break

                    print(f">>> Resynced at offset {new_pos}")
                    pos = new_pos
                    consecutive_failures = 0
                    last_timestamp = None
                    continue
                
                pos += 1
                continue

            # Successfully parsed a chunk
            consecutive_failures = 0
            
            if data_chunk_index < VERBOSE_CHUNK_COUNT or data_chunk_index % PROGRESS_INTERVAL == 0:
                print(
                    f"    Chunk #{data_chunk_index}: offset={candidate['start_pos']} "
                    f"id={candidate['chunk_id']} kind={candidate['kind']} "
                    f"ts={candidate['timestamp']}"
                )

            parseDataChunk(candidate["chunk_id"], candidate["payload"], data_chunk_index)

            last_timestamp = candidate["timestamp"]
            pos = candidate["end_pos"]
            data_chunk_index += 1

        print(f"\n>>> Successfully parsed {data_chunk_index} data chunks")

    except Exception as e:
        print("Error processing SBEM file:", e)
        import traceback
        traceback.print_exc()

    print("\n>>> Finished processing file:", file_path)
    print(f"Total chunks parsed: {len(data_chunks)}")
    print("Unique chunk IDs encountered:", unique_chunk_ids)
    print("Unique chunk lengths encountered:", unique_chunk_lengths)


# --- Main Entry Point ---
def main():
    parser = argparse.ArgumentParser(
        description="Parse SBEM files with robust resync for interrupted/corrupted files."
    )
    parser.add_argument("folder", help="Target folder containing SBEM files.")
    parser.add_argument("--output-suffix", default="", help="Suffix to add to output CSV filename")
    args = parser.parse_args()

    target_folder = args.folder
    sbem_files = glob.glob(os.path.join(target_folder, "*.sbem"))
    
    if not sbem_files:
        print("No SBEM files found in folder:", target_folder)
        return

    for sbem_file in sbem_files:
        print("\n" + "="*80)
        print("Processing file:", sbem_file)
        print("="*80)
        
        processSBEM(sbem_file)
        
        if len(data_chunks) > 0:
            df_chunks = pd.json_normalize(data_chunks)
            base_name = os.path.splitext(os.path.basename(sbem_file))[0]
            csv_filename = os.path.join(target_folder, base_name + args.output_suffix + ".csv")
            df_chunks.to_csv(csv_filename, index=False)
            print(f"\n>>> Saved {len(df_chunks)} rows to: {csv_filename}")
        else:
            print("\n>>> WARNING: No data chunks found, CSV not created")


if __name__ == "__main__":
    main()