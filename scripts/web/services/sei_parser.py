"""
Tesla Dashcam SEI (Supplemental Enhancement Information) Parser.

Extracts GPS coordinates, speed, acceleration, steering, autopilot state, and other
telemetry data embedded as protobuf-encoded SEI NAL units in Tesla dashcam MP4 files.

This is a pure-Python port of the client-side JavaScript parser in dashcam-mp4.js.
Designed for low-memory operation on Pi Zero 2 W (512MB RAM).

Usage:
    from services.sei_parser import extract_sei_messages, parse_video_sei

    # Generator-based (memory-efficient):
    for msg in extract_sei_messages('/path/to/video.mp4', sample_rate=30):
        print(f"Frame {msg.frame_index}: lat={msg.latitude_deg}, lon={msg.longitude_deg}")

    # Or get all at once:
    messages = parse_video_sei('/path/to/video.mp4')
"""

import logging
import os
import struct
from dataclasses import dataclass
from typing import Generator, List, Optional

logger = logging.getLogger(__name__)

# Lazy-load protobuf to avoid import cost when not needed
_SeiMetadata = None


def _get_sei_metadata_class():
    """Lazy-load the compiled protobuf class."""
    global _SeiMetadata
    if _SeiMetadata is None:
        from services.dashcam_pb2 import SeiMetadata
        _SeiMetadata = SeiMetadata
    return _SeiMetadata


# --- Data classes for parsed results ---

@dataclass
class SeiMessage:
    """Parsed SEI telemetry from a single video frame."""
    frame_index: int
    timestamp_ms: float
    # GPS
    latitude_deg: float
    longitude_deg: float
    heading_deg: float
    # Motion
    vehicle_speed_mps: float
    linear_acceleration_x: float
    linear_acceleration_y: float
    linear_acceleration_z: float
    # Controls
    steering_wheel_angle: float
    accelerator_pedal_position: float
    brake_applied: bool
    # State
    gear_state: str  # 'PARK', 'DRIVE', 'REVERSE', 'NEUTRAL'
    autopilot_state: str  # 'NONE', 'SELF_DRIVING', 'AUTOSTEER', 'TACC'
    blinker_on_left: bool
    blinker_on_right: bool
    # Raw
    frame_seq_no: int
    video_path: str

    @property
    def has_gps(self) -> bool:
        """Check if this message has valid GPS coordinates."""
        return (self.latitude_deg != 0.0 or self.longitude_deg != 0.0)

    @property
    def speed_mph(self) -> float:
        """Speed in miles per hour."""
        return abs(self.vehicle_speed_mps) * 2.23694

    @property
    def speed_kph(self) -> float:
        """Speed in kilometers per hour."""
        return abs(self.vehicle_speed_mps) * 3.6


# Gear and autopilot enum mappings (match dashcam.proto)
_GEAR_NAMES = {0: 'PARK', 1: 'DRIVE', 2: 'REVERSE', 3: 'NEUTRAL'}
_AUTOPILOT_NAMES = {0: 'NONE', 1: 'SELF_DRIVING', 2: 'AUTOSTEER', 3: 'TACC'}


# --- MP4 Box Parsing ---

def _find_box(data: bytes, start: int, end: int, name: str) -> Optional[dict]:
    """Find an MP4 box by 4-char name within a byte range.

    Returns dict with 'start' (content start), 'end', 'size' (content size),
    or None if not found.
    """
    pos = start
    name_bytes = name.encode('ascii')

    while pos + 8 <= end:
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]

        if size == 1:
            # Extended size (64-bit)
            if pos + 16 > end:
                break
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
            header_size = 16
        elif size == 0:
            # Box extends to end of data
            size = end - pos
            header_size = 8
        else:
            header_size = 8

        if size < header_size:
            break

        # Clamp box to actual data bounds (malicious files may claim larger)
        if pos + size > end:
            # If this is the box we're looking for, clamp its size
            if box_type == name_bytes:
                size = end - pos
            else:
                break

        if box_type == name_bytes:
            return {
                'start': pos + header_size,
                'end': pos + size,
                'size': size - header_size
            }

        pos += size

    return None


