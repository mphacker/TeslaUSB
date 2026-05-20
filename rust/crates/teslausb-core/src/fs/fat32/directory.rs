//! FAT32 directory entry synthesizer (SFN + LFN).
//!
//! Phase 2.5 of the B-1 rewrite. A FAT32 directory is an array of
//! **32-byte entries**. Two kinds of entry coexist:
//!
//! * **Short File Name (SFN, "8.3")** — one 32-byte entry per
//!   file/directory carrying the file's metadata: short name,
//!   attributes, timestamps, first cluster, and size. fatgen103 §6.
//! * **Long File Name (LFN)** — zero or more 32-byte entries
//!   placed immediately **before** the SFN that describe the file's
//!   Unicode name. Each LFN entry carries 13 UCS-2 (UTF-16 BMP)
//!   characters; the maximum filename of 255 chars therefore
//!   occupies up to 20 LFN entries + 1 SFN entry = 21 dir entries
//!   (672 bytes). fatgen103 §7.
//!
//! This module synthesizes the **bytes** of those entries. It does
//! not lay out a directory cluster (that's the Phase 2.6 read
//! dispatcher's job) and it does not allocate clusters (Phase 3).
//!
//! ## Specification anchors
//!
//! * Microsoft FAT Specification (fatgen103.pdf) §6 — SFN entry
//!   layout, attribute byte definitions, allowed short-name
//!   character set, time/date bit packing.
//! * fatgen103 §7 — LFN entry layout, LFN ordinal byte, LFN UCS-2
//!   payload split into Name1/Name2/Name3 fields with `0x0000`
//!   terminator and `0xFFFF` guard padding, and the **one-byte
//!   short-name checksum** algorithm.
//!
//! ## SFN entry layout (32 bytes)
//!
//! ```text
//! Offset Size Field             Source
//! 0x00  11   Name              fixed 8.3 ASCII, space-padded
//! 0x0B   1   Attr              FileAttributes bitflags
//! 0x0C   1   NTRes             fixed: 0
//! 0x0D   1   CrtTimeTenth      created sub-second (0..199); B-1: 0
//! 0x0E   2   CrtTime           created time bits
//! 0x10   2   CrtDate           created date bits
//! 0x12   2   LstAccDate        last-access date bits
//! 0x14   2   FstClusHI         high 16 bits of first cluster (FAT32)
//! 0x16   2   WrtTime           last-write time bits
//! 0x18   2   WrtDate           last-write date bits
//! 0x1A   2   FstClusLO         low 16 bits of first cluster
//! 0x1C   4   FileSize          file size in bytes (0 for directories)
//! ```
//!
//! ## LFN entry layout (32 bytes)
//!
//! ```text
//! Offset Size Field             Value
//! 0x00   1   Ord               ordinal | LAST_LONG_ENTRY (0x40) for last
//! 0x01  10   Name1             chars 1..=5  (5 UCS-2 LE pairs)
//! 0x0B   1   Attr              fixed: ATTR_LONG_NAME (0x0F)
//! 0x0C   1   Type              fixed: 0
//! 0x0D   1   Chksum            SFN checksum (fatgen103 §7 algorithm)
//! 0x0E  12   Name2             chars 6..=11 (6 UCS-2 LE pairs)
//! 0x1A   2   FstClusLO         fixed: 0 (must be zero for LFN)
//! 0x1C   4   Name3             chars 12..=13 (2 UCS-2 LE pairs)
//! ```
//!
//! Name chars beyond the actual filename length are: one `0x0000`
//! terminator (if the name length is not a multiple of 13), then
//! `0xFFFF` guard padding to fill out the entry. The 0xFFFF guard
//! is a fatgen103 §7 normative requirement, not a "may"; the
//! Linux FAT driver rejects LFN entries with arbitrary padding.
//!
//! ## On-disk LFN ordering
//!
//! LFN entries appear on disk in **reverse order**: the entry
//! holding the *last* 13 chars of the filename appears first,
//! followed by descending ordinals, followed by the SFN entry
//! immediately after the ordinal-1 LFN. [`synthesize_lfn_sequence`]
//! returns entries already in on-disk order; the caller appends
//! the SFN bytes directly after.
//!
//! ## What this module does NOT do
//!
//! * It does not allocate first-cluster numbers (Phase 3.4).
//! * It does not derive an SFN alias from a long name (the SFN
//!   passed to [`synthesize_lfn_sequence`] / [`synthesize_sfn_entry`]
//!   must be supplied by the caller). Tesla's on-disk filenames
//!   (e.g. `2024-01-15_12-34-56-front.mp4`) all exceed 8.3, so the
//!   caller will use a simple `~N`-suffixed alias generator
//!   (Phase 2.6 / Phase 3 territory).
//! * It does not lay out the directory cluster — the dispatcher
//!   in Phase 2.6 will concatenate entries and pad the trailing
//!   tail with `0x00` zero entries to mark end-of-directory.

use core::fmt;

use crate::fs::geometry::SECTOR_SIZE_BYTES;

/// Byte width of one FAT32 directory entry (fatgen103 §6).
pub const DIR_ENTRY_SIZE_BYTES: usize = 32;

const _: () = {
    assert!(
        SECTOR_SIZE_BYTES as usize % DIR_ENTRY_SIZE_BYTES == 0,
        "sector size must hold a whole number of directory entries"
    );
};

// ── Attribute bits (fatgen103 §6.1) ───────────────────────────────────

/// `ATTR_READ_ONLY` — file is read-only.
pub const ATTR_READ_ONLY: u8 = 0x01;
/// `ATTR_HIDDEN` — file is hidden from default directory listings.
pub const ATTR_HIDDEN: u8 = 0x02;
/// `ATTR_SYSTEM` — system file.
pub const ATTR_SYSTEM: u8 = 0x04;
/// `ATTR_VOLUME_ID` — entry is a volume label (lives only in root dir).
pub const ATTR_VOLUME_ID: u8 = 0x08;
/// `ATTR_DIRECTORY` — entry describes a subdirectory.
pub const ATTR_DIRECTORY: u8 = 0x10;
/// `ATTR_ARCHIVE` — set by writes; cleared by backup tools.
pub const ATTR_ARCHIVE: u8 = 0x20;
/// `ATTR_LONG_NAME` — sentinel for an LFN entry's attribute byte
/// (`ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID =
/// 0x0F`). fatgen103 §7 normative: an LFN entry MUST set exactly
/// these four bits.
pub const ATTR_LONG_NAME: u8 = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID;

const _: () = {
    assert!(ATTR_LONG_NAME == 0x0F, "ATTR_LONG_NAME must equal 0x0F");
};

// ── Special first-byte sentinels (fatgen103 §6) ────────────────────────

/// First-byte sentinel meaning "no more entries follow in this
/// directory" (fatgen103 §6.1).
pub const DIR_ENTRY_END_OF_DIRECTORY: u8 = 0x00;

/// First-byte sentinel meaning "this entry is deleted but more
/// may follow" (fatgen103 §6.1).
pub const DIR_ENTRY_DELETED: u8 = 0xE5;

/// Replacement byte for a real `0xE5` first character (fatgen103
/// §6.1). The on-disk byte `0x05` is translated back to `0xE5`
/// by every conforming FAT driver. B-1 synth never emits names
/// starting with `0xE5`, but the constant is exported for
/// downstream parsers (Phase 3.1).
pub const DIR_ENTRY_ESCAPED_E5: u8 = 0x05;

// ── LFN ordinal-byte bits (fatgen103 §7) ──────────────────────────────

/// Bit set in the ordinal byte of the **last** LFN entry of a
/// sequence (the entry that appears first on disk and holds the
/// highest-numbered chars of the long name).
pub const LAST_LONG_ENTRY: u8 = 0x40;

/// Number of UCS-2 chars carried by each LFN entry (5 + 6 + 2).
pub const LFN_CHARS_PER_ENTRY: usize = 13;

/// Maximum number of chars in a single long filename (fatgen103 §7).
pub const LFN_MAX_CHARS: usize = 255;

/// Maximum number of LFN entries one filename can occupy
/// (= `ceil(LFN_MAX_CHARS / LFN_CHARS_PER_ENTRY)` = 20).
pub const LFN_MAX_ENTRIES: usize = 20;

const _: () = {
    assert!(
        LFN_MAX_ENTRIES * LFN_CHARS_PER_ENTRY >= LFN_MAX_CHARS,
        "LFN_MAX_ENTRIES must cover LFN_MAX_CHARS"
    );
};

// ── Short-name field widths (fatgen103 §6.2) ──────────────────────────

/// Width of an SFN base name in bytes (the "8" of "8.3").
pub const SHORT_NAME_BASE_LEN: usize = 8;

/// Width of an SFN extension in bytes (the "3" of "8.3").
pub const SHORT_NAME_EXT_LEN: usize = 3;

/// Total width of the SFN field (`Name + Ext`), space-padded
/// (fatgen103 §6.2).
pub const SHORT_NAME_LEN: usize = SHORT_NAME_BASE_LEN + SHORT_NAME_EXT_LEN;

// ── FAT date / time bit packing (fatgen103 §6.4) ──────────────────────

/// Minimum representable year in a FAT timestamp (`year - 1980`,
/// stored in the high 7 bits of the date field).
pub const FAT_DATE_MIN_YEAR: u16 = 1980;

/// Maximum representable year in a FAT timestamp (`1980 + 127`).
pub const FAT_DATE_MAX_YEAR: u16 = 1980 + 0x7F;

// ── Errors ────────────────────────────────────────────────────────────

