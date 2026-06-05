#!/usr/bin/env python3
"""
parse_old_sbem_rr_hr.py

Parses older SBEM files that contain:
- MeasHR.average   -> float32
- MeasHR.rrData    -> repeated uint16
and optionally:
- MeasIMU6         -> 52-byte IMU packets

This parser is designed for files where ECG is not present, but RR intervals
and average HR are stored in variable-length payloads.

Supported packet types
----------------------
1) IMU packet
   - 52 bytes
   - uint32 timestamp + 12 float32 values

2) HR/RR packet (old format)
   Two layouts are tried:
   A. [float32 avg_hr][uint16 rr_1][uint16 rr_2]...
   B. [uint8 array_begin][float32 avg_hr][uint16 rr_1][uint16 rr_2]...

Timestamp interpolation
-----------------------
HR/RR packets do NOT carry their own timestamp. They are always interleaved
with IMU packets that do. After parsing, each HR/RR packet is assigned a
TIMESTAMP by linear interpolation between the preceding and following IMU
timestamps (based on chunk_index). This replaces the old cumulative_time_ms
approach which drifts from wall-clock time.

Resync strategy
---------------
- robust byte-by-byte scanning
- accepts only plausible packets
- can resync after corruption/interruption

Output
------
One CSV per SBEM file.
For HR packets:
- one row per packet
- rr_intervals_ms stored as a Python-style list string
- rr_count
- avg_hr_bpm
- TIMESTAMP interpolated from surrounding IMU packets

If desired, you can later explode rr_intervals_ms to long format.
"""

from __future__ import annotations

import os
import glob
import struct
import argparse
import math
from io import BytesIO

import pandas as pd


# -----------------------------
# Configuration
# -----------------------------
VERBOSE_CHUNK_COUNT = 10
PROGRESS_INTERVAL = 1000
MIN_CONSECUTIVE_VALID_CHUNKS = 3
RESYNC_SEARCH_WINDOW = 500000
MAX_CONSECUTIVE_FAILURES = 100

ReservedSbemId_e_Escape = b"\xff"
ReservedSbemId_e_Descriptor = 0


# -----------------------------
# Globals per file
# -----------------------------
descriptor_definitions = []
group_definitions = []
data_chunks = []
unique_chunk_ids = set()
unique_chunk_lengths = set()


# -----------------------------
# Low-level SBEM readers
# -----------------------------
def readId(f):
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
    byte1 = f.read(1)
    if not byte1:
        return None
    first_val = byte1[0]
    if first_val < ReservedSbemId_e_Escape[0]:
        return first_val
    extra = f.read(4)
    if len(extra) != 4:
        return None
    return int.from_bytes(extra, byteorder="little")


def readHeader(f):
    header_bytes = f.read(8)
    print("SBEM Header:", header_bytes)
    return header_bytes


def parseGroupLine(line):
    clean_line = line.strip()[len("<GRP>"):]
    tokens = clean_line.split(",")
    vals = []
    for tok in tokens:
        tok_clean = "".join(ch for ch in tok if ch.isdigit())
        if tok_clean:
            try:
                vals.append(int(tok_clean))
            except Exception:
                pass
    group_definitions.append({"raw_line": line.strip(), "tokens": vals})
    return vals


# -----------------------------
# Generic validation helpers
# -----------------------------
def is_valid_float(val):
    if not math.isfinite(val):
        return False
    if abs(val) > 1e6:
        return False
    return True


def plausible_timestamp(ts, prev_ts=None):
    if ts == 0 or ts > 0xFFFFFFF0:
        return False
    if ts < 100:
        return False
    if prev_ts is not None:
        if ts <= prev_ts:
            return False
        if (ts - prev_ts) > 60000:
            return False
    return True


# -----------------------------
# IMU parsing/validation
# -----------------------------
def validate_imu_payload(payload):
    if len(payload) != 52:
        return False
    try:
        timestamp = struct.unpack("<I", payload[0:4])[0]
        if not plausible_timestamp(timestamp):
            return False

        floats = struct.unpack("<12f", payload[4:52])

        if not all(is_valid_float(f) for f in floats):
            return False

        accel_vals = floats[0:6]
        gyro_vals = floats[6:12]

        if any(abs(a) > 200.0 for a in accel_vals):
            return False
        if any(abs(g) > 100.0 for g in gyro_vals):
            return False

        return True
    except Exception:
        return False


