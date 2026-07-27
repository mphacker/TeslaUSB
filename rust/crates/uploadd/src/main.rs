//! `uploadd` binary entrypoint.
//!
//! Parses top-level commands and forwards `serve` wiring to the live loop.
#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::process::ExitCode;

#[cfg(unix)]
use uploadd::live::serve::{run_serve, serve_usage};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("version" | "--version" | "-V") => {
            println!("uploadd {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("--help" | "-h" | "help") | None => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        Some("serve") => serve(args.get(1..).unwrap_or(&[])),
        Some(other) => {
            eprintln!("uploadd: unknown command `{other}`\n{}", usage());
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    format!("usage: uploadd <version|serve|help>\n{}", serve_subcommand_usage())
}

#[cfg(unix)]
fn serve(args: &[String]) -> ExitCode {
    run_serve(args)
}

#[cfg(not(unix))]
fn serve(_args: &[String]) -> ExitCode {
    eprintln!("uploadd serve: only supported on Unix targets");
    ExitCode::FAILURE
}

#[cfg(unix)]
fn serve_subcommand_usage() -> String {
    serve_usage()
}

#[cfg(not(unix))]
fn serve_subcommand_usage() -> String {
    "serve is only supported on Unix targets".to_owned()
}
