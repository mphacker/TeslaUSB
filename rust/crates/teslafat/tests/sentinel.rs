//! Phase 1.1 sentinel integration test.
//!
//! Runs the compiled `teslafat` binary against a known-good TOML
//! fixture and asserts:
//!
//! * Exit code is `0` on a valid config.
//! * stderr contains the JSON-formatted "started" sentinel line with
//!   the structured fields from the fixture (`volume_size_gb`,
//!   `volume_label`).
//! * Exit code is non-zero when the config path does not exist, and
//!   the error message names the path so the operator can find it.
//! * Exit code is non-zero when post-parse validation rejects a
//!   value, and the error names the field.
//!
//! This satisfies the "sentinel `started` log line" deliverable in
//! `docs/00-PLAN.md` Phase 1.1.

// Integration tests are a separate compilation unit from the main
// crate, so the `#![cfg_attr(test, allow(clippy::unwrap_used))]`
// attribute on `src/main.rs` does not reach here. `unwrap` on test
// setup (tempfile creation, fixture writes) is idiomatic in
// integration tests — the charter explicitly carves out tests in the
// §"Lints" discussion.
#![allow(clippy::unwrap_used)]

use std::io::Write;

use assert_cmd::Command;
use predicates::str;
use tempfile::NamedTempFile;

const FIXTURE_TOML: &str = "\
disk_signature = 0x12345678

[nbd]
socket_path = \"/run/teslausb/teslafat.sock\"

[[partition]]
backing_root = \"/var/teslacam\"
volume_size_gb = 64
volume_label = \"TESLACAM\"
fs_type = \"exfat\"

[partition.retention]
recentclips_hide_after_seconds = 1800
";

fn write_fixture(body: &[u8]) -> NamedTempFile {
    let mut f = NamedTempFile::new().unwrap();
    f.write_all(body).unwrap();
    f.flush().unwrap();
    f
}

#[test]
fn emits_started_sentinel_on_stderr_and_exits_clean() {
    let fixture = write_fixture(FIXTURE_TOML.as_bytes());

    Command::cargo_bin("teslafat")
        .unwrap()
        .arg("--config")
        .arg(fixture.path())
        // `--check-config` is the Phase 1.1 "validate and exit"
        // mode: load config, emit sentinel, return 0 without
        // binding the NBD socket. The Phase 1.6 default behaviour
        // is to bind the socket and block until SIGTERM, which
        // would hang `assert_cmd` indefinitely.
        .arg("--check-config")
        // Be explicit so a developer's exported RUST_LOG doesn't
        // silently suppress the line the test is asserting on.
        .env("RUST_LOG", "info")
        .assert()
        .success()
        .stderr(str::contains(r#""message":"started""#))
        .stderr(str::contains(r#""volume_size_gb":64"#))
        .stderr(str::contains(r#""volume_label":"TESLACAM""#));
}

#[test]
fn missing_config_exits_failure_with_path_in_error() {
    Command::cargo_bin("teslafat")
        .unwrap()
        .arg("--config")
        .arg("/definitely/does/not/exist/teslafat.toml")
        .env("RUST_LOG", "info")
        .assert()
        .failure()
        .stderr(str::contains("/definitely/does/not/exist/teslafat.toml"));
}

#[test]
fn invalid_volume_size_in_config_exits_failure() {
    let fixture = write_fixture(b"[[partition]]\nbacking_root = \"/x\"\nvolume_size_gb = 1\n");

    Command::cargo_bin("teslafat")
        .unwrap()
        .arg("--config")
        .arg(fixture.path())
        .env("RUST_LOG", "info")
        .assert()
        .failure()
        .stderr(str::contains("volume_size_gb"));
}

#[test]
fn unknown_field_in_config_exits_failure() {
    let fixture = write_fixture(
        b"[[partition]]\nbacking_root = \"/x\"\nvolume_size_gb = 64\nnot_a_field = \"boom\"\n",
    );

    Command::cargo_bin("teslafat")
        .unwrap()
        .arg("--config")
        .arg(fixture.path())
        .env("RUST_LOG", "info")
        .assert()
        .failure()
        .stderr(str::contains("not_a_field"));
}