/// Errors returned by [`ShortName::from_bytes`] and
/// [`ShortName::from_padded_str`].
#[derive(Debug, PartialEq, Eq)]
pub enum ShortNameError {
    /// The caller-supplied byte slice exceeds the 11-byte SFN
    /// field width (= [`SHORT_NAME_LEN`]).
    TooLong {
        /// Actual length of the caller's slice in bytes.
        actual: usize,
        /// Maximum allowed: [`SHORT_NAME_LEN`].
        maximum: usize,
    },
    /// The caller-supplied SFN bytes contain a byte not allowed
    /// in a FAT short-name per fatgen103 §6.1.
    ///
    /// Disallowed bytes include lowercase ASCII (`a..=z`), all
    /// control characters (`< 0x20`), the explicit forbidden
    /// punctuation set (`"`, `*`, `+`, `,`, `.`, `/`, `:`, `;`,
    /// `<`, `=`, `>`, `?`, `[`, `\`, `]`, `|`), and `0x7F` (DEL).
    InvalidByte {
        /// Zero-based offset within the SFN field.
        offset: usize,
        /// The offending byte.
        byte: u8,
    },
    /// First-byte position holds `0x20` (space) which is reserved
    /// to mean "no name" (the entry would look deleted-or-empty
    /// to some legacy parsers). fatgen103 §6.1.
    LeadingSpace,
    /// First-byte position holds `0xE5` which would be interpreted
    /// as the "deleted entry" sentinel. fatgen103 §6.1 mandates
    /// escaping such names to `0x05` at write time; B-1 synth
    /// refuses to construct one rather than silently escaping
    /// (the caller should pick a different short-name alias).
    LeadingDeletedSentinel,
}

impl fmt::Display for ShortNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLong { actual, maximum } => write!(
                f,
                "short name is {actual} bytes; SFN field is {maximum} bytes wide"
            ),
            Self::InvalidByte { offset, byte } => write!(
                f,
                "short name byte 0x{byte:02X} at offset {offset} is not allowed in a FAT 8.3 name"
            ),
            Self::LeadingSpace => write!(
                f,
                "short name cannot start with a space (offset 0 = 0x20 is reserved)"
            ),
            Self::LeadingDeletedSentinel => write!(
                f,
                "short name cannot start with 0xE5 (reserved as the deleted-entry marker)"
            ),
        }
    }
}

impl std::error::Error for ShortNameError {}

/// Errors returned by [`synthesize_lfn_sequence`].
#[derive(Debug, PartialEq, Eq)]
pub enum LfnError {
    /// The long name is empty.
    NameEmpty,
    /// The long name exceeds [`LFN_MAX_CHARS`] **UTF-16 code units**.
    ///
    /// "Char count" here is the number of `u16` units after
    /// UTF-16 encoding (surrogate pairs count as two). A BMP-only
    /// 255-char name fits; a name with one supplementary-plane
    /// emoji at position 255 does not (its surrogate pair pushes
    /// the unit count to 256).
    NameTooLong {
        /// Actual UTF-16 unit count.
        units: usize,
        /// Maximum allowed: [`LFN_MAX_CHARS`].
        maximum: usize,
    },
    /// The long name contains a UTF-16 code unit not allowed in a
    /// FAT long name per fatgen103 §7.
    ///
    /// Disallowed code units: all `< 0x0020` (control), `0x007F`
    /// (DEL), and the explicit forbidden punctuation set
    /// (`"`, `*`, `/`, `:`, `<`, `>`, `?`, `\`, `|`). The SFN's
    /// additional forbidden chars (`+`, `,`, `;`, `=`, `[`, `]`)
    /// are **allowed** in LFN per the spec.
    NameHasInvalidUnit {
        /// Zero-based UTF-16 unit offset within the long name.
        offset: usize,
        /// The offending UTF-16 unit value.
        unit: u16,
    },
}

impl fmt::Display for LfnError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NameEmpty => write!(f, "long name is empty"),
            Self::NameTooLong { units, maximum } => write!(
                f,
                "long name is {units} UTF-16 code units; FAT LFN allows at most {maximum}"
            ),
            Self::NameHasInvalidUnit { offset, unit } => write!(
                f,
                "long name UTF-16 unit 0x{unit:04X} at offset {offset} is not allowed in a FAT long name"
            ),
        }
    }
}

impl std::error::Error for LfnError {}

/// Errors returned by [`FatDate::new`].
#[derive(Debug, PartialEq, Eq)]
pub enum FatDateError {
    /// Year is below [`FAT_DATE_MIN_YEAR`] (1980) or above
    /// [`FAT_DATE_MAX_YEAR`] (2107).
    YearOutOfRange {
        /// The supplied year.
        year: u16,
        /// Inclusive minimum.
        min: u16,
        /// Inclusive maximum.
        max: u16,
    },
    /// Month is not in `1..=12`.
    MonthOutOfRange {
        /// The supplied month.
        month: u8,
    },
    /// Day is not in `1..=31` (no per-month validation — the FAT
    /// driver does not enforce calendar-correct days either).
    DayOutOfRange {
        /// The supplied day.
        day: u8,
    },
}

impl fmt::Display for FatDateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::YearOutOfRange { year, min, max } => {
                write!(f, "year {year} is outside FAT date range {min}..={max}")
            }
            Self::MonthOutOfRange { month } => {
                write!(f, "month {month} is outside FAT date range 1..=12")
            }
            Self::DayOutOfRange { day } => {
                write!(f, "day {day} is outside FAT date range 1..=31")
            }
        }
    }
}

impl std::error::Error for FatDateError {}

/// Errors returned by [`FatTime::new`].
#[derive(Debug, PartialEq, Eq)]
pub enum FatTimeError {
    /// Hour is not in `0..=23`.
    HourOutOfRange {
        /// The supplied hour.
        hour: u8,
    },
    /// Minute is not in `0..=59`.
    MinuteOutOfRange {
        /// The supplied minute.
        minute: u8,
    },
    /// Second is not in `0..=59`. Note FAT time has 2-second
    /// granularity; odd seconds are rounded down on encode.
    SecondOutOfRange {
        /// The supplied second.
        second: u8,
    },
}

impl fmt::Display for FatTimeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::HourOutOfRange { hour } => {
                write!(f, "hour {hour} is outside FAT time range 0..=23")
            }
            Self::MinuteOutOfRange { minute } => {
                write!(f, "minute {minute} is outside FAT time range 0..=59")
            }
            Self::SecondOutOfRange { second } => {
                write!(f, "second {second} is outside FAT time range 0..=59")
            }
        }
    }
}

impl std::error::Error for FatTimeError {}

// ── ShortName ─────────────────────────────────────────────────────────

/// A validated FAT 8.3 short name (11 bytes, space-padded).
///
/// Construction enforces fatgen103 §6.1 character rules; once
/// constructed the bytes are guaranteed to be acceptable to every
/// conforming FAT driver.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ShortName([u8; SHORT_NAME_LEN]);

impl ShortName {
    /// Build a `ShortName` from a pre-padded 11-byte field.
    ///
    /// The caller is responsible for layout (`Name` in bytes 0..8,
    /// `Ext` in bytes 8..11, both space-padded). Use
    /// [`Self::from_padded_str`] for a friendlier constructor that
    /// accepts "FOO.TXT"-style strings.
    ///
    /// # Errors
    ///
    /// * [`ShortNameError::InvalidByte`] for any disallowed byte.
    /// * [`ShortNameError::LeadingSpace`] if `bytes[0] == 0x20`.
    /// * [`ShortNameError::LeadingDeletedSentinel`] if `bytes[0] == 0xE5`.
    pub fn from_bytes(bytes: &[u8; SHORT_NAME_LEN]) -> Result<Self, ShortNameError> {
        if let Some(&first) = bytes.first() {
            if first == 0x20 {
                return Err(ShortNameError::LeadingSpace);
            }
            if first == DIR_ENTRY_DELETED {
                return Err(ShortNameError::LeadingDeletedSentinel);
            }
        }
        for (offset, &b) in bytes.iter().enumerate() {
            if !is_valid_short_name_byte(b) {
                return Err(ShortNameError::InvalidByte { offset, byte: b });
            }
        }
        Ok(Self(*bytes))
    }

    /// Build a `ShortName` from a `"NAME.EXT"` or `"NAME"` string.
    ///
    /// The base portion (before the `.`) is right-padded with
    /// spaces to 8 bytes; the extension is right-padded to 3
    /// bytes. The dot itself is not stored. Trailing spaces in
    /// either component are preserved as-is — callers usually
    /// don't include them.
    ///
    /// # Errors
    ///
    /// * [`ShortNameError::TooLong`] if the base exceeds
    ///   [`SHORT_NAME_BASE_LEN`] (8) or the extension exceeds
    ///   [`SHORT_NAME_EXT_LEN`] (3). The `actual` field reports
    ///   the offending component's length; the `maximum` field
    ///   reports its allowed width.
    /// * Variants from [`Self::from_bytes`] for byte-level
    ///   validation of the padded result.
    pub fn from_padded_str(name: &str) -> Result<Self, ShortNameError> {
        let bytes = name.as_bytes();
        let (base, ext) = match bytes.iter().position(|&b| b == b'.') {
            Some(dot_idx) => {
                let (b, rest) = bytes.split_at(dot_idx);
                let ext_bytes = rest.get(1..).unwrap_or(&[]);
                (b, ext_bytes)
            }
            None => (bytes, &[][..]),
        };
        if base.len() > SHORT_NAME_BASE_LEN {
            return Err(ShortNameError::TooLong {
                actual: base.len(),
                maximum: SHORT_NAME_BASE_LEN,
            });
        }
        if ext.len() > SHORT_NAME_EXT_LEN {
            return Err(ShortNameError::TooLong {
                actual: ext.len(),
                maximum: SHORT_NAME_EXT_LEN,
            });
        }
        let mut padded = [b' '; SHORT_NAME_LEN];
        copy_into(&mut padded, 0, base);
        copy_into(&mut padded, SHORT_NAME_BASE_LEN, ext);
        Self::from_bytes(&padded)
    }

