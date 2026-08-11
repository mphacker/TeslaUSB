//! Live Unix adapters for `uploadd` seams.

/// Live read-only control socket (`get_status`) for `webd`.
pub mod control;
/// Live discover→enqueue producer and archive child hashing seam.
pub mod enqueue;
/// Live `indexd`-backed queue and lease adapters.
pub mod indexd;
/// Live daemon serve loop (`discover -> hydrate -> drain`).
pub mod serve;
/// Live system adapters (archive I/O, throttle, time, subprocess).
pub mod system;
