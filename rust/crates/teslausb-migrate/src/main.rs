//! CLI entrypoint for read-only migration discovery and dry-run conversion.

use std::io::Write;
use std::process::ExitCode;

use serde::Serialize;
use teslausb_migrate::convert;
use teslausb_migrate::discover;
use teslausb_migrate::parse_convert_root;
use teslausb_migrate::parse_discover_root;
use teslausb_migrate::parse_plan_root;
use teslausb_migrate::plan;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = args.first().map(String::as_str);
    let root = match command {
        Some("discover") => parse_discover_root(&args),
        Some("plan") => parse_plan_root(&args),
        Some("convert") => parse_convert_root(&args),
        _ => Err(teslausb_migrate::usage()),
    };
    let root = match root {
        Ok(root) => root,
        Err(message) => return fail(&message),
    };
    match command {
        Some("discover") => emit(discover(&root)),
        Some("plan") => emit(plan(&root)),
        Some("convert") => emit(convert(&root)),
        _ => unreachable!(),
    }
}

fn emit<T: Serialize>(result: Result<T, teslausb_migrate::DiscoverError>) -> ExitCode {
    match result.and_then(|report| {
        serde_json::to_string_pretty(&report)
            .map_err(|error| teslausb_migrate::DiscoverError::RootUnreadable(error.to_string()))
    }) {
        Ok(json) => {
            let mut stdout = std::io::stdout().lock();
            if stdout.write_all(json.as_bytes()).is_err() || stdout.write_all(b"\n").is_err() {
                return fail("failed to write migration report");
            }
            ExitCode::SUCCESS
        }
        Err(error) => fail(&error.to_string()),
    }
}

fn fail(message: &str) -> ExitCode {
    let mut stderr = std::io::stderr().lock();
    let _ = stderr.write_all(message.as_bytes());
    let _ = stderr.write_all(b"\n");
    ExitCode::FAILURE
}