def parse_MEASIMU6_new(data_bytes, chunk_index):
    timestamp = struct.unpack("<I", data_bytes[0:4])[0]
    offset = 4

    accel_samples = []
    for _ in range(2):
        sample = struct.unpack("<fff", data_bytes[offset:offset + 12])
        accel_samples.append({"x": sample[0], "y": sample[1], "z": sample[2]})
        offset += 12

    gyro_samples = []
    for _ in range(2):
        sample = struct.unpack("<fff", data_bytes[offset:offset + 12])
        gyro_samples.append({"x": sample[0], "y": sample[1], "z": sample[2]})
        offset += 12

    data_chunks.append({
        "chunk_index": chunk_index,
        "group": "IMU",
        "TIMESTAMP": timestamp,
        "ACCEL": accel_samples,
        "GYRO": gyro_samples,
    })


# -----------------------------
# HR / RR parsing/validation
# -----------------------------
def plausible_avg_hr(avg_hr):
    return is_valid_float(avg_hr) and 20.0 <= avg_hr <= 260.0


def plausible_rr_values(rr_vals):
    if len(rr_vals) == 0:
        return False

    # very permissive physiological range in ms
    # 250 ms = 240 bpm, 3000 ms = 20 bpm
    if any((rr < 250 or rr > 3000) for rr in rr_vals):
        return False

    # reject packets that are basically all identical junk like 0/65535 etc.
    unique_n = len(set(rr_vals))
    if unique_n == 1 and len(rr_vals) > 3:
        return False

    return True


def try_parse_hr_payload(payload):
    """
    Try both plausible old HR layouts:

    Layout A:
        [float32 avg_hr][uint16 rr_1][uint16 rr_2]...

    Layout B:
        [uint8 array_begin][float32 avg_hr][uint16 rr_1][uint16 rr_2]...

    Returns dict or None.
    """
    candidates = []

    # Layout A: 4 + 2*n
    if len(payload) >= 6 and (len(payload) - 4) % 2 == 0:
        try:
            avg_hr = struct.unpack("<f", payload[0:4])[0]
            rr_count = (len(payload) - 4) // 2
            rr_vals = list(struct.unpack("<" + "H" * rr_count, payload[4:]))
            if plausible_avg_hr(avg_hr) and plausible_rr_values(rr_vals):
                candidates.append({
                    "layout": "avghr_float32__rr_uint16[]",
                    "array_begin": None,
                    "avg_hr_bpm": float(avg_hr),
                    "rr_intervals_ms": rr_vals,
                })
        except Exception:
            pass

    # Layout B: 1 + 4 + 2*n
    if len(payload) >= 7 and (len(payload) - 5) % 2 == 0:
        try:
            array_begin = payload[0]
            avg_hr = struct.unpack("<f", payload[1:5])[0]
            rr_count = (len(payload) - 5) // 2
            rr_vals = list(struct.unpack("<" + "H" * rr_count, payload[5:]))

            # array_begin likely small marker; keep permissive
            if plausible_avg_hr(avg_hr) and plausible_rr_values(rr_vals):
                candidates.append({
                    "layout": "arraybegin_uint8__avghr_float32__rr_uint16[]",
                    "array_begin": int(array_begin),
                    "avg_hr_bpm": float(avg_hr),
                    "rr_intervals_ms": rr_vals,
                })
        except Exception:
            pass

    if not candidates:
        return None

    # Prefer the candidate with:
    # 1) more RR values
    # 2) avg HR consistent with mean RR
    def score(c):
        rr = c["rr_intervals_ms"]
        mean_rr = sum(rr) / len(rr)
        implied_hr = 60000.0 / mean_rr
        consistency = abs(implied_hr - c["avg_hr_bpm"])
        return (len(rr), -consistency)

    best = sorted(candidates, key=score, reverse=True)[0]
    return best


def validate_hr_payload(payload):
    return try_parse_hr_payload(payload) is not None


def parse_HR_RR_chunk(data_bytes, chunk_index, cumulative_time_ms):
    parsed = try_parse_hr_payload(data_bytes)
    if parsed is None:
        return cumulative_time_ms

    rr_vals = parsed["rr_intervals_ms"]
    packet_duration = int(sum(rr_vals))

    data_chunks.append({
        "chunk_index": chunk_index,
        "group": "HR_RR",
        "layout": parsed["layout"],
        "array_begin": parsed["array_begin"],
        "avg_hr_bpm": parsed["avg_hr_bpm"],
        "rr_count": len(rr_vals),
        "rr_intervals_ms": rr_vals,
        "packet_duration_ms": packet_duration,
        # TIMESTAMP will be filled by post-processing interpolation
        "TIMESTAMP": None,
    })

    return cumulative_time_ms + packet_duration