    /// Borrow the 11 raw bytes of the SFN field.
    #[must_use]
    pub fn as_bytes(&self) -> &[u8; SHORT_NAME_LEN] {
        &self.0
    }

    /// Compute the one-byte LFN checksum of this short name per
    /// fatgen103 §7.
    ///
    /// Reference algorithm:
    ///
    /// ```text
    /// Sum = 0
    /// for each of the 11 SFN bytes:
    ///     Sum = ((Sum & 1) ? 0x80 : 0) + (Sum >> 1) + byte
    /// ```
    ///
    /// All arithmetic wraps mod 256 (single-byte unsigned).
    #[must_use]
    pub fn checksum(&self) -> u8 {
        let mut sum: u8 = 0;
        for &b in &self.0 {
            let rotated: u8 = if sum & 1 != 0 { 0x80 } else { 0 };
            sum = rotated.wrapping_add(sum >> 1).wrapping_add(b);
        }
        sum
    }
}

/// Whether `byte` is allowed in a FAT 8.3 short-name field
/// (fatgen103 §6.1).
fn is_valid_short_name_byte(byte: u8) -> bool {
    // Control characters and DEL — never allowed (space itself is
    // allowed as padding; the leading-space check is enforced
    // separately in ShortName::from_bytes).
    if byte < 0x20 || byte == 0x7F {
        return false;
    }
    // Lowercase ASCII — fatgen103 §6.1 forbids; kernel will
    // accept-and-uppercase but B-1 produces canonical bytes.
    if byte.is_ascii_lowercase() {
        return false;
    }
    matches!(byte,
        0x20  // space (padding)
        | b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'(' | b')'
        | b'-' | b'@' | b'^' | b'_' | b'`' | b'{' | b'}' | b'~'
        | b'0'..=b'9'
        | b'A'..=b'Z'
        | 0x80..=0xFF // OEM / codepage-dependent high range
    )
}

fn copy_into(dest: &mut [u8; SHORT_NAME_LEN], offset: usize, src: &[u8]) {
    for (i, &b) in src.iter().enumerate() {
        if let Some(slot) = dest.get_mut(offset + i) {
            *slot = b;
        }
    }
}

// ── FatDate / FatTime / FatDateTime ───────────────────────────────────

/// A validated calendar date packable into a 16-bit FAT date field
/// (fatgen103 §6.4).
///
/// The on-disk encoding is `(year - 1980) << 9 | month << 5 | day`.
/// Years are limited to 1980..=2107 (7 bits).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FatDate {
    year: u16,
    month: u8,
    day: u8,
}

impl FatDate {
    /// The fixed FAT "epoch" date 1980-01-01 (returned by
    /// [`Self::epoch`]).
    pub const EPOCH_BITS: u16 = (1u16 << 5) | 1;

    /// Construct a `FatDate` for the given calendar values.
    ///
    /// # Errors
    ///
    /// * [`FatDateError::YearOutOfRange`] if `year` is outside
    ///   [`FAT_DATE_MIN_YEAR`]..=[`FAT_DATE_MAX_YEAR`].
    /// * [`FatDateError::MonthOutOfRange`] if `month` is not in
    ///   `1..=12`.
    /// * [`FatDateError::DayOutOfRange`] if `day` is not in
    ///   `1..=31`.
    pub fn new(year: u16, month: u8, day: u8) -> Result<Self, FatDateError> {
        if !(FAT_DATE_MIN_YEAR..=FAT_DATE_MAX_YEAR).contains(&year) {
            return Err(FatDateError::YearOutOfRange {
                year,
                min: FAT_DATE_MIN_YEAR,
                max: FAT_DATE_MAX_YEAR,
            });
        }
        if !(1..=12).contains(&month) {
            return Err(FatDateError::MonthOutOfRange { month });
        }
        if !(1..=31).contains(&day) {
            return Err(FatDateError::DayOutOfRange { day });
        }
        Ok(Self { year, month, day })
    }

    /// The FAT "epoch" date 1980-01-01.
    #[must_use]
    pub fn epoch() -> Self {
        Self {
            year: FAT_DATE_MIN_YEAR,
            month: 1,
            day: 1,
        }
    }

    /// Pack into the 16-bit on-disk encoding.
    #[must_use]
    pub fn to_bits(self) -> u16 {
        let yr = (self.year - FAT_DATE_MIN_YEAR) & 0x7F;
        (yr << 9) | (u16::from(self.month) << 5) | u16::from(self.day)
    }
}

/// A validated wall-clock time packable into a 16-bit FAT time
/// field (fatgen103 §6.4).
///
/// The on-disk encoding is `hour << 11 | minute << 5 | (second / 2)`.
/// Granularity is 2 seconds; odd seconds are rounded down on
/// encode. The companion `CrtTimeTenth` byte at SFN offset 0x0D
/// adds 10 ms resolution to creation timestamps; B-1 emits 0 for
/// that byte (see [`synthesize_sfn_entry`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FatTime {
    hour: u8,
    minute: u8,
    second: u8,
}

impl FatTime {
    /// Construct a `FatTime` for the given wall-clock values.
    ///
    /// # Errors
    ///
    /// See [`FatTimeError`].
    pub fn new(hour: u8, minute: u8, second: u8) -> Result<Self, FatTimeError> {
        if hour > 23 {
            return Err(FatTimeError::HourOutOfRange { hour });
        }
        if minute > 59 {
            return Err(FatTimeError::MinuteOutOfRange { minute });
        }
        if second > 59 {
            return Err(FatTimeError::SecondOutOfRange { second });
        }
        Ok(Self {
            hour,
            minute,
            second,
        })
    }

    /// The "midnight" time 00:00:00.
    #[must_use]
    pub fn midnight() -> Self {
        Self {
            hour: 0,
            minute: 0,
            second: 0,
        }
    }

    /// Pack into the 16-bit on-disk encoding.
    #[must_use]
    pub fn to_bits(self) -> u16 {
        (u16::from(self.hour) << 11) | (u16::from(self.minute) << 5) | u16::from(self.second / 2)
    }
}

/// A combined date + time, the natural input to [`Timestamps`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FatDateTime {
    /// The date component.
    pub date: FatDate,
    /// The time component.
    pub time: FatTime,
}

impl FatDateTime {
    /// Construct from a date + time pair.
    #[must_use]
    pub fn new(date: FatDate, time: FatTime) -> Self {
        Self { date, time }
    }

    /// The FAT epoch 1980-01-01 00:00:00.
    #[must_use]
    pub fn epoch() -> Self {
        Self {
            date: FatDate::epoch(),
            time: FatTime::midnight(),
        }
    }
}

/// The four timestamps every SFN entry carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timestamps {
    /// Creation date + time (the SFN also carries a 10 ms-tenths
    /// byte; B-1 emits 0 for it).
    pub created: FatDateTime,
    /// Last-modified date + time.
    pub modified: FatDateTime,
    /// Last-access date (no time field exists for access on FAT).
    pub accessed: FatDate,
}

impl Timestamps {
    /// All four timestamps set to the FAT epoch (1980-01-01 00:00:00).
    ///
    /// Useful for deterministic tests; production callers should
    /// pass real wall-clock values from a clock source.
    #[must_use]
    pub fn epoch() -> Self {
        Self {
            created: FatDateTime::epoch(),
            modified: FatDateTime::epoch(),
            accessed: FatDate::epoch(),
        }
    }
}

// ── FileAttributes ────────────────────────────────────────────────────

/// Type-safe wrapper around the 8-bit `Attr` field at SFN offset
/// `0x0B`.
///
/// Use the named constructors ([`Self::archive`],
/// [`Self::read_only_archive`], [`Self::directory`],
/// [`Self::volume_label`]) instead of constructing from a raw
/// `u8`; that way it's a compile error to accidentally set the
/// LFN bit pattern (`0x0F`) on a regular file entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileAttributes(u8);

impl FileAttributes {
    /// File with the `ARCHIVE` bit set — what `touch foo.txt`
    /// produces. The default for new files.
    #[must_use]
    pub const fn archive() -> Self {
        Self(ATTR_ARCHIVE)
    }

    /// File marked read-only AND archive.
    #[must_use]
    pub const fn read_only_archive() -> Self {
        Self(ATTR_READ_ONLY | ATTR_ARCHIVE)
    }

    /// A subdirectory entry (the `.` and `..` entries plus every
    /// `mkdir`-produced entry).
    #[must_use]
    pub const fn directory() -> Self {
        Self(ATTR_DIRECTORY)
    }

    /// The volume label entry that lives in the root directory of
    /// every FAT volume (fatgen103 §6.1).
    #[must_use]
    pub const fn volume_label() -> Self {
        Self(ATTR_VOLUME_ID)
    }

    /// The raw byte that will be written to SFN offset `0x0B`.
    #[must_use]
    pub const fn raw(self) -> u8 {
        self.0
    }
}

// ── SFN entry synthesizer ─────────────────────────────────────────────

