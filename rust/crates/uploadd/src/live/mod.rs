//! Live Unix adapters for `uploadd` seams.

/// Live `indexd`-backed queue and lease adapters.
pub mod indexd;
/// Live system adapters (archive I/O, throttle, time, subprocess).
pub mod system;