# -----------------------------
# Candidate parsing
# -----------------------------
def read_candidate_header_from_bytes(data, pos):
    if pos >= len(data):
        return None

    start = pos

    # read ID
    first_id = data[pos]
    pos += 1
    if first_id >= ReservedSbemId_e_Escape[0]:
        if pos + 2 > len(data):
            return None
        chunk_id = int.from_bytes(data[pos:pos+2], byteorder="little")
        pos += 2
    else:
        chunk_id = first_id

    # read length
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
        "datasize": datasize,
    }


def try_parse_candidate(data, pos, prev_imu_timestamp=None):
    header = read_candidate_header_from_bytes(data, pos)
    if header is None:
        return None

    chunk_id = header["chunk_id"]
    datasize = header["datasize"]
    payload_pos = header["payload_pos"]

    if chunk_id == ReservedSbemId_e_Descriptor:
        return None

    if datasize <= 0 or payload_pos + datasize > len(data):
        return None

    payload = data[payload_pos:payload_pos + datasize]

    # known IMU packet
    if datasize == 52 and validate_imu_payload(payload):
        timestamp = struct.unpack("<I", payload[0:4])[0]
        if prev_imu_timestamp is not None and not plausible_timestamp(timestamp, prev_imu_timestamp):
            return None
        return {
            "chunk_id": chunk_id,
            "datasize": datasize,
            "payload": payload,
            "kind": "IMU",
            "timestamp": timestamp,
            "start_pos": header["start_pos"],
            "payload_pos": payload_pos,
            "end_pos": payload_pos + datasize,
        }

    # variable-length HR/RR packet
    if validate_hr_payload(payload):
        return {
            "chunk_id": chunk_id,
            "datasize": datasize,
            "payload": payload,
            "kind": "HR_RR",
            "timestamp": None,
            "start_pos": header["start_pos"],
            "payload_pos": payload_pos,
            "end_pos": payload_pos + datasize,
        }

    return None


def find_resync_start(data, start_pos, min_consecutive=3, search_window=200000):
    max_pos = min(len(data) - 32, start_pos + search_window)

    print(f">>> Starting resync scan from offset {start_pos} to {max_pos}")

    pos = start_pos
    scan_count = 0

    while pos < max_pos:
        scan_count += 1
        if scan_count % 10000 == 0:
            print(f"    Scanned {scan_count} positions, currently at {pos}")

        if pos + 64 < len(data):
            chunk = data[pos:pos+64]
            if chunk.count(0) > 60 or chunk.count(0xFF) > 60:
                pos += 32
                continue

        first = try_parse_candidate(data, pos)
        if first is None:
            pos += 1
            continue

        run = [first]
        current = first
        last_imu_ts = first["timestamp"] if first["kind"] == "IMU" else None
        ok = True

        for _ in range(min_consecutive - 1):
            nxt = try_parse_candidate(data, current["end_pos"], last_imu_ts)
            if nxt is None:
                ok = False
                break

            if nxt["kind"] == "IMU":
                if last_imu_ts is not None and nxt["timestamp"] <= last_imu_ts:
                    ok = False
                    break
                last_imu_ts = nxt["timestamp"]

            run.append(nxt)
            current = nxt

        if ok and len(run) >= min_consecutive:
            print(f">>> Found plausible resync run after scanning {scan_count} positions:")
            for i, r in enumerate(run):
                print(
                    f"    run[{i}] offset={r['start_pos']} id={r['chunk_id']} "
                    f"len={r['datasize']} kind={r['kind']}"
                    + (f" ts={r['timestamp']}" if r['timestamp'] is not None else "")
                )
            return run[0]["start_pos"]

        pos += 1

    print(f">>> No resync point found after scanning {scan_count} positions")
    return None


# -----------------------------
# Data-chunk dispatcher
# -----------------------------
def parseDataChunk(chunk_id, data_bytes, data_chunk_index, cumulative_time_ms):
    global data_chunks, unique_chunk_ids, unique_chunk_lengths

    unique_chunk_ids.add(chunk_id)
    unique_chunk_lengths.add(len(data_bytes))

    if len(data_bytes) == 52 and validate_imu_payload(data_bytes):
        parse_MEASIMU6_new(data_bytes, data_chunk_index)
        return cumulative_time_ms

    if validate_hr_payload(data_bytes):
        return parse_HR_RR_chunk(data_bytes, data_chunk_index, cumulative_time_ms)

    # fallback unknown
    data_chunks.append({
        "chunk_index": data_chunk_index,
        "group": "UNKNOWN",
        "chunk_id": chunk_id,
        "payload_len": len(data_bytes),
    })
    return cumulative_time_ms


