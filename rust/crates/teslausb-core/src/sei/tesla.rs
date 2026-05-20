//! Tesla `SeiMetadata` protobuf decoder (Phase 4b.1c).
#![allow(clippy::doc_markdown)] // domain terms ("SEI", "Tesla", "GPS") need not be backticked
//!
//! Hand-rolled minimal protobuf wire-format decoder targeted
//! at the `SeiMetadata` schema defined in v1's
//! `scripts/web/static/dashcam.proto`. We deliberately avoid
//! pulling in `prost` / `prost-build` / `protoc` to keep the
//! Pi cross-build pipeline minimal: `dashcam.proto` is a
//! frozen 16-field schema that has not changed across Tesla
//! firmware revisions since 2024, the wire format is trivial
//! (4 wire types, 16 known fields, no nesting, no repeated
//! fields, no `repeated packed`), and a hand-roll is ~250
//! lines vs. a build-script dep chain that requires `protoc`
//! on the host (`prost-build` will hard-fail on a system
//! without `protoc`).
//!
//! ## Schema (frozen)
//!
//! From v1 (`scripts/web/static/dashcam.proto`):
//!
//! | # | Wire | Rust type | Tesla name |
//! |---|------|-----------|-----------|
//! | 1 | varint | `u32` | `version` |
//! | 2 | varint | [`Gear`] | `gear_state` |
//! | 3 | varint | `u64` | `frame_seq_no` |
//! | 4 | fixed32 | `f32` | `vehicle_speed_mps` |
//! | 5 | fixed32 | `f32` | `accelerator_pedal_position` |
//! | 6 | fixed32 | `f32` | `steering_wheel_angle` |
//! | 7 | varint | `bool` | `blinker_on_left` |
//! | 8 | varint | `bool` | `blinker_on_right` |
//! | 9 | varint | `bool` | `brake_applied` |
//! | 10 | varint | [`AutopilotState`] | `autopilot_state` |
//! | 11 | fixed64 | `f64` | `latitude_deg` |
//! | 12 | fixed64 | `f64` | `longitude_deg` |
//! | 13 | fixed64 | `f64` | `heading_deg` |
//! | 14 | fixed64 | `f64` | `linear_acceleration_mps2_x` |
//! | 15 | fixed64 | `f64` | `linear_acceleration_mps2_y` |
//! | 16 | fixed64 | `f64` | `linear_acceleration_mps2_z` |
//!
//! ## proto3 default semantics
//!
//! Per the proto3 specification, scalar fields default to
//! their zero value when absent from the wire. We follow that
//! convention here ([`SeiMessage::default`] is the all-zeros /
//! all-`PARK`/`NONE` message). The Phase 4b.2 indexer applies
//! the "GPS present" heuristic (lat ≠ 0 OR lon ≠ 0) — this
//! matches v1's behaviour, where `metadata.latitude_deg == 0
//! and metadata.longitude_deg == 0` is treated as "no GPS lock".
//!
//! Unknown fields (any tag not in the table above) are
//! silently skipped per proto3 forward-compatibility rules.

use std::fmt;

/// Tesla gear-state enum. Matches the four `Gear` variants in
/// `dashcam.proto`. Unknown enum integers are decoded to
/// [`Self::Unknown`] rather than rejected, mirroring proto3
/// forward-compatibility semantics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Gear {
    /// `GEAR_PARK = 0` — also the proto3 default.
    #[default]
    Park,
    /// `GEAR_DRIVE = 1`.
    Drive,
    /// `GEAR_REVERSE = 2`.
    Reverse,
    /// `GEAR_NEUTRAL = 3`.
    Neutral,
    /// Any enum integer Tesla introduces after 2024 lands here.
    Unknown(u32),
}

impl From<u32> for Gear {
    fn from(v: u32) -> Self {
        match v {
            0 => Self::Park,
            1 => Self::Drive,
            2 => Self::Reverse,
            3 => Self::Neutral,
            other => Self::Unknown(other),
        }
    }
}