/// Synthesize a single 32-byte SFN directory entry.
///
/// The caller supplies the short name (already validated), the
/// attribute byte (typed via [`FileAttributes`]), the first
/// cluster number, the file size in bytes, and the four
/// timestamps. The output is the entry's on-disk bytes ready to
/// be concatenated into a directory cluster.
///
/// For directory entries `file_size` must be `0` per fatgen103
/// §6.2; this function does not enforce that — the caller is
/// expected to construct `FileAttributes::directory()` with
/// `file_size = 0`.
///
/// `first_cluster = 0` is legal and means "empty file" — a file
/// with zero bytes has no allocated clusters. The dispatcher and
/// the write-side will produce this for empty Tesla `event.json`
/// placeholders.
#[must_use]
pub fn synthesize_sfn_entry(
    short: &ShortName,
    attrs: FileAttributes,
    first_cluster: u32,
    file_size: u32,
    timestamps: &Timestamps,
) -> [u8; DIR_ENTRY_SIZE_BYTES] {
    let mut buf = [0u8; DIR_ENTRY_SIZE_BYTES];
    write_bytes(&mut buf, 0x00, short.as_bytes());
    write_u8(&mut buf, 0x0B, attrs.raw());
    write_u8(&mut buf, 0x0C, 0); // NTRes
    write_u8(&mut buf, 0x0D, 0); // CrtTimeTenth — B-1 omits sub-second
    write_u16_le(&mut buf, 0x0E, timestamps.created.time.to_bits());
    write_u16_le(&mut buf, 0x10, timestamps.created.date.to_bits());
    write_u16_le(&mut buf, 0x12, timestamps.accessed.to_bits());
    let (clus_hi, clus_lo) = split_cluster(first_cluster);
    write_u16_le(&mut buf, 0x14, clus_hi);
    write_u16_le(&mut buf, 0x16, timestamps.modified.time.to_bits());
    write_u16_le(&mut buf, 0x18, timestamps.modified.date.to_bits());
    write_u16_le(&mut buf, 0x1A, clus_lo);
    write_u32_le(&mut buf, 0x1C, file_size);
    buf
}

fn split_cluster(cluster: u32) -> (u16, u16) {
    let hi = ((cluster >> 16) & 0xFFFF) as u16;
    let lo = (cluster & 0xFFFF) as u16;
    (hi, lo)
}

// ── Volume label entry synthesizer ────────────────────────────────────

/// Byte width of the FAT volume-label entry's name field.
///
/// Equal to [`DIR_ENTRY_SIZE_BYTES`]'s `Name` field width
/// (fatgen103 §6) and to the boot sector's `BS_VolLab` width
/// (`super::boot_sector::VOLUME_LABEL_LEN_BYTES`). A volume label
/// always occupies exactly 11 bytes, space-padded.
pub const VOLUME_LABEL_NAME_LEN: usize = 11;

const _: () = {
    assert!(
        VOLUME_LABEL_NAME_LEN == SHORT_NAME_LEN,
        "volume-label name field must match SFN name width",
    );
};

/// Synthesize the 32-byte root-directory volume label entry.
///
/// fatgen103 §6.1 states that the volume label appears twice on a
/// FAT volume: in the boot sector's `BS_VolLab` field AND as a
/// distinguished entry in the root directory carrying
/// [`ATTR_VOLUME_ID`] (`0x08`). The two MUST agree; `fsck.vfat`
/// reports `"Label in boot sector is X, but there is no volume
/// label in root directory."` when the root entry is missing.
///
/// `label_11` MUST be the same 11-byte padded label that
/// [`super::boot_sector::synthesize`] wrote at offset `0x47`;
/// callers obtain it from
/// [`super::boot_sector::pad_volume_label`] so a single validation
/// + padding step feeds both consumers.
///
/// Per spec, the volume label entry's `FstClusHI`, `FstClusLO`,
/// and `FileSize` MUST be zero — the entry is metadata-only and
/// does not own any cluster chain. This function enforces that
/// invariant unconditionally; only the timestamps come from the
/// caller.
///
/// `Attr` is fixed at [`ATTR_VOLUME_ID`] (`0x08`) — never
/// combined with [`ATTR_DIRECTORY`] or [`ATTR_ARCHIVE`].
#[must_use]
pub fn synthesize_volume_label_entry(
    label_11: &[u8; VOLUME_LABEL_NAME_LEN],
    timestamps: &Timestamps,
) -> [u8; DIR_ENTRY_SIZE_BYTES] {
    let mut buf = [0u8; DIR_ENTRY_SIZE_BYTES];
    write_bytes(&mut buf, 0x00, label_11);
    write_u8(&mut buf, 0x0B, ATTR_VOLUME_ID);
    write_u8(&mut buf, 0x0C, 0); // NTRes
    write_u8(&mut buf, 0x0D, 0); // CrtTimeTenth
    write_u16_le(&mut buf, 0x0E, timestamps.created.time.to_bits());
    write_u16_le(&mut buf, 0x10, timestamps.created.date.to_bits());
    write_u16_le(&mut buf, 0x12, timestamps.accessed.to_bits());
    // FstClusHI (0x14) and FstClusLO (0x1A) must remain zero per
    // fatgen103 §6.1 — leave the buffer's initial zero-fill
    // untouched at those offsets.
    write_u16_le(&mut buf, 0x16, timestamps.modified.time.to_bits());
    write_u16_le(&mut buf, 0x18, timestamps.modified.date.to_bits());
    // FileSize (0x1C) must remain zero per fatgen103 §6.1.
    buf
}

// ── LFN sequence synthesizer ──────────────────────────────────────────

/// Synthesize the LFN entries describing `long_name` for an SFN
/// whose checksum is `sfn_checksum`.
///
/// The returned vector contains the LFN entries **in on-disk
/// order**: the entry holding the *last* chunk of the filename
/// (and carrying the [`LAST_LONG_ENTRY`] bit in its ordinal) is
/// at index 0; descending ordinals follow; the SFN entry is
/// appended by the caller immediately after the last (ordinal-1)
/// LFN.
///
/// Each LFN entry carries 13 UTF-16 code units. The terminator
/// `0x0000` is written immediately after the last filename code
/// unit (only if the unit count is not a multiple of 13). All
/// subsequent slots in that final entry are filled with `0xFFFF`
/// per fatgen103 §7.
///
/// # Errors
///
/// * [`LfnError::NameEmpty`] if `long_name` is empty.
/// * [`LfnError::NameTooLong`] if its UTF-16 encoding exceeds
///   [`LFN_MAX_CHARS`] units.
/// * [`LfnError::NameHasInvalidUnit`] for any disallowed UTF-16
///   code unit.
pub fn synthesize_lfn_sequence(
    long_name: &str,
    sfn_checksum: u8,
) -> Result<Vec<[u8; DIR_ENTRY_SIZE_BYTES]>, LfnError> {
    if long_name.is_empty() {
        return Err(LfnError::NameEmpty);
    }
    let units: Vec<u16> = long_name.encode_utf16().collect();
    if units.len() > LFN_MAX_CHARS {
        return Err(LfnError::NameTooLong {
            units: units.len(),
            maximum: LFN_MAX_CHARS,
        });
    }
    for (offset, &u) in units.iter().enumerate() {
        if !is_valid_long_name_unit(u) {
            return Err(LfnError::NameHasInvalidUnit { offset, unit: u });
        }
    }
    let entry_count = units.len().div_ceil(LFN_CHARS_PER_ENTRY);
    let mut out = Vec::with_capacity(entry_count);
    for ord in 1..=entry_count {
        let start = (ord - 1) * LFN_CHARS_PER_ENTRY;
        let end = (ord * LFN_CHARS_PER_ENTRY).min(units.len());
        let chunk = units.get(start..end).unwrap_or(&[]);
        let is_last = ord == entry_count;
        out.push(build_lfn_entry(
            #[allow(clippy::cast_possible_truncation)]
            (ord as u8),
            is_last,
            chunk,
            sfn_checksum,
        ));
    }
    // On-disk order is highest ordinal first.
    out.reverse();
    Ok(out)
}

const _: () = {
    assert!(
        LFN_MAX_ENTRIES <= u8::MAX as usize,
        "LFN ordinal must fit in u8"
    );
};

fn build_lfn_entry(
    ordinal: u8,
    is_last: bool,
    chunk: &[u16],
    sfn_checksum: u8,
) -> [u8; DIR_ENTRY_SIZE_BYTES] {
    let mut buf = [0xFFu8; DIR_ENTRY_SIZE_BYTES];
    let ord_byte = if is_last {
        ordinal | LAST_LONG_ENTRY
    } else {
        ordinal
    };
    write_u8(&mut buf, 0x00, ord_byte);
    // Fixed scaffolding.
    write_u8(&mut buf, 0x0B, ATTR_LONG_NAME);
    write_u8(&mut buf, 0x0C, 0);
    write_u8(&mut buf, 0x0D, sfn_checksum);
    write_u16_le(&mut buf, 0x1A, 0); // FstClusLO MUST be 0 for LFN entries.

    // Layout the 13 UTF-16 units across Name1 (5) + Name2 (6) + Name3 (2).
    // Anything past the chunk length receives the 0x0000 terminator
    // (exactly once) followed by 0xFFFF guards (already pre-filled).
    let mut wrote_terminator = false;
    for slot in 0..LFN_CHARS_PER_ENTRY {
        let unit = if slot < chunk.len() {
            chunk.get(slot).copied().unwrap_or(0xFFFF)
        } else if !wrote_terminator {
            wrote_terminator = true;
            0x0000
        } else {
            0xFFFF
        };
        let offset = lfn_slot_offset(slot);
        write_u16_le(&mut buf, offset, unit);
    }
    buf
}

const fn lfn_slot_offset(slot: usize) -> usize {
    // Name1: slots 0..5  at 0x01..0x0B (5 UCS-2 = 10 bytes)
    // Name2: slots 5..11 at 0x0E..0x1A (6 UCS-2 = 12 bytes)
    // Name3: slots 11..13 at 0x1C..0x20 (2 UCS-2 = 4 bytes)
    if slot < 5 {
        0x01 + slot * 2
    } else if slot < 11 {
        0x0E + (slot - 5) * 2
    } else {
        0x1C + (slot - 11) * 2
    }
}