# -----------------------------
# Main per-file processing
# -----------------------------
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
        if not header.startswith(b"SBEM"):
            print("WARNING: Header does not start with 'SBEM'")

        descriptor_end_pos = bio.tell()
        chunk_index = 0

        # Pass 1: descriptors
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
                print(f"Incomplete chunk read, expected {datasize}, got {len(chunk_bytes)}")
                break

            if chunk_id == ReservedSbemId_e_Descriptor:
                print(f">>> Descriptor chunk at offset {current_offset}, size {datasize}")
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
                print(f">>> First non-descriptor at offset {current_offset}")
                descriptor_end_pos = current_offset
                break

            chunk_index += 1

        print(f">>> Descriptor section ends at offset {descriptor_end_pos}")

        # Pass 2: resync
        print("\n>>> PASS 2: Finding resync point...")
        resync_pos = find_resync_start(
            full_data,
            start_pos=descriptor_end_pos,
            min_consecutive=MIN_CONSECUTIVE_VALID_CHUNKS,
            search_window=RESYNC_SEARCH_WINDOW,
        )

        if resync_pos is None:
            print(">>> ERROR: Could not find valid data chunks")
            return

        print(f">>> Successfully resynchronized at offset {resync_pos}")
        print(f">>> Skipped {resync_pos - descriptor_end_pos} bytes")

        # Pass 3: parse
        print("\n>>> PASS 3: Parsing synchronized data...")
        pos = resync_pos
        data_chunk_index = 0
        last_imu_timestamp = None
        consecutive_failures = 0
        cumulative_time_ms = 0

        while pos < len(full_data):
            if data_chunk_index % PROGRESS_INTERVAL == 0:
                print(
                    f"\n--- Parsed {data_chunk_index} chunks, "
                    f"offset {pos}/{len(full_data)} ({100 * pos // len(full_data)}%) ---"
                )

            candidate = try_parse_candidate(full_data, pos, last_imu_timestamp)

            if candidate is None:
                consecutive_failures += 1

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f">>> Lost sync at offset {pos} after {consecutive_failures} failures")
                    print(">>> Attempting resync...")

                    new_pos = find_resync_start(
                        full_data,
                        start_pos=pos + 1,
                        min_consecutive=MIN_CONSECUTIVE_VALID_CHUNKS,
                        search_window=min(100000, len(full_data) - pos),
                    )

                    if new_pos is None:
                        print(">>> No further valid data found. Stopping.")
                        break

                    print(f">>> Resynced at offset {new_pos}")
                    pos = new_pos
                    consecutive_failures = 0
                    last_imu_timestamp = None
                    continue

                pos += 1
                continue

            consecutive_failures = 0

            if data_chunk_index < VERBOSE_CHUNK_COUNT or data_chunk_index % PROGRESS_INTERVAL == 0:
                msg = (
                    f"    Chunk #{data_chunk_index}: offset={candidate['start_pos']} "
                    f"id={candidate['chunk_id']} kind={candidate['kind']} "
                    f"len={candidate['datasize']}"
                )
                if candidate["timestamp"] is not None:
                    msg += f" ts={candidate['timestamp']}"
                print(msg)

            cumulative_time_ms = parseDataChunk(
                candidate["chunk_id"],
                candidate["payload"],
                data_chunk_index,
                cumulative_time_ms,
            )

            if candidate["kind"] == "IMU":
                last_imu_timestamp = candidate["timestamp"]

            pos = candidate["end_pos"]
            data_chunk_index += 1

        print(f"\n>>> Successfully parsed {data_chunk_index} chunks")

    except Exception as e:
        print("Error processing SBEM file:", e)
        import traceback
        traceback.print_exc()

    print("\n>>> Finished processing file:", file_path)
    print(f"Total chunks parsed: {len(data_chunks)}")
    print("Unique chunk IDs encountered:", unique_chunk_ids)
    print("Unique chunk lengths encountered:", unique_chunk_lengths)


