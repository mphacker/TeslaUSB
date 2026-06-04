# SPEC — `wifid` (STA/AP state machine + SDIO chip-reset watchdog)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: disposable (but reliability-sensitive)
> Language: Rust · Reference: `wifi_service.py`, `wifi_hostapd.py`, cloud_archive `wifi.py`.

## 1. Objective

Provide WiFi connectivity for cloud upload and the local UI **without ever
endangering the car's write path**. WiFi is **convenience, not a reliability
dependency**: it must never reboot the Pi while the car is writing, and must
avoid the BCM43436 SDIO deadlock.

## 2. Responsibilities

1. **STA/AP state machine:** connect to home WiFi (STA) when reachable; fall back
   to **AP mode** (hostapd + a DHCP server, e.g. dnsmasq) for onboarding/management
   when home WiFi is unreachable. **Never run AP and STA concurrently.**
   "Reachable" = associated **and** carrier/IP up **and** a cheap reachability
   probe (gateway ping / DNS) succeeds within a timeout — not mere association;
   debounce flaps before switching mode. The **AP must be WPA2** (never open).
2. **Credential storage:** STA PSK and AP passphrase are owned by `wifid` and
   persisted **root-only (`0600`)** (reference: `wifi_service.py`,
   `wifi_hostapd.py`); never world-readable, never logged, never surfaced to the
   SPA. `webd` requests changes via IPC; it does not read the secrets.
3. **TX rate limiting:** enforce a token-bucket / `tc` TX cap (coordinated with
   `uploadd`) to stay **under the SDIO-deadlock threshold** (exact Mbps/chunk
   size from prototype unknown #4).
4. **Liveness watchdog:** detect a wedged chip and recover by **resetting the WiFi
   chip only** (`rmmod/modprobe brcmfmac`) — **not** the whole Pi. A full Pi
   reboot is permitted **only if USB is already idle** (car not writing), and
   even then is a last resort.
5. **AP onboarding** integration with the captive portal (`webd` `/portal`):
   serve the portal over the AP's DHCP/DNS so a joining phone is redirected to it.
6. **Expose status** (mode, link, signal, throttle state) to `webd`.

## 3. Non-responsibilities

- Does not perform uploads (that is `uploadd`; `wifid` only provides/limits the
  link).
- Does not own cloud config.
- Must never take an action that resets/reboots while the car is writing.

## 4. Acceptance criteria

- [ ] Cleanly switches STA↔AP; never both at once.
- [ ] TX stays under the measured SDIO-deadlock threshold under sustained upload.
- [ ] A wedged chip recovers via `rmmod/modprobe` without rebooting the Pi.
- [ ] Never reboots while USB/car write activity is present (verified against the
      `gadgetd` write heartbeat).
- [ ] Runs within `MemoryMax`.

## 5. Testing

- State-machine tests (STA↔AP transitions; mutual exclusion).
- Throttle test (token bucket caps sustained TX at the configured rate).
- Recovery test (simulated wedge → chip reset path chosen, not Pi reboot;
  reboot path gated on USB-idle).

## 6. Boundaries

**ALWAYS** treat WiFi as non-critical; prefer chip reset over Pi reboot; keep TX
under the deadlock threshold; gate any reboot on USB-idle.
**ASK FIRST** before changing the throttle threshold or the recovery escalation
policy.
**NEVER** run AP+STA concurrently; never reboot the Pi while the car is writing;
never let WiFi recovery endanger the write path.