/// Whether `unit` is allowed in a FAT long filename
/// (fatgen103 §7).
fn is_valid_long_name_unit(unit: u16) -> bool {
    if unit < 0x0020 || unit == 0x007F {
        return false;
    }
    !matches!(
        unit,
        0x0022  // "
        | 0x002A // *
        | 0x002F // /
        | 0x003A // :
        | 0x003C // <
        | 0x003E // >
        | 0x003F // ?
        | 0x005C // \
        | 0x007C // |
    )
}

// ── Dot/dotdot entries (subdirectories) ───────────────────────────────

/// Synthesize the two mandatory `.` and `..` entries that every
/// subdirectory cluster begins with (fatgen103 §6.5.2).
///
/// `this_cluster` is the first cluster of the subdirectory
/// itself; `parent_cluster` is the first cluster of the parent
/// directory. Per fatgen103 §6.5.2 a subdirectory whose parent is
/// the root directory MUST write `parent_cluster = 0`, not the
/// root's actual cluster number (2). Callers handling that case
/// must pass `0` explicitly.
///
/// The returned array is `[<dot>, <dotdot>]` in on-disk order.
#[must_use]
pub fn synthesize_dot_entries(
    this_cluster: u32,
    parent_cluster: u32,
    timestamps: &Timestamps,
) -> [[u8; DIR_ENTRY_SIZE_BYTES]; 2] {
    let dot = synthesize_sfn_entry(
        &dot_short_name(),
        FileAttributes::directory(),
        this_cluster,
        0,
        timestamps,
    );
    let dotdot = synthesize_sfn_entry(
        &dotdot_short_name(),
        FileAttributes::directory(),
        parent_cluster,
        0,
        timestamps,
    );
    [dot, dotdot]
}

fn dot_short_name() -> ShortName {
    // ". " padded to 11 bytes. fatgen103 §6.5.2 normative.
    let mut bytes = [b' '; SHORT_NAME_LEN];
    if let Some(slot) = bytes.first_mut() {
        *slot = b'.';
    }
    // Construct directly — '.' is forbidden in regular SFNs by
    // is_valid_short_name_byte, but the dot entries are spec-
    // mandated literal names. Bypass validation here.
    ShortName(bytes)
}

fn dotdot_short_name() -> ShortName {
    let mut bytes = [b' '; SHORT_NAME_LEN];
    if let Some(slot) = bytes.first_mut() {
        *slot = b'.';
    }
    if let Some(slot) = bytes.get_mut(1) {
        *slot = b'.';
    }
    ShortName(bytes)
}

// ── Low-level write helpers ───────────────────────────────────────────

// Every call site in this module writes to a compile-time-constant
// offset within a fixed-size 32-byte buffer; the `indexing_slicing`
// lint is therefore safe to suppress on these helpers.

#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u8(buf: &mut [u8; DIR_ENTRY_SIZE_BYTES], offset: usize, value: u8) {
    buf[offset] = value;
}

#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u16_le(buf: &mut [u8; DIR_ENTRY_SIZE_BYTES], offset: usize, value: u16) {
    buf[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
}