def _find_box_required(data: bytes, start: int, end: int, name: str) -> dict:
    """Find an MP4 box, raising ValueError if not found."""
    box = _find_box(data, start, end, name)
    if box is None:
        raise ValueError(f'MP4 box "{name}" not found')
    return box


# --- H.264 NAL Unit Parsing ---

def _strip_emulation_prevention_bytes(data: bytes) -> bytes:
    """Remove H.264 emulation prevention bytes (0x03 after 0x0000).

    H.264 inserts 0x03 bytes to prevent start code emulation (0x000001).
    These must be removed before decoding the protobuf payload.
    """
    out = bytearray()
    zeros = 0

    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0

    return bytes(out)


def _decode_sei_nal(nal_data: bytes) -> Optional[object]:
    """Decode a SEI NAL unit to a protobuf SeiMetadata message.

    Tesla SEI NAL structure:
    - Bytes 0-2: NAL header + padding (0x42 bytes)
    - Variable 0x42 padding bytes
    - Payload type marker: 0x69
    - Protobuf payload (with emulation prevention bytes)
    - Trailing RBSP byte (0x80)
    """
    if len(nal_data) < 4:
        return None

    # Skip first 3 bytes, then skip 0x42 padding
    i = 3
    while i < len(nal_data) and nal_data[i] == 0x42:
        i += 1

    # Must have had at least one 0x42 padding byte, and next byte must be 0x69
    if i <= 3 or i + 1 >= len(nal_data) or nal_data[i] != 0x69:
        return None

    try:
        # Extract protobuf payload: after 0x69 marker, before trailing byte
        payload = nal_data[i + 1:len(nal_data) - 1]
        clean_payload = _strip_emulation_prevention_bytes(payload)

        SeiMetadata = _get_sei_metadata_class()
        return SeiMetadata.FromString(clean_payload)
    except Exception:
        return None


def _get_timescale_and_durations(data: bytes) -> tuple:
    """Extract timescale and frame durations from MP4 moov box.

    Returns (timescale, durations_ms_list).
    """
    moov = _find_box_required(data, 0, len(data), 'moov')
    trak = _find_box_required(data, moov['start'], moov['end'], 'trak')
    mdia = _find_box_required(data, trak['start'], trak['end'], 'mdia')

    # Get timescale from mdhd box
    mdhd = _find_box_required(data, mdia['start'], mdia['end'], 'mdhd')
    mdhd_version = data[mdhd['start']]
    if mdhd_version == 1:
        timescale = struct.unpack('>I', data[mdhd['start'] + 20:mdhd['start'] + 24])[0]
    else:
        timescale = struct.unpack('>I', data[mdhd['start'] + 12:mdhd['start'] + 16])[0]

    if timescale == 0:
        timescale = 30000  # Fallback default

    # Get frame durations from stts (Sample-to-Time box)
    minf = _find_box_required(data, mdia['start'], mdia['end'], 'minf')
    stbl = _find_box_required(data, minf['start'], minf['end'], 'stbl')
    stts = _find_box_required(data, stbl['start'], stbl['end'], 'stts')

    entry_count = struct.unpack('>I', data[stts['start'] + 4:stts['start'] + 8])[0]

    # Sanity check: Tesla clips are ~30-60s at 30fps ≈ 1800 frames max.
    # Allow generous headroom but prevent malicious values.
    if entry_count > 50000:
        logger.warning("Suspicious stts entry_count %d in video, using fallback", entry_count)
        return timescale, []

    MAX_TOTAL_SAMPLES = 10000  # Cap total samples to prevent memory exhaustion
    durations = []
    pos = stts['start'] + 8
    for _ in range(entry_count):
        if pos + 8 > stts['end']:
            break
        count = struct.unpack('>I', data[pos:pos + 4])[0]
        delta = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        remaining = MAX_TOTAL_SAMPLES - len(durations)
        if remaining <= 0:
            logger.warning("stts total samples capped at %d", MAX_TOTAL_SAMPLES)
            break
        if count > remaining:
            count = remaining
        duration_ms = (delta / timescale) * 1000
        durations.extend([duration_ms] * count)
        pos += 8

    return timescale, durations