# -----------------------------
# Post-processing: interpolate HR_RR timestamps from IMU
# -----------------------------
def interpolate_hr_timestamps(df):
    """Assign real timestamps to HR_RR packets using surrounding IMU timestamps.

    Each HR_RR packet sits between two IMU packets (gap=1 in chunk_index).
    We linearly interpolate the HR_RR timestamp from the nearest preceding
    and following IMU timestamps based on chunk_index position.

    For HR_RR packets with multiple RR intervals, individual beat timestamps
    are spaced within the interpolated window using cumulative RR durations.
    """
    import numpy as np

    imu = df[df["group"] == "IMU"].copy()
    hr  = df[df["group"] == "HR_RR"].copy()

    if imu.empty or hr.empty:
        print(">>> No IMU or HR_RR data to interpolate")
        return df

    # Build lookup: chunk_index → IMU TIMESTAMP
    imu_ci = imu["chunk_index"].values.astype(np.int64)
    imu_ts = imu["TIMESTAMP"].values.astype(np.float64)

    # For each HR_RR packet, find bracketing IMU timestamps
    hr_ci = hr["chunk_index"].values.astype(np.int64)
    interpolated_ts = np.full(len(hr), np.nan)

    for i, ci in enumerate(hr_ci):
        # Find last IMU before this chunk
        mask_before = imu_ci < ci
        mask_after  = imu_ci > ci

        if mask_before.any() and mask_after.any():
            # Bracketed: interpolate
            idx_b = np.flatnonzero(mask_before)[-1]
            idx_a = np.flatnonzero(mask_after)[0]
            ts_b, ci_b = imu_ts[idx_b], imu_ci[idx_b]
            ts_a, ci_a = imu_ts[idx_a], imu_ci[idx_a]
            # Linear interpolation by chunk_index position
            if ci_a > ci_b:
                frac = (ci - ci_b) / (ci_a - ci_b)
                interpolated_ts[i] = ts_b + frac * (ts_a - ts_b)
            else:
                interpolated_ts[i] = ts_b
        elif mask_before.any():
            # After last IMU: extrapolate using last IMU gap
            idx_b = np.flatnonzero(mask_before)[-1]
            if idx_b > 0:
                dt = imu_ts[idx_b] - imu_ts[idx_b - 1]
                gap = ci - imu_ci[idx_b]
                interpolated_ts[i] = imu_ts[idx_b] + gap * dt
            else:
                interpolated_ts[i] = imu_ts[idx_b]
        elif mask_after.any():
            # Before first IMU: extrapolate backwards
            idx_a = np.flatnonzero(mask_after)[0]
            if idx_a + 1 < len(imu_ts):
                dt = imu_ts[idx_a + 1] - imu_ts[idx_a]
                gap = imu_ci[idx_a] - ci
                interpolated_ts[i] = imu_ts[idx_a] - gap * dt
            else:
                interpolated_ts[i] = imu_ts[idx_a]

    # Write back
    hr_indices = hr.index
    df.loc[hr_indices, "TIMESTAMP"] = interpolated_ts

    n_ok = np.isfinite(interpolated_ts).sum()
    print(f">>> Interpolated timestamps for {n_ok}/{len(hr)} HR_RR packets")

    return df


# -----------------------------
# CLI
# -----------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Parse older SBEM files with RR intervals + average HR."
    )
    parser.add_argument("folder", help="Folder containing SBEM files")
    parser.add_argument("--output-suffix", default="_parsed", help="Suffix for output CSV")
    args = parser.parse_args()

    sbem_files = glob.glob(os.path.join(args.folder, "*.sbem"))
    if not sbem_files:
        print("No SBEM files found in:", args.folder)
        return

    for sbem_file in sbem_files:
        print("\n" + "=" * 80)
        print("Processing file:", sbem_file)
        print("=" * 80)

        processSBEM(sbem_file)

        if len(data_chunks) > 0:
            df = pd.json_normalize(data_chunks)

            # Interpolate HR_RR timestamps from surrounding IMU packets
            df = interpolate_hr_timestamps(df)

            base_name = os.path.splitext(os.path.basename(sbem_file))[0]
            out_csv = os.path.join(args.folder, base_name + args.output_suffix + ".csv")
            df.to_csv(out_csv, index=False)
            print(f"\n>>> Saved {len(df)} rows to: {out_csv}")

            # Summary
            imu_n = (df["group"] == "IMU").sum()
            hr_n  = (df["group"] == "HR_RR").sum()
            hr_with_ts = df.loc[df["group"] == "HR_RR", "TIMESTAMP"].notna().sum()
            print(f"    IMU packets: {imu_n}")
            print(f"    HR_RR packets: {hr_n} ({hr_with_ts} with interpolated timestamps)")
            if hr_n > 0:
                ts_range = df.loc[df["group"] == "HR_RR", "TIMESTAMP"]
                ts_ok = ts_range.dropna()
                if len(ts_ok) > 1:
                    dur_min = (ts_ok.max() - ts_ok.min()) / 60000.0
                    print(f"    HR_RR time span: {dur_min:.1f} minutes")
        else:
            print("\n>>> WARNING: No data chunks found, CSV not created")


if __name__ == "__main__":
    main()