#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u32_le(buf: &mut [u8; DIR_ENTRY_SIZE_BYTES], offset: usize, value: u32) {
    buf[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

#[inline]
#[allow(clippy::indexing_slicing)]
fn write_bytes(buf: &mut [u8; DIR_ENTRY_SIZE_BYTES], offset: usize, src: &[u8]) {
    buf[offset..offset + src.len()].copy_from_slice(src);
}

#[cfg(test)]
#[allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use super::*;

    fn epoch_ts() -> Timestamps {
        Timestamps::epoch()
    }

    fn sample_ts() -> Timestamps {
        Timestamps {
            created: FatDateTime::new(
                FatDate::new(2024, 1, 15).unwrap(),
                FatTime::new(12, 34, 56).unwrap(),
            ),
            modified: FatDateTime::new(
                FatDate::new(2024, 6, 30).unwrap(),
                FatTime::new(23, 59, 58).unwrap(),
            ),
            accessed: FatDate::new(2024, 12, 31).unwrap(),
        }
    }

    fn read_u16_le(entry: &[u8; 32], offset: usize) -> u16 {
        u16::from_le_bytes(entry[offset..offset + 2].try_into().unwrap())
    }

    fn read_u32_le(entry: &[u8; 32], offset: usize) -> u32 {
        u32::from_le_bytes(entry[offset..offset + 4].try_into().unwrap())
    }

    // ── Sanity: shape + sizing ────────────────────────────────────────

    #[test]
    fn dir_entry_is_32_bytes() {
        assert_eq!(DIR_ENTRY_SIZE_BYTES, 32);
        assert_eq!(SECTOR_SIZE_BYTES as usize % DIR_ENTRY_SIZE_BYTES, 0);
    }

    #[test]
    fn attr_long_name_equals_0x0f() {
        assert_eq!(ATTR_LONG_NAME, 0x0F);
        // It's the OR of READ_ONLY | HIDDEN | SYSTEM | VOLUME_ID.
        assert_eq!(
            ATTR_LONG_NAME,
            ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID
        );
    }

    #[test]
    fn lfn_max_chars_fits_in_max_entries() {
        // Locked at module level via `const _ : () = { assert!(...); };`,
        // confirmed at runtime via the exact constant values.
        assert_eq!(LFN_MAX_ENTRIES, 20);
        assert_eq!(LFN_CHARS_PER_ENTRY, 13);
        assert_eq!(LFN_MAX_CHARS, 255);
        // 20 * 13 = 260 >= 255.
        assert_eq!(LFN_MAX_ENTRIES * LFN_CHARS_PER_ENTRY, 260);
    }

    // ── ShortName: validation ─────────────────────────────────────────

    #[test]
    fn short_name_from_bytes_accepts_canonical_padded_name() {
        let bytes = *b"FOO     TXT";
        let s = ShortName::from_bytes(&bytes).expect("valid 8.3");
        assert_eq!(s.as_bytes(), &bytes);
    }

    #[test]
    fn short_name_rejects_lowercase() {
        let bytes = *b"foo     TXT";
        let err = ShortName::from_bytes(&bytes).expect_err("lowercase rejected");
        assert_eq!(
            err,
            ShortNameError::InvalidByte {
                offset: 0,
                byte: b'f'
            }
        );
    }

    #[test]
    fn short_name_rejects_dot_byte() {
        // The dot itself is the SFN separator; storing it as a
        // name byte would be ambiguous.
        let bytes = *b"FOO.    TXT";
        let err = ShortName::from_bytes(&bytes).expect_err("dot byte rejected");
        assert_eq!(
            err,
            ShortNameError::InvalidByte {
                offset: 3,
                byte: b'.'
            }
        );
    }

    #[test]
    fn short_name_rejects_control_chars() {
        let bytes: [u8; 11] = [
            0x01, b'F', b'O', b'O', b' ', b' ', b' ', b' ', b'T', b'X', b'T',
        ];
        let err = ShortName::from_bytes(&bytes).expect_err("control byte rejected");
        assert_eq!(
            err,
            ShortNameError::InvalidByte {
                offset: 0,
                byte: 0x01
            }
        );
    }

    #[test]
    fn short_name_rejects_leading_space() {
        let bytes = *b"        TXT";
        let err = ShortName::from_bytes(&bytes).expect_err("leading space rejected");
        assert_eq!(err, ShortNameError::LeadingSpace);
    }

    #[test]
    fn short_name_rejects_leading_deleted_sentinel() {
        let bytes: [u8; 11] = [
            0xE5, b'O', b'O', b' ', b' ', b' ', b' ', b' ', b'T', b'X', b'T',
        ];
        let err = ShortName::from_bytes(&bytes).expect_err("0xE5 rejected");
        assert_eq!(err, ShortNameError::LeadingDeletedSentinel);
    }

    #[test]
    fn short_name_accepts_high_byte_after_first() {
        // 0xE5 in any position other than the first is fine.
        let bytes: [u8; 11] = [
            b'F', 0xE5, b'O', b' ', b' ', b' ', b' ', b' ', b'T', b'X', b'T',
        ];
        let _ = ShortName::from_bytes(&bytes).expect("0xE5 at offset 1 ok");
    }

    #[test]
    fn short_name_from_padded_str_pads_base_and_ext() {
        let s = ShortName::from_padded_str("FOO.TXT").expect("valid");
        assert_eq!(s.as_bytes(), b"FOO     TXT");
    }

    #[test]
    fn short_name_from_padded_str_handles_no_extension() {
        let s = ShortName::from_padded_str("README").expect("valid");
        assert_eq!(s.as_bytes(), b"README     ");
    }

    #[test]
    fn short_name_from_padded_str_rejects_too_long_base() {
        let err = ShortName::from_padded_str("LONGFILENAME.TXT").expect_err("base too long");
        assert_eq!(
            err,
            ShortNameError::TooLong {
                actual: 12,
                maximum: SHORT_NAME_BASE_LEN
            }
        );
    }

    #[test]
    fn short_name_from_padded_str_rejects_too_long_ext() {
        let err = ShortName::from_padded_str("FOO.HTML").expect_err("ext too long");
        assert_eq!(
            err,
            ShortNameError::TooLong {
                actual: 4,
                maximum: SHORT_NAME_EXT_LEN
            }
        );
    }

    // ── ShortName: checksum (fatgen103 §7 reference algorithm) ────────

    #[test]
    fn checksum_matches_fatgen103_reference_for_known_name() {
        // Hand-traced through the reference algorithm for "FOO     TXT":
        //   sum = 0
        //   'F' = 0x46:
        //     sum&1 = 0 → rotated=0; sum>>1 = 0; new = 0+0+0x46 = 0x46
        //   'O' = 0x4F:
        //     sum&1 = 0 → rotated=0; sum>>1 = 0x23; new = 0+0x23+0x4F = 0x72
        //   'O' = 0x4F:
        //     sum&1 = 0 → rotated=0; sum>>1 = 0x39; new = 0+0x39+0x4F = 0x88
        //   ' ' = 0x20:  88&1=0, 88>>1=0x44, new=0+0x44+0x20=0x64
        //   ' ' = 0x20:  64&1=0, 64>>1=0x32, new=0+0x32+0x20=0x52
        //   ' ' = 0x20:  52&1=0, 52>>1=0x29, new=0+0x29+0x20=0x49
        //   ' ' = 0x20:  49&1=1, rotated=0x80, 49>>1=0x24, new=0x80+0x24+0x20=0xC4
        //   ' ' = 0x20:  C4&1=0, C4>>1=0x62, new=0+0x62+0x20=0x82
        //   'T' = 0x54:  82&1=0, 82>>1=0x41, new=0+0x41+0x54=0x95
        //   'X' = 0x58:  95&1=1, rotated=0x80, 95>>1=0x4A, new=0x80+0x4A+0x58=0x122 mod 256=0x22
        //   'T' = 0x54:  22&1=0, 22>>1=0x11, new=0+0x11+0x54=0x65
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        assert_eq!(s.checksum(), 0x65);
    }

    #[test]
    fn checksum_of_all_spaces_is_zero() {
        // Identity case: 11 zero-deltas through a zero accumulator
        // with an even number of right-shifts → 0. Hand-trace:
        // every byte is 0x20; after 11 iterations the sum is...
        // actually not zero. Compute and lock the result.
        let s = ShortName::from_bytes(&[b' '; 11]).expect_err("leading space rejected");
        assert_eq!(s, ShortNameError::LeadingSpace);
        // Instead, build a "X         " name and verify checksum
        // is deterministic.
        let s = ShortName::from_padded_str("X").unwrap();
        assert_eq!(s.as_bytes(), b"X          ");
        // Hand: 'X'=0x58, then 10× ' '=0x20.
        //   start 0; 'X': new = 0+0+0x58 = 0x58
        //   ' ': 58&1=0, 58>>1=0x2C, new = 0+0x2C+0x20 = 0x4C
        //   ' ': 4C&1=0, 4C>>1=0x26, new = 0+0x26+0x20 = 0x46
        //   ' ': 46&1=0, 46>>1=0x23, new = 0+0x23+0x20 = 0x43
        //   ' ': 43&1=1, rotated=0x80, 43>>1=0x21, new = 0x80+0x21+0x20=0xC1
        //   ' ': C1&1=1, rotated=0x80, C1>>1=0x60, new = 0x80+0x60+0x20=0x100 mod 256 = 0x00
        //   ' ': 00&1=0, 00>>1=0, new = 0+0+0x20=0x20
        //   ' ': 20&1=0, 20>>1=0x10, new = 0+0x10+0x20=0x30
        //   ' ': 30&1=0, 30>>1=0x18, new = 0+0x18+0x20=0x38
        //   ' ': 38&1=0, 38>>1=0x1C, new = 0+0x1C+0x20=0x3C
        //   ' ': 3C&1=0, 3C>>1=0x1E, new = 0+0x1E+0x20=0x3E
        assert_eq!(s.checksum(), 0x3E);
    }

    #[test]
    fn checksum_depends_on_all_eleven_bytes() {
        // Two different names must produce different checksums
        // (probabilistically — the 8-bit checksum space is small,
        // but these two are picked to differ).
        let a = ShortName::from_padded_str("AAA.AAA").unwrap();
        let b = ShortName::from_padded_str("BBB.BBB").unwrap();
        assert_ne!(a.checksum(), b.checksum());
    }

    // ── FatDate / FatTime / FatDateTime ──────────────────────────────

    #[test]
    fn fat_date_epoch_encodes_to_known_bits() {
        // 1980-01-01: (0 << 9) | (1 << 5) | 1 = 0x0021
        assert_eq!(FatDate::epoch().to_bits(), 0x0021);
        assert_eq!(FatDate::EPOCH_BITS, 0x0021);
    }

    #[test]
    fn fat_date_arbitrary_packing_matches_spec() {
        // 2024-03-15: (44 << 9) | (3 << 5) | 15
        let d = FatDate::new(2024, 3, 15).unwrap();
        let expected = ((2024 - 1980) << 9) | (3 << 5) | 15;
        assert_eq!(d.to_bits(), expected);
    }

    #[test]
    fn fat_date_rejects_year_below_minimum() {
        let err = FatDate::new(1979, 6, 15).expect_err("year < 1980");
        assert_eq!(
            err,
            FatDateError::YearOutOfRange {
                year: 1979,
                min: FAT_DATE_MIN_YEAR,
                max: FAT_DATE_MAX_YEAR
            }
        );
    }

    #[test]
    fn fat_date_rejects_year_above_maximum() {
        let err = FatDate::new(2108, 6, 15).expect_err("year > 2107");
        assert!(matches!(
            err,
            FatDateError::YearOutOfRange { year: 2108, .. }
        ));
    }

    #[test]
    fn fat_date_accepts_year_2107_boundary() {
        let _ = FatDate::new(2107, 12, 31).expect("2107 is the inclusive maximum");
    }

    #[test]
    fn fat_date_rejects_month_zero_and_thirteen() {
        assert!(matches!(
            FatDate::new(2024, 0, 15).unwrap_err(),
            FatDateError::MonthOutOfRange { month: 0 }
        ));
        assert!(matches!(
            FatDate::new(2024, 13, 15).unwrap_err(),
            FatDateError::MonthOutOfRange { month: 13 }
        ));
    }

    #[test]
    fn fat_date_rejects_day_zero_and_thirty_two() {
        assert!(matches!(
            FatDate::new(2024, 6, 0).unwrap_err(),
            FatDateError::DayOutOfRange { day: 0 }
        ));
        assert!(matches!(
            FatDate::new(2024, 6, 32).unwrap_err(),
            FatDateError::DayOutOfRange { day: 32 }
        ));
    }

    #[test]
    fn fat_time_midnight_packs_to_zero() {
        assert_eq!(FatTime::midnight().to_bits(), 0);
    }

    #[test]
    fn fat_time_arbitrary_packing_matches_spec() {
        // 14:23:46 → (14 << 11) | (23 << 5) | (46/2=23)
        let t = FatTime::new(14, 23, 46).unwrap();
        let expected = (14u16 << 11) | (23u16 << 5) | 23;
        assert_eq!(t.to_bits(), expected);
    }

    #[test]
    fn fat_time_odd_second_rounds_down() {
        // 12:30:47 → seconds/2 = 23 (not 24).
        let t = FatTime::new(12, 30, 47).unwrap();
        let secs = t.to_bits() & 0x1F;
        assert_eq!(secs, 23);
    }

    #[test]
    fn fat_time_rejects_hour_24() {
        assert!(matches!(
            FatTime::new(24, 0, 0).unwrap_err(),
            FatTimeError::HourOutOfRange { hour: 24 }
        ));
    }

    #[test]
    fn fat_time_rejects_minute_60() {
        assert!(matches!(
            FatTime::new(0, 60, 0).unwrap_err(),
            FatTimeError::MinuteOutOfRange { minute: 60 }
        ));
    }

    #[test]
    fn fat_time_rejects_second_60() {
        assert!(matches!(
            FatTime::new(0, 0, 60).unwrap_err(),
            FatTimeError::SecondOutOfRange { second: 60 }
        ));
    }

    // ── FileAttributes ───────────────────────────────────────────────

    #[test]
    fn file_attributes_named_constructors_match_spec() {
        assert_eq!(FileAttributes::archive().raw(), ATTR_ARCHIVE);
        assert_eq!(
            FileAttributes::read_only_archive().raw(),
            ATTR_READ_ONLY | ATTR_ARCHIVE
        );
        assert_eq!(FileAttributes::directory().raw(), ATTR_DIRECTORY);
        assert_eq!(FileAttributes::volume_label().raw(), ATTR_VOLUME_ID);
    }

    #[test]
    fn file_attributes_named_constructors_never_collide_with_long_name_pattern() {
        // Sanity: none of the named constructors accidentally
        // produce the LFN attribute byte (0x0F).
        for raw in [
            FileAttributes::archive().raw(),
            FileAttributes::read_only_archive().raw(),
            FileAttributes::directory().raw(),
            FileAttributes::volume_label().raw(),
        ] {
            assert_ne!(
                raw, ATTR_LONG_NAME,
                "{raw:#04X} collides with ATTR_LONG_NAME"
            );
        }
    }

    // ── SFN entry synthesizer ────────────────────────────────────────

    #[test]
    fn sfn_entry_name_at_offset_0() {
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let e = synthesize_sfn_entry(&s, FileAttributes::archive(), 5, 100, &epoch_ts());
        assert_eq!(&e[0x00..0x0B], b"FOO     TXT");
    }

    #[test]
    fn sfn_entry_attr_at_offset_0x0b() {
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let e = synthesize_sfn_entry(&s, FileAttributes::archive(), 5, 100, &epoch_ts());
        assert_eq!(e[0x0B], ATTR_ARCHIVE);
    }

    #[test]
    fn sfn_entry_ntres_and_crt_time_tenth_are_zero() {
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let e = synthesize_sfn_entry(&s, FileAttributes::archive(), 5, 100, &epoch_ts());
        assert_eq!(e[0x0C], 0, "NTRes must be 0");
        assert_eq!(e[0x0D], 0, "CrtTimeTenth must be 0 (B-1 omits sub-second)");
    }

    #[test]
    fn sfn_entry_first_cluster_split_high_low() {
        // A first cluster of 0xABCDEF12 splits as:
        //   FstClusHI = 0xABCD at offset 0x14
        //   FstClusLO = 0xEF12 at offset 0x1A
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let e = synthesize_sfn_entry(&s, FileAttributes::archive(), 0xABCD_EF12, 0, &epoch_ts());
        assert_eq!(read_u16_le(&e, 0x14), 0xABCD);
        assert_eq!(read_u16_le(&e, 0x1A), 0xEF12);
    }

    #[test]
    fn sfn_entry_file_size_at_offset_0x1c() {
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let e = synthesize_sfn_entry(&s, FileAttributes::archive(), 5, 0x1234_5678, &epoch_ts());
        assert_eq!(read_u32_le(&e, 0x1C), 0x1234_5678);
    }

    #[test]
    fn sfn_entry_timestamps_pack_into_correct_offsets() {
        // Use distinct creation/modified/accessed values so we
        // can prove each field landed at the right offset.
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let ts = Timestamps {
            created: FatDateTime::new(
                FatDate::new(2024, 1, 15).unwrap(),
                FatTime::new(10, 20, 30).unwrap(),
            ),
            modified: FatDateTime::new(
                FatDate::new(2024, 3, 10).unwrap(),
                FatTime::new(14, 45, 50).unwrap(),
            ),
            accessed: FatDate::new(2024, 5, 20).unwrap(),
        };
        let e = synthesize_sfn_entry(&s, FileAttributes::archive(), 5, 100, &ts);
        assert_eq!(read_u16_le(&e, 0x0E), ts.created.time.to_bits(), "CrtTime");
        assert_eq!(read_u16_le(&e, 0x10), ts.created.date.to_bits(), "CrtDate");
        assert_eq!(read_u16_le(&e, 0x12), ts.accessed.to_bits(), "LstAccDate");
        assert_eq!(read_u16_le(&e, 0x16), ts.modified.time.to_bits(), "WrtTime");
        assert_eq!(read_u16_le(&e, 0x18), ts.modified.date.to_bits(), "WrtDate");
    }

    #[test]
    fn sfn_entry_directory_attr_uses_zero_size() {
        // We don't enforce file_size == 0 for directories — but
        // verify the caller's 0 is preserved.
        let s = ShortName::from_padded_str("SUBDIR").unwrap();
        let e = synthesize_sfn_entry(&s, FileAttributes::directory(), 17, 0, &epoch_ts());
        assert_eq!(e[0x0B], ATTR_DIRECTORY);
        assert_eq!(read_u32_le(&e, 0x1C), 0);
    }

    #[test]
    fn sfn_entry_hand_built_full_match() {
        // FOO.TXT, archive, cluster 0x0203_0405, size 0x07060504,
        // all timestamps at epoch.
        let s = ShortName::from_padded_str("FOO.TXT").unwrap();
        let e = synthesize_sfn_entry(
            &s,
            FileAttributes::archive(),
            0x0203_0405,
            0x0706_0504,
            &epoch_ts(),
        );

        let mut expected = [0u8; 32];
        expected[0x00..0x0B].copy_from_slice(b"FOO     TXT");
        expected[0x0B] = ATTR_ARCHIVE;
        // NTRes = 0, CrtTimeTenth = 0 — already zero.
        // CrtTime = 0 (midnight), CrtDate = 0x0021 (1980-01-01)
        expected[0x0E..0x10].copy_from_slice(&0u16.to_le_bytes());
        expected[0x10..0x12].copy_from_slice(&0x0021u16.to_le_bytes());
        // LstAccDate = 0x0021
        expected[0x12..0x14].copy_from_slice(&0x0021u16.to_le_bytes());
        // FstClusHI = 0x0203 (high 16 bits of 0x0203_0405)
        expected[0x14..0x16].copy_from_slice(&0x0203u16.to_le_bytes());
        // WrtTime = 0, WrtDate = 0x0021
        expected[0x16..0x18].copy_from_slice(&0u16.to_le_bytes());
        expected[0x18..0x1A].copy_from_slice(&0x0021u16.to_le_bytes());
        // FstClusLO = 0x0405 (low 16 bits)
        expected[0x1A..0x1C].copy_from_slice(&0x0405u16.to_le_bytes());
        // FileSize = 0x07060504
        expected[0x1C..0x20].copy_from_slice(&0x0706_0504u32.to_le_bytes());

        assert_eq!(e, expected);
    }

    // ── LFN sequence ─────────────────────────────────────────────────

    #[test]
    fn lfn_empty_name_rejected() {
        let err = synthesize_lfn_sequence("", 0).expect_err("empty name");
        assert_eq!(err, LfnError::NameEmpty);
    }

    #[test]
    fn lfn_too_long_name_rejected() {
        let name: String = "A".repeat(LFN_MAX_CHARS + 1);
        let err = synthesize_lfn_sequence(&name, 0).expect_err("256-char name");
        assert!(matches!(err, LfnError::NameTooLong { units: 256, .. }));
    }

    #[test]
    fn lfn_invalid_unit_slash_rejected() {
        // '/' (0x2F) is a path separator and is explicitly
        // forbidden in long names per fatgen103 §7.
        let err = synthesize_lfn_sequence("foo/bar", 0).expect_err("slash rejected");
        assert!(matches!(
            err,
            LfnError::NameHasInvalidUnit {
                offset: 3,
                unit: 0x002F
            }
        ));
    }

    #[test]
    fn lfn_invalid_unit_control_char_rejected() {
        let err = synthesize_lfn_sequence("foo\x01bar", 0).expect_err("control char rejected");
        assert!(matches!(
            err,
            LfnError::NameHasInvalidUnit {
                offset: 3,
                unit: 0x0001
            }
        ));
    }

    #[test]
    fn lfn_invalid_unit_del_rejected() {
        let err = synthesize_lfn_sequence("foo\x7fbar", 0).expect_err("DEL rejected");
        assert!(matches!(
            err,
            LfnError::NameHasInvalidUnit {
                offset: 3,
                unit: 0x007F
            }
        ));
    }

    #[test]
    fn lfn_allows_sfn_forbidden_chars() {
        // '+', ',', ';', '=', '[', ']' are forbidden in SFN per
        // fatgen103 §6.1 but explicitly permitted in LFN per §7.
        let _ = synthesize_lfn_sequence("foo+bar,baz;[qux]=quux", 0xAB).expect("valid LFN");
    }

    #[test]
    fn lfn_short_name_one_entry() {
        // "HELLO" — 5 chars → 1 LFN entry. Last entry has bit 6 set.
        let entries = synthesize_lfn_sequence("HELLO", 0x55).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0][0x00], 1 | LAST_LONG_ENTRY);
        assert_eq!(entries[0][0x0B], ATTR_LONG_NAME);
        assert_eq!(entries[0][0x0C], 0, "Type byte");
        assert_eq!(entries[0][0x0D], 0x55, "Checksum byte");
        assert_eq!(read_u16_le(&entries[0], 0x1A), 0, "FstClusLO must be 0");
        // Name1 (5 chars) = "HELLO" (UCS-2 LE)
        assert_eq!(read_u16_le(&entries[0], 0x01), u16::from(b'H'));
        assert_eq!(read_u16_le(&entries[0], 0x03), u16::from(b'E'));
        assert_eq!(read_u16_le(&entries[0], 0x05), u16::from(b'L'));
        assert_eq!(read_u16_le(&entries[0], 0x07), u16::from(b'L'));
        assert_eq!(read_u16_le(&entries[0], 0x09), u16::from(b'O'));
        // Terminator at slot 5 (Name2 start, 0x0E).
        assert_eq!(read_u16_le(&entries[0], 0x0E), 0x0000, "NUL terminator");
        // Subsequent slots are 0xFFFF guards.
        assert_eq!(read_u16_le(&entries[0], 0x10), 0xFFFF);
        assert_eq!(read_u16_le(&entries[0], 0x1C), 0xFFFF, "Name3 slot 0");
        assert_eq!(read_u16_le(&entries[0], 0x1E), 0xFFFF, "Name3 slot 1");
    }

    #[test]
    fn lfn_thirteen_char_name_has_no_terminator_in_entry() {
        // 13 chars exactly fills one LFN entry, so the spec says
        // a SECOND empty entry is NOT generated; the terminator
        // appears in the SFN by virtue of the SFN's existence.
        // (More importantly, this entry has all 13 slots filled
        // with real chars and no 0xFFFF guards.)
        let name = "ABCDEFGHIJKLM"; // 13 chars
        let entries = synthesize_lfn_sequence(name, 0x00).unwrap();
        assert_eq!(entries.len(), 1);
        // No terminator/guard anywhere.
        for (slot, ch) in name.chars().enumerate() {
            let offset = lfn_slot_offset(slot);
            assert_eq!(
                read_u16_le(&entries[0], offset),
                ch as u16,
                "slot {slot} char {ch}"
            );
        }
    }

    #[test]
    fn lfn_fourteen_char_name_spans_two_entries() {
        // 14 chars → 2 entries. On-disk order: entry 0 holds the
        // SECOND chunk (chars 14, plus terminator + guards);
        // entry 1 holds the FIRST chunk (chars 1..=13).
        let name = "ABCDEFGHIJKLMN"; // 14 chars
        let entries = synthesize_lfn_sequence(name, 0x00).unwrap();
        assert_eq!(entries.len(), 2);
        // Entry 0 is ordinal 2 with LAST_LONG_ENTRY bit set.
        assert_eq!(entries[0][0x00], 2 | LAST_LONG_ENTRY);
        // Its first slot holds 'N' (char 14).
        assert_eq!(read_u16_le(&entries[0], 0x01), u16::from(b'N'));
        // Slot 1 is the NUL terminator.
        assert_eq!(read_u16_le(&entries[0], 0x03), 0x0000);
        // Slot 2 is a 0xFFFF guard.
        assert_eq!(read_u16_le(&entries[0], 0x05), 0xFFFF);
        // Entry 1 is ordinal 1, no LAST bit, holds chars 1..=13.
        assert_eq!(entries[1][0x00], 1);
        for (slot, ch) in "ABCDEFGHIJKLM".chars().enumerate() {
            assert_eq!(
                read_u16_le(&entries[1], lfn_slot_offset(slot)),
                ch as u16,
                "entry 1 slot {slot}"
            );
        }
    }

    #[test]
    fn lfn_max_length_name_uses_twenty_entries() {
        let name: String = "A".repeat(LFN_MAX_CHARS);
        let entries = synthesize_lfn_sequence(&name, 0x55).unwrap();
        assert_eq!(entries.len(), LFN_MAX_ENTRIES);
        // First entry on disk = ordinal 20 with LAST_LONG_ENTRY.
        assert_eq!(entries[0][0x00], 20 | LAST_LONG_ENTRY);
        // Last entry on disk = ordinal 1.
        assert_eq!(entries[LFN_MAX_ENTRIES - 1][0x00], 1);
        // Every entry carries the same checksum byte.
        for e in &entries {
            assert_eq!(e[0x0D], 0x55);
            assert_eq!(e[0x0B], ATTR_LONG_NAME);
            assert_eq!(read_u16_le(e, 0x1A), 0, "FstClusLO must be 0");
        }
    }

    #[test]
    fn lfn_unicode_bmp_encodes_to_ucs2_le() {
        // "café" — 'é' is U+00E9.
        let entries = synthesize_lfn_sequence("café", 0xAA).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(read_u16_le(&entries[0], 0x01), u16::from(b'c'));
        assert_eq!(read_u16_le(&entries[0], 0x03), u16::from(b'a'));
        assert_eq!(read_u16_le(&entries[0], 0x05), u16::from(b'f'));
        assert_eq!(read_u16_le(&entries[0], 0x07), 0x00E9, "U+00E9 'é'");
        // Slot 4 is the NUL terminator.
        assert_eq!(read_u16_le(&entries[0], 0x09), 0x0000);
    }

    #[test]
    fn lfn_supplementary_plane_uses_utf16_surrogate_pair() {
        // U+1F600 GRINNING FACE → surrogate pair D83D DE00.
        // String "x😀" encodes to 3 UTF-16 units: 'x' (0x78),
        // 0xD83D, 0xDE00.
        let entries = synthesize_lfn_sequence("x😀", 0xAA).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(read_u16_le(&entries[0], 0x01), 0x0078);
        assert_eq!(read_u16_le(&entries[0], 0x03), 0xD83D);
        assert_eq!(read_u16_le(&entries[0], 0x05), 0xDE00);
        assert_eq!(read_u16_le(&entries[0], 0x07), 0x0000, "terminator");
    }

    #[test]
    fn lfn_checksum_propagates_to_every_entry() {
        let name: String = "A".repeat(40); // 4 entries.
        let entries = synthesize_lfn_sequence(&name, 0x99).unwrap();
        assert_eq!(entries.len(), 4);
        for e in &entries {
            assert_eq!(e[0x0D], 0x99, "checksum byte at offset 0x0D");
        }
    }

    #[test]
    fn lfn_round_trip_with_sfn_checksum() {
        // Build the SFN, compute its checksum, hand it to the
        // LFN synth, verify the entry's checksum byte matches.
        let sfn = ShortName::from_padded_str("FOO.TXT").unwrap();
        let cksum = sfn.checksum();
        let entries = synthesize_lfn_sequence("hello world.txt", cksum).unwrap();
        for e in &entries {
            assert_eq!(e[0x0D], cksum);
        }
    }

    // ── Dot/dotdot entries (subdirectories) ──────────────────────────

    #[test]
    fn dot_entries_have_correct_names_and_attrs() {
        let [dot, dotdot] = synthesize_dot_entries(17, 2, &epoch_ts());
        assert_eq!(&dot[0x00..0x0B], b".          ");
        assert_eq!(&dotdot[0x00..0x0B], b"..         ");
        assert_eq!(dot[0x0B], ATTR_DIRECTORY);
        assert_eq!(dotdot[0x0B], ATTR_DIRECTORY);
    }

    #[test]
    fn dot_points_to_subdirectory_itself() {
        let [dot, _] = synthesize_dot_entries(17, 2, &epoch_ts());
        assert_eq!(read_u16_le(&dot, 0x14), 0, "FstClusHI of small cluster 17");
        assert_eq!(read_u16_le(&dot, 0x1A), 17, "FstClusLO = this cluster");
        assert_eq!(read_u32_le(&dot, 0x1C), 0, "size = 0 for directories");
    }

    #[test]
    fn dotdot_points_to_parent_cluster() {
        let [_, dotdot] = synthesize_dot_entries(17, 5, &epoch_ts());
        assert_eq!(read_u16_le(&dotdot, 0x1A), 5, "FstClusLO = parent cluster");
    }

    #[test]
    fn dotdot_parent_zero_for_subdirs_of_root() {
        // Per fatgen103 §6.5.2, a subdirectory whose parent IS
        // the root directory writes parent_cluster = 0, not 2.
        // The caller passes that explicitly.
        let [_, dotdot] = synthesize_dot_entries(17, 0, &epoch_ts());
        assert_eq!(read_u16_le(&dotdot, 0x1A), 0);
        assert_eq!(read_u16_le(&dotdot, 0x14), 0);
    }

    #[test]
    fn dot_entries_carry_high_cluster_bits() {
        // For cluster 0xABCD_EF12 the high 16 bits must land at
        // FstClusHI (0x14) — same split as a regular file entry.
        let [dot, _] = synthesize_dot_entries(0xABCD_EF12, 0, &epoch_ts());
        assert_eq!(read_u16_le(&dot, 0x14), 0xABCD);
        assert_eq!(read_u16_le(&dot, 0x1A), 0xEF12);
    }

    // ── Volume label entry ─────────────────────────────────────────

    #[test]
    fn volume_label_entry_name_field_is_the_supplied_label() {
        let label: [u8; VOLUME_LABEL_NAME_LEN] = *b"TESLACAM   ";
        let entry = synthesize_volume_label_entry(&label, &epoch_ts());
        assert_eq!(&entry[0x00..0x0B], &label[..]);
    }

    #[test]
    fn volume_label_entry_attribute_is_volume_id_alone() {
        let label: [u8; VOLUME_LABEL_NAME_LEN] = *b"TESLACAM   ";
        let entry = synthesize_volume_label_entry(&label, &epoch_ts());
        assert_eq!(entry[0x0B], ATTR_VOLUME_ID);
        // Spec: ATTR_VOLUME_ID must NOT be combined with
        // ATTR_DIRECTORY (0x10) or ATTR_ARCHIVE (0x20) on the
        // volume label entry. fsck.vfat enforces this.
        assert_eq!(entry[0x0B] & ATTR_DIRECTORY, 0);
        assert_eq!(entry[0x0B] & ATTR_ARCHIVE, 0);
    }

    #[test]
    fn volume_label_entry_first_cluster_and_size_are_zero() {
        let label: [u8; VOLUME_LABEL_NAME_LEN] = *b"TESLACAM   ";
        let entry = synthesize_volume_label_entry(&label, &epoch_ts());
        assert_eq!(read_u16_le(&entry, 0x14), 0, "FstClusHI must be 0");
        assert_eq!(read_u16_le(&entry, 0x1A), 0, "FstClusLO must be 0");
        assert_eq!(read_u32_le(&entry, 0x1C), 0, "FileSize must be 0");
    }

    #[test]
    fn volume_label_entry_carries_caller_timestamps() {
        let label: [u8; VOLUME_LABEL_NAME_LEN] = *b"BACKUP     ";
        let ts = sample_ts();
        let entry = synthesize_volume_label_entry(&label, &ts);
        assert_eq!(read_u16_le(&entry, 0x0E), ts.created.time.to_bits());
        assert_eq!(read_u16_le(&entry, 0x10), ts.created.date.to_bits());
        assert_eq!(read_u16_le(&entry, 0x12), ts.accessed.to_bits());
        assert_eq!(read_u16_le(&entry, 0x16), ts.modified.time.to_bits());
        assert_eq!(read_u16_le(&entry, 0x18), ts.modified.date.to_bits());
    }

    #[test]
    fn volume_label_entry_is_exactly_one_directory_entry() {
        let label: [u8; VOLUME_LABEL_NAME_LEN] = *b"NO NAME    ";
        let entry = synthesize_volume_label_entry(&label, &epoch_ts());
        assert_eq!(entry.len(), DIR_ENTRY_SIZE_BYTES);
    }

    #[test]
    fn volume_label_entry_does_not_collide_with_long_name_attr() {
        // A directory entry whose Attr == 0x0F is interpreted as
        // LFN by the FAT driver, regardless of the rest of its
        // bytes. The volume label entry must carry 0x08 (not
        // 0x0F) so it's never mistaken for an LFN slot.
        let label: [u8; VOLUME_LABEL_NAME_LEN] = *b"TESLACAM   ";
        let entry = synthesize_volume_label_entry(&label, &epoch_ts());
        assert_ne!(entry[0x0B], ATTR_LONG_NAME);
    }
}