# --- Public API ---

def extract_sei_messages(
    video_path: str,
    sample_rate: int = 1
) -> Generator[SeiMessage, None, None]:
    """Extract SEI telemetry messages from a Tesla dashcam MP4 file.

    Generator-based for memory efficiency on Pi Zero 2 W. Reads the file
    once and yields SeiMessage objects for frames that contain SEI data.

    Args:
        video_path: Path to the MP4 file.
        sample_rate: Only process every Nth frame (1=all, 30=~1/sec at 30fps).
            Use 1 for maximum resolution, 30 for route mapping.

    Yields:
        SeiMessage objects with GPS, speed, acceleration, and control data.

    Raises:
        FileNotFoundError: If video_path doesn't exist.
        ValueError: If the file is not a valid MP4 with H.264 video.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    file_size = os.path.getsize(video_path)
    if file_size < 8:
        raise ValueError(f"File too small to be a valid MP4: {video_path}")

    max_file_size = 150 * 1024 * 1024  # 150 MB
    if file_size > max_file_size:
        raise ValueError(
            f"File too large ({file_size / 1024 / 1024:.0f} MB) — "
            f"max {max_file_size // 1024 // 1024} MB: {video_path}"
        )

    # Read entire file into memory (Tesla clips are typically 30-60 seconds,
    # ~30-80MB each). For Pi Zero 2 W, process one file at a time.
    with open(video_path, 'rb') as f:
        data = f.read()

    # Parse timing information from moov box
    try:
        timescale, durations = _get_timescale_and_durations(data)
    except ValueError as e:
        logger.warning("Could not parse MP4 metadata for %s: %s", video_path, e)
        # Fall back to default timing (33ms per frame = ~30fps)
        timescale = 30000
        durations = []
    default_duration_ms = 33.33  # ~30fps fallback

    # Find mdat box (contains video data)
    mdat = _find_box(data, 0, len(data), 'mdat')
    if mdat is None:
        raise ValueError(f"No mdat box found in {video_path}")

    # Walk through NAL units in mdat
    cursor = mdat['start']
    end = mdat['end']
    frame_index = 0
    cumulative_time_ms = 0.0

    while cursor + 4 <= end:
        # Read 4-byte big-endian NAL unit length
        nal_size = struct.unpack('>I', data[cursor:cursor + 4])[0]
        cursor += 4

        if nal_size < 1 or cursor + nal_size > len(data):
            break

        # Extract NAL unit type (lower 5 bits of first byte)
        nal_type = data[cursor] & 0x1F

        if nal_type == 6:
            # SEI NAL unit — check if this is a sampled frame
            if frame_index % sample_rate == 0:
                nal_data = data[cursor:cursor + nal_size]
                # Quick check: payload type 5 (user data unregistered)
                if nal_size >= 2 and nal_data[1] == 5:
                    sei = _decode_sei_nal(nal_data)
                    if sei is not None:
                        # Get frame duration
                        if frame_index < len(durations):
                            duration_ms = durations[frame_index]
                        else:
                            duration_ms = default_duration_ms

                        yield SeiMessage(
                            frame_index=frame_index,
                            timestamp_ms=cumulative_time_ms,
                            latitude_deg=sei.latitude_deg,
                            longitude_deg=sei.longitude_deg,
                            heading_deg=sei.heading_deg,
                            vehicle_speed_mps=sei.vehicle_speed_mps,
                            linear_acceleration_x=sei.linear_acceleration_mps2_x,
                            linear_acceleration_y=sei.linear_acceleration_mps2_y,
                            linear_acceleration_z=sei.linear_acceleration_mps2_z,
                            steering_wheel_angle=sei.steering_wheel_angle,
                            accelerator_pedal_position=sei.accelerator_pedal_position,
                            brake_applied=sei.brake_applied,
                            gear_state=_GEAR_NAMES.get(sei.gear_state, 'UNKNOWN'),
                            autopilot_state=_AUTOPILOT_NAMES.get(
                                sei.autopilot_state, 'UNKNOWN'
                            ),
                            blinker_on_left=sei.blinker_on_left,
                            blinker_on_right=sei.blinker_on_right,
                            frame_seq_no=sei.frame_seq_no,
                            video_path=video_path,
                        )

        elif nal_type == 5 or nal_type == 1:
            # IDR (keyframe) or non-IDR slice — advance frame counter and timing
            if frame_index < len(durations):
                cumulative_time_ms += durations[frame_index]
            else:
                cumulative_time_ms += default_duration_ms
            frame_index += 1

        cursor += nal_size


def parse_video_sei(
    video_path: str,
    sample_rate: int = 1
) -> List[SeiMessage]:
    """Parse all SEI messages from a video file into a list.

    Convenience wrapper around extract_sei_messages() for when you need
    all messages at once. For large-scale indexing, prefer the generator.

    Args:
        video_path: Path to the MP4 file.
        sample_rate: Only process every Nth frame (1=all, 30=~1/sec at 30fps).

    Returns:
        List of SeiMessage objects.
    """
    return list(extract_sei_messages(video_path, sample_rate))


def get_video_gps_summary(video_path: str) -> Optional[dict]:
    """Get a quick GPS summary from a video file (first and last GPS points).

    Samples only the first and last few seconds of the video for speed.
    Returns None if no GPS data is found.

    Args:
        video_path: Path to the MP4 file.

    Returns:
        Dict with 'start_lat', 'start_lon', 'end_lat', 'end_lon',
        'start_heading', 'end_heading', 'frame_count', or None.
    """
    try:
        messages = list(extract_sei_messages(video_path, sample_rate=30))
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Cannot get GPS summary for %s: %s", video_path, e)
        return None

    # Filter to messages with valid GPS
    gps_messages = [m for m in messages if m.has_gps]

    if not gps_messages:
        return None

    first = gps_messages[0]
    last = gps_messages[-1]

    return {
        'start_lat': first.latitude_deg,
        'start_lon': first.longitude_deg,
        'start_heading': first.heading_deg,
        'end_lat': last.latitude_deg,
        'end_lon': last.longitude_deg,
        'end_heading': last.heading_deg,
        'frame_count': len(gps_messages),
        'duration_ms': last.timestamp_ms - first.timestamp_ms,
    }


# --- CLI usage ---

if __name__ == '__main__':
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python sei_parser.py <video.mp4> [sample_rate]")
        print("  sample_rate: 1=every frame, 30=~1/sec (default: 30)")
        sys.exit(1)

    path = sys.argv[1]
    rate = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    count = 0
    for msg in extract_sei_messages(path, sample_rate=rate):
        if msg.has_gps:
            print(json.dumps({
                'frame': msg.frame_index,
                'time_ms': round(msg.timestamp_ms, 1),
                'lat': msg.latitude_deg,
                'lon': msg.longitude_deg,
                'heading': round(msg.heading_deg, 1),
                'speed_mph': round(msg.speed_mph, 1),
                'gear': msg.gear_state,
                'autopilot': msg.autopilot_state,
                'brake': msg.brake_applied,
                'steering': round(msg.steering_wheel_angle, 1),
            }))
            count += 1

    print(f"\n--- Extracted {count} GPS-tagged SEI messages from {path} ---",
          file=sys.stderr)
