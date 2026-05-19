# TeslaUSB B-1 cross-build image: aarch64-unknown-linux-gnu
#
# Why podman / a container at all?
#   The Pi Zero 2 W (464 MB RAM, single SDIO bus) cannot host the
#   Rust toolchain — two attempts during Phase H1 to `cargo build`
#   on-device both wedged the device (one hard power cycle, one
#   sysrq+b reboot). See ADR-0008. Every Rust binary that ships to
#   the Pi MUST be cross-compiled from a dev box; this image is
#   the canonical, reproducible way to do that.
#
# Why pin Rust 1.85.0 explicitly?
#   `rust/rust-toolchain.toml` pins `channel = "1.85.0"`. The base
#   image `rust:1-bookworm` ships a newer minor (1.95.x at the time
#   of writing); rustup respects the toolchain file but the 1.85
#   aarch64 std/core artifacts are not present by default, so
#   `cargo build --target=aarch64-unknown-linux-gnu` fails with
#   "can't find crate for `core`". Installing the pinned toolchain
#   + aarch64 target inside the image avoids that surprise.
#
# Reusability:
#   Mount three named volumes for cargo cache + cargo git + target
#   dir to make warm builds ~12 s instead of ~5 min. See
#   `tools/xbuild/README.md` for the exact `podman run` invocation.

FROM docker.io/rust:1-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc-aarch64-linux-gnu \
        libc6-dev-arm64-cross \
 && rm -rf /var/lib/apt/lists/*

# Pin the workspace toolchain so rust-toolchain.toml resolves locally
RUN rustup toolchain install 1.85.0 --profile minimal \
 && rustup component add --toolchain 1.85.0 rustfmt clippy \
 && rustup target add --toolchain 1.85.0 aarch64-unknown-linux-gnu \
 && rustup default 1.85.0

ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
ENV CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc

WORKDIR /work