/// Tesla autopilot-state enum. Matches the four
/// `AutopilotState` variants in `dashcam.proto`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AutopilotState {
    /// `NONE = 0` — also the proto3 default.
    #[default]
    None,
    /// `SELF_DRIVING = 1`.
    SelfDriving,
    /// `AUTOSTEER = 2`.
    Autosteer,
    /// `TACC = 3` — Traffic-Aware Cruise Control.
    Tacc,
    /// Any enum integer Tesla introduces after 2024 lands here.
    Unknown(u32),
}

impl From<u32> for AutopilotState {
    fn from(v: u32) -> Self {
        match v {
            0 => Self::None,
            1 => Self::SelfDriving,
            2 => Self::Autosteer,
            3 => Self::Tacc,
            other => Self::Unknown(other),
        }
    }
}

/// One decoded SEI payload. Field defaults follow proto3:
/// numeric → 0, bool → false, enum → first variant
/// (`Park` / `None`).
///
/// `latitude_deg == 0.0 && longitude_deg == 0.0` is the
/// standard "GPS not yet locked" sentinel — the indexer
/// (Phase 4b.2) filters on this to decide whether a clip is
/// worth preserving past the retention threshold.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct SeiMessage {
    /// Schema version Tesla stamps on every message.
    pub version: u32,
    /// Gear position at the moment the frame was captured.
    pub gear_state: Gear,
    /// Monotonically increasing frame counter.
    pub frame_seq_no: u64,
    /// Vehicle speed in metres per second.
    pub vehicle_speed_mps: f32,
    /// Accelerator pedal position, 0.0 (released) .. 1.0
    /// (floored).
    pub accelerator_pedal_position: f32,
    /// Steering-wheel angle in radians (sign convention
    /// matches Tesla's: positive = left).
    pub steering_wheel_angle: f32,
    /// Left-turn-signal lamp state.
    pub blinker_on_left: bool,
    /// Right-turn-signal lamp state.
    pub blinker_on_right: bool,
    /// Brake-pedal-engaged flag.
    pub brake_applied: bool,
    /// Active autopilot mode.
    pub autopilot_state: AutopilotState,
    /// WGS-84 latitude, degrees.
    pub latitude_deg: f64,
    /// WGS-84 longitude, degrees.
    pub longitude_deg: f64,
    /// Compass heading, degrees (0 = north, clockwise).
    pub heading_deg: f64,
    /// Linear acceleration X, m/s².
    pub linear_acceleration_mps2_x: f64,
    /// Linear acceleration Y, m/s².
    pub linear_acceleration_mps2_y: f64,
    /// Linear acceleration Z, m/s².
    pub linear_acceleration_mps2_z: f64,
}

impl SeiMessage {
    /// Convenience: returns `true` if either lat or lon is
    /// non-zero (i.e. the GPS receiver had at least a partial
    /// fix when this frame was emitted). Mirrors the
    /// "preserve clip if GPS-tagged" rule the v1 cleanup worker
    /// applies.
    #[must_use]
    pub fn has_gps_fix(&self) -> bool {
        self.latitude_deg != 0.0 || self.longitude_deg != 0.0
    }
}

/// Errors emitted by the protobuf decoder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtoError {
    /// A varint took more than 10 bytes (the maximum valid
    /// length for a 64-bit varint).
    VarintTooLong,
    /// The buffer ended before a length-prefixed value
    /// (varint, fixed32, or fixed64) was fully consumed.
    UnexpectedEnd,
    /// A length-delimited field declared a length that does
    /// not fit in `usize`. Effectively only triggers on 32-bit
    /// hosts with malicious input.
    LengthOverflow,
    /// The wire-type bits of a tag are not one of the four
    /// proto3 wire types we recognise (0, 1, 2, 5). Group
    /// wire types (3, 4) are deprecated; Tesla does not emit
    /// them.
    UnknownWireType {
        /// The 3-bit wire type that was not recognised.
        wire_type: u8,
    },
    /// A tag with `field_number == 0` was encountered.
    /// proto3 forbids field 0; this indicates corruption.
    InvalidFieldNumber,
}

impl fmt::Display for ProtoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::VarintTooLong => f.write_str("protobuf varint exceeded 10 bytes"),
            Self::UnexpectedEnd => f.write_str("protobuf buffer ended mid-field"),
            Self::LengthOverflow => f.write_str("protobuf length-delimited field overflowed usize"),
            Self::UnknownWireType { wire_type } => {
                write!(f, "protobuf unknown wire type {wire_type}")
            }
            Self::InvalidFieldNumber => f.write_str("protobuf field number 0 is reserved"),
        }
    }
}

impl std::error::Error for ProtoError {}

const WIRE_VARINT: u8 = 0;
const WIRE_FIXED64: u8 = 1;
const WIRE_LENGTH_DELIMITED: u8 = 2;
const WIRE_FIXED32: u8 = 5;

/// Decode a `SeiMetadata` protobuf message.
///
/// `buf` is the protobuf wire bytes, e.g. the output of
/// [`super::payload::extract_tesla_payload`]. Returns a
/// fully-populated [`SeiMessage`]; unspecified fields take
/// their proto3 defaults.
///
/// # Errors
///
/// Returns the appropriate [`ProtoError`] variant on
/// malformed wire bytes. Unknown field numbers are skipped
/// silently per proto3 forward-compatibility — they are not
/// errors.
pub fn decode_sei_message(buf: &[u8]) -> Result<SeiMessage, ProtoError> {
    let mut msg = SeiMessage::default();
    let mut cur = Cursor::new(buf);
    while !cur.is_empty() {
        let tag = cur.read_varint()?;
        let wire_type =
            u8::try_from(tag & 0x07).map_err(|_| ProtoError::UnknownWireType { wire_type: 0 })?;
        let field_number = u32::try_from(tag >> 3).map_err(|_| ProtoError::InvalidFieldNumber)?;
        if field_number == 0 {
            return Err(ProtoError::InvalidFieldNumber);
        }
        match (field_number, wire_type) {
            (1, WIRE_VARINT) => {
                msg.version = u32::try_from(cur.read_varint()?).unwrap_or(u32::MAX);
            }
            (2, WIRE_VARINT) => {
                msg.gear_state = Gear::from(u32::try_from(cur.read_varint()?).unwrap_or(u32::MAX));
            }
            (3, WIRE_VARINT) => msg.frame_seq_no = cur.read_varint()?,
            (4, WIRE_FIXED32) => msg.vehicle_speed_mps = f32::from_bits(cur.read_fixed32()?),
            (5, WIRE_FIXED32) => {
                msg.accelerator_pedal_position = f32::from_bits(cur.read_fixed32()?);
            }
            (6, WIRE_FIXED32) => msg.steering_wheel_angle = f32::from_bits(cur.read_fixed32()?),
            (7, WIRE_VARINT) => msg.blinker_on_left = cur.read_varint()? != 0,
            (8, WIRE_VARINT) => msg.blinker_on_right = cur.read_varint()? != 0,
            (9, WIRE_VARINT) => msg.brake_applied = cur.read_varint()? != 0,
            (10, WIRE_VARINT) => {
                msg.autopilot_state =
                    AutopilotState::from(u32::try_from(cur.read_varint()?).unwrap_or(u32::MAX));
            }
            (11, WIRE_FIXED64) => msg.latitude_deg = f64::from_bits(cur.read_fixed64()?),
            (12, WIRE_FIXED64) => msg.longitude_deg = f64::from_bits(cur.read_fixed64()?),
            (13, WIRE_FIXED64) => msg.heading_deg = f64::from_bits(cur.read_fixed64()?),
            (14, WIRE_FIXED64) => {
                msg.linear_acceleration_mps2_x = f64::from_bits(cur.read_fixed64()?);
            }
            (15, WIRE_FIXED64) => {
                msg.linear_acceleration_mps2_y = f64::from_bits(cur.read_fixed64()?);
            }
            (16, WIRE_FIXED64) => {
                msg.linear_acceleration_mps2_z = f64::from_bits(cur.read_fixed64()?);
            }
            // Unknown field number or wrong wire type for a
            // known field number — skip per proto3
            // forward-compatibility rules. A wrong wire type
            // on a KNOWN field is rare in practice (would mean
            // Tesla retyped a field in place); skipping rather
            // than rejecting maximises tolerance.
            _ => cur.skip_field(wire_type)?,
        }
    }
    Ok(msg)
}

/// Internal helper: byte-by-byte cursor over a wire-format buffer.
struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    fn is_empty(&self) -> bool {
        self.pos >= self.buf.len()
    }

    fn remaining(&self) -> usize {
        self.buf.len().saturating_sub(self.pos)
    }

    fn read_u8(&mut self) -> Result<u8, ProtoError> {
        let b = *self.buf.get(self.pos).ok_or(ProtoError::UnexpectedEnd)?;
        self.pos = self.pos.saturating_add(1);
        Ok(b)
    }

    fn read_varint(&mut self) -> Result<u64, ProtoError> {
        let mut result: u64 = 0;
        for shift in 0..10_u32 {
            let b = self.read_u8()?;
            result |= u64::from(b & 0x7F) << (shift * 7);
            if b & 0x80 == 0 {
                return Ok(result);
            }
        }
        Err(ProtoError::VarintTooLong)
    }

    fn read_fixed32(&mut self) -> Result<u32, ProtoError> {
        if self.remaining() < 4 {
            return Err(ProtoError::UnexpectedEnd);
        }
        let slice = self
            .buf
            .get(self.pos..self.pos.saturating_add(4))
            .ok_or(ProtoError::UnexpectedEnd)?;
        let arr: [u8; 4] = slice.try_into().map_err(|_| ProtoError::UnexpectedEnd)?;
        self.pos = self.pos.saturating_add(4);
        Ok(u32::from_le_bytes(arr))
    }

    fn read_fixed64(&mut self) -> Result<u64, ProtoError> {
        if self.remaining() < 8 {
            return Err(ProtoError::UnexpectedEnd);
        }
        let slice = self
            .buf
            .get(self.pos..self.pos.saturating_add(8))
            .ok_or(ProtoError::UnexpectedEnd)?;
        let arr: [u8; 8] = slice.try_into().map_err(|_| ProtoError::UnexpectedEnd)?;
        self.pos = self.pos.saturating_add(8);
        Ok(u64::from_le_bytes(arr))
    }

    fn skip_field(&mut self, wire_type: u8) -> Result<(), ProtoError> {
        match wire_type {
            WIRE_VARINT => {
                let _ = self.read_varint()?;
                Ok(())
            }
            WIRE_FIXED64 => {
                let _ = self.read_fixed64()?;
                Ok(())
            }
            WIRE_LENGTH_DELIMITED => {
                let len = self.read_varint()?;
                let len_usize = usize::try_from(len).map_err(|_| ProtoError::LengthOverflow)?;
                if self.remaining() < len_usize {
                    return Err(ProtoError::UnexpectedEnd);
                }
                self.pos = self.pos.saturating_add(len_usize);
                Ok(())
            }
            WIRE_FIXED32 => {
                let _ = self.read_fixed32()?;
                Ok(())
            }
            other => Err(ProtoError::UnknownWireType { wire_type: other }),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::float_cmp,
        clippy::cast_possible_truncation
    )]

    use super::*;

    // ───────────────────── encoder helpers ─────────────────────
    // Used to build fixture wire bytes in tests. NOT exposed in
    // the public API — Tesla writes the SEI on the camera side,
    // we only decode.

    fn encode_varint(mut value: u64, out: &mut Vec<u8>) {
        while value >= 0x80 {
            out.push(((value as u8) & 0x7F) | 0x80);
            value >>= 7;
        }
        out.push(value as u8);
    }

    fn encode_tag(field_number: u32, wire_type: u8, out: &mut Vec<u8>) {
        encode_varint((u64::from(field_number) << 3) | u64::from(wire_type), out);
    }

    fn encode_field_varint(field: u32, value: u64, out: &mut Vec<u8>) {
        encode_tag(field, WIRE_VARINT, out);
        encode_varint(value, out);
    }

    fn encode_field_fixed32(field: u32, value: u32, out: &mut Vec<u8>) {
        encode_tag(field, WIRE_FIXED32, out);
        out.extend_from_slice(&value.to_le_bytes());
    }

    fn encode_field_fixed64(field: u32, value: u64, out: &mut Vec<u8>) {
        encode_tag(field, WIRE_FIXED64, out);
        out.extend_from_slice(&value.to_le_bytes());
    }

    fn encode_field_f32(field: u32, value: f32, out: &mut Vec<u8>) {
        encode_field_fixed32(field, value.to_bits(), out);
    }

    fn encode_field_f64(field: u32, value: f64, out: &mut Vec<u8>) {
        encode_field_fixed64(field, value.to_bits(), out);
    }

    // ───────────────────── enum decoding ───────────────────────

    #[test]
    fn gear_from_known_values_maps_correctly() {
        assert_eq!(Gear::from(0), Gear::Park);
        assert_eq!(Gear::from(1), Gear::Drive);
        assert_eq!(Gear::from(2), Gear::Reverse);
        assert_eq!(Gear::from(3), Gear::Neutral);
    }

    #[test]
    fn gear_from_unknown_value_is_preserved() {
        assert_eq!(Gear::from(99), Gear::Unknown(99));
    }

    #[test]
    fn autopilot_from_known_values_maps_correctly() {
        assert_eq!(AutopilotState::from(0), AutopilotState::None);
        assert_eq!(AutopilotState::from(1), AutopilotState::SelfDriving);
        assert_eq!(AutopilotState::from(2), AutopilotState::Autosteer);
        assert_eq!(AutopilotState::from(3), AutopilotState::Tacc);
    }

    #[test]
    fn autopilot_from_unknown_value_is_preserved() {
        assert_eq!(AutopilotState::from(7), AutopilotState::Unknown(7));
    }

    // ───────────────────── cursor primitives ───────────────────

    #[test]
    fn cursor_read_varint_single_byte() {
        let buf = [0x07_u8];
        let mut c = Cursor::new(&buf);
        assert_eq!(c.read_varint().unwrap(), 7);
        assert!(c.is_empty());
    }

    #[test]
    fn cursor_read_varint_multi_byte() {
        // 300 = 0xAC 0x02
        let buf = [0xAC, 0x02];
        let mut c = Cursor::new(&buf);
        assert_eq!(c.read_varint().unwrap(), 300);
    }

    #[test]
    fn cursor_read_varint_max_u64() {
        // 10 bytes, all-ones-with-continuation-bits-set produces u64::MAX.
        let buf = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01];
        let mut c = Cursor::new(&buf);
        assert_eq!(c.read_varint().unwrap(), u64::MAX);
    }

    #[test]
    fn cursor_read_varint_too_long_rejected() {
        // 11 bytes all with continuation bit set → reject.
        let buf = [0xFF_u8; 11];
        let mut c = Cursor::new(&buf);
        assert!(matches!(c.read_varint(), Err(ProtoError::VarintTooLong)));
    }

    #[test]
    fn cursor_read_varint_truncated_rejected() {
        let buf = [0x80_u8]; // continuation bit set but no follow byte
        let mut c = Cursor::new(&buf);
        assert!(matches!(c.read_varint(), Err(ProtoError::UnexpectedEnd)));
    }

    #[test]
    fn cursor_read_fixed32_decodes_little_endian() {
        let buf = [0xCD, 0xAB, 0x00, 0x00];
        let mut c = Cursor::new(&buf);
        assert_eq!(c.read_fixed32().unwrap(), 0x0000_ABCD);
    }

    #[test]
    fn cursor_read_fixed64_decodes_little_endian() {
        let buf = [0x01, 0, 0, 0, 0, 0, 0, 0];
        let mut c = Cursor::new(&buf);
        assert_eq!(c.read_fixed64().unwrap(), 1);
    }

    // ───────────────────── decode_sei_message ──────────────────

    #[test]
    fn decode_empty_buffer_returns_default_message() {
        let msg = decode_sei_message(&[]).unwrap();
        assert_eq!(msg, SeiMessage::default());
        assert_eq!(msg.gear_state, Gear::Park);
        assert_eq!(msg.autopilot_state, AutopilotState::None);
        assert!(!msg.has_gps_fix());
    }

    #[test]
    fn decode_single_uint32_version_field() {
        let mut buf = Vec::new();
        encode_field_varint(1, 7, &mut buf);
        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.version, 7);
    }

    #[test]
    fn decode_full_message_roundtrip_via_handwritten_encoder() {
        let mut buf = Vec::new();
        encode_field_varint(1, 42, &mut buf); // version
        encode_field_varint(2, 1, &mut buf); // gear = Drive
        encode_field_varint(3, 1_234_567, &mut buf); // frame_seq_no
        encode_field_f32(4, 27.5, &mut buf); // 27.5 m/s ≈ 99 km/h
        encode_field_f32(5, 0.25, &mut buf);
        encode_field_f32(6, -0.1, &mut buf);
        encode_field_varint(7, 0, &mut buf); // blinker_left = false
        encode_field_varint(8, 1, &mut buf); // blinker_right = true
        encode_field_varint(9, 0, &mut buf); // brake = false
        encode_field_varint(10, 2, &mut buf); // autopilot = Autosteer
        encode_field_f64(11, 37.7749, &mut buf); // lat (SF)
        encode_field_f64(12, -122.4194, &mut buf); // lon (SF)
        encode_field_f64(13, 270.0, &mut buf); // heading
        encode_field_f64(14, 0.1, &mut buf);
        encode_field_f64(15, -0.05, &mut buf);
        encode_field_f64(16, 9.81, &mut buf);

        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.version, 42);
        assert_eq!(msg.gear_state, Gear::Drive);
        assert_eq!(msg.frame_seq_no, 1_234_567);
        assert_eq!(msg.vehicle_speed_mps, 27.5);
        assert_eq!(msg.accelerator_pedal_position, 0.25);
        assert_eq!(msg.steering_wheel_angle, -0.1);
        assert!(!msg.blinker_on_left);
        assert!(msg.blinker_on_right);
        assert!(!msg.brake_applied);
        assert_eq!(msg.autopilot_state, AutopilotState::Autosteer);
        assert_eq!(msg.latitude_deg, 37.7749);
        assert_eq!(msg.longitude_deg, -122.4194);
        assert_eq!(msg.heading_deg, 270.0);
        assert_eq!(msg.linear_acceleration_mps2_x, 0.1);
        assert_eq!(msg.linear_acceleration_mps2_y, -0.05);
        assert_eq!(msg.linear_acceleration_mps2_z, 9.81);
        assert!(msg.has_gps_fix());
    }

    #[test]
    fn decode_unknown_field_number_is_silently_skipped() {
        // Field 99, wire type varint = (99<<3)|0 = 792 → varint
        // 0x98 0x06.
        let mut buf = Vec::new();
        encode_field_varint(1, 7, &mut buf); // known: version = 7
        encode_field_varint(99, 12345, &mut buf); // unknown
        encode_field_varint(3, 1, &mut buf); // known: frame_seq_no
        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.version, 7);
        assert_eq!(msg.frame_seq_no, 1);
    }

    #[test]
    fn decode_skips_length_delimited_unknown_field() {
        let mut buf = Vec::new();
        encode_field_varint(1, 7, &mut buf);
        // Field 50, wire 2 (length-delimited)
        encode_tag(50, WIRE_LENGTH_DELIMITED, &mut buf);
        encode_varint(5, &mut buf);
        buf.extend_from_slice(b"hello");
        encode_field_varint(3, 99, &mut buf);
        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.version, 7);
        assert_eq!(msg.frame_seq_no, 99);
    }

    #[test]
    fn decode_skips_fixed32_and_fixed64_unknown_fields() {
        let mut buf = Vec::new();
        encode_field_fixed32(40, 0xDEAD_BEEF, &mut buf);
        encode_field_fixed64(41, 0x1234_5678_9ABC_DEF0, &mut buf);
        encode_field_varint(1, 5, &mut buf);
        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.version, 5);
    }

    #[test]
    fn decode_rejects_field_number_zero() {
        // tag = 0 → field=0, wire=0 → invalid
        let buf = [0x00_u8, 0x00];
        let err = decode_sei_message(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::InvalidFieldNumber));
    }

    #[test]
    fn decode_rejects_unknown_wire_type_on_unknown_field() {
        // Field 99, wire type 3 (deprecated group start)
        let mut buf = Vec::new();
        encode_tag(99, 3, &mut buf);
        let err = decode_sei_message(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::UnknownWireType { wire_type: 3 }));
    }

    #[test]
    fn decode_rejects_truncated_fixed32() {
        let mut buf = Vec::new();
        encode_tag(4, WIRE_FIXED32, &mut buf);
        buf.extend_from_slice(&[0x12, 0x34]); // only 2 bytes
        let err = decode_sei_message(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::UnexpectedEnd));
    }

    #[test]
    fn decode_rejects_truncated_fixed64() {
        let mut buf = Vec::new();
        encode_tag(11, WIRE_FIXED64, &mut buf);
        buf.extend_from_slice(&[0x12, 0x34, 0x56]); // only 3 bytes
        let err = decode_sei_message(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::UnexpectedEnd));
    }

    #[test]
    fn decode_rejects_length_delimited_overrun() {
        let mut buf = Vec::new();
        encode_tag(50, WIRE_LENGTH_DELIMITED, &mut buf);
        encode_varint(100, &mut buf); // claims 100 bytes
        buf.extend_from_slice(b"hi"); // only 2 bytes follow
        let err = decode_sei_message(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::UnexpectedEnd));
    }

    #[test]
    fn decode_wrong_wire_type_on_known_field_is_silently_skipped() {
        // Field 1 (version) normally varint; encode it as
        // fixed32 instead. Decoder treats it as "unknown" and
        // skips — proto3 forward-compat.
        let mut buf = Vec::new();
        encode_field_fixed32(1, 0xDEAD_BEEF, &mut buf);
        encode_field_varint(1, 5, &mut buf); // then a correctly-typed version
        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.version, 5);
    }

    #[test]
    fn has_gps_fix_returns_true_for_nonzero_latitude() {
        let msg = SeiMessage {
            latitude_deg: 47.0,
            ..Default::default()
        };
        assert!(msg.has_gps_fix());
    }

    #[test]
    fn has_gps_fix_returns_true_for_nonzero_longitude() {
        let msg = SeiMessage {
            longitude_deg: -122.0,
            ..Default::default()
        };
        assert!(msg.has_gps_fix());
    }

    #[test]
    fn has_gps_fix_returns_false_when_both_zero() {
        let msg = SeiMessage::default();
        assert!(!msg.has_gps_fix());
    }

    #[test]
    fn decode_handles_high_field_number_with_multi_byte_tag() {
        // Field 16 → tag = (16<<3)|1 = 129 → varint [0x81, 0x01]
        let mut buf = Vec::new();
        encode_field_f64(16, 1.5, &mut buf);
        assert_eq!(buf[0], 0x81);
        assert_eq!(buf[1], 0x01);
        let msg = decode_sei_message(&buf).unwrap();
        assert_eq!(msg.linear_acceleration_mps2_z, 1.5);
    }
}
