---
name: hardware-test
description: >
  Run a hardware spike / PoC or a migration step on the live B-1
  hardware target (`cybertruckusb.local`, user `pi`) using the
  mandatory safety wrapper: dead-man reboot timer, SSH liveness
  checks, file backups, idempotent operations. Use when asked to
  run a hardware spike from the de-risking backlog
  (`docs/specs/hardware-first-development.md`), deploy a B-1
  binary to the Pi, run a migration step
  (`docs/specs/migration.md`), verify a hardware change, capture
  diagnostics from the device, or do any work that requires the
  live device to be touched. NEVER use this skill for v1
  production work — it is B-1-only and assumes the device runs
  (or is being decommissioned to) the B-1 architecture. Refuses
  to proceed without explicit operator confirmation when the
  action could affect SSH, WiFi, or boot.
---

# Hardware Test on `cybertruckusb.local`

This skill is the single sanctioned way to interact with the
B-1 hardware target. Every other approach (raw `ssh`, raw
`scp`, ad-hoc `sshpass`, custom Python paramiko scripts) is
forbidden because they bypass the safety net.

**Operator directives this enforces (binding):**

> *"You will use the device at cybertruckusb.local (login with
> the account pi) for testing... You do need to be very careful
> to not knock it offline (break wifi connection), cause boot
> issue, or cause anything that would block you from SSH into
> the device."* — 2026-05-19

> *"Don't do a ton of work and wait to do code reviews. Have
> specific code review breaks and then fix ALL issues you find."*
> — 2026-05-19

**Second opinion before risky actions (binding).** When this skill
is reached as the culmination of *solving an issue* (a deploy that
fixes a bug, a diagnostic to root-cause a hardware fault), follow the
parallel GPT-5.5 second-opinion workflow in
`.github/copilot-instructions.md`: have a GPT-5.5 agent independently
research the problem, reconcile its view with yours into a single root
cause + plan, and **submit the deploy/diagnostic plan to the GPT-5.5
agent for review before touching the live device**. Live-hardware and
recording-critical actions must not proceed on a single (your own)
opinion alone.

The three sacred rails:

1. **SSH must stay up.** If it goes down between two of our
   commands, we cannot recover the device remotely. The
   operator would have to physically remove the Pi from the
   Cybertruck.
2. **WiFi must stay up.** SSH rides over WiFi. Any
   NetworkManager / wpa_supplicant change is GUARDED by a
   dead-man reboot.
3. **Boot must succeed.** Edits to `/boot/firmware/cmdline.txt`,
   `/boot/firmware/config.txt`, `/etc/fstab`, or kernel modules
   require a `.b1-backup-<timestamp>` sibling created BEFORE
   the edit, AND a dead-man reboot armed BEFORE the change is
   written.

---

## Phase 0 — Prerequisites

### Confirm the target

```bash
echo "B1_TARGET_HOST=${B1_TARGET_HOST:-cybertruckusb.local}"
echo "B1_TARGET_USER=${B1_TARGET_USER:-pi}"
```

If the user hasn't confirmed the target this session, ask:

> "Hardware test will target `cybertruckusb.local` (user `pi`).
> Confirm to proceed, or specify an alternate host."

### Verify known_hosts

```bash
ssh-keygen -F "${B1_TARGET_HOST:-cybertruckusb.local}"
```

If this returns empty, **stop and ask the operator** to
manually `ssh pi@cybertruckusb.local` once to record the host
key. Auto-accepting a host key is a security failure.

### Verify SSH key auth (no password prompts)

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${B1_TARGET_USER:-pi}@${B1_TARGET_HOST:-cybertruckusb.local}" \
    'echo SSH_OK'
```

If this prompts for a password or fails, stop and ask the
operator to set up SSH key auth. Password-prompt SSH is
incompatible with the dead-man wrapper.

### Confirm baseline alive

```bash
ssh ... 'uptime && systemctl is-system-running'
```

Capture the baseline. Any later check shows degradation, we
have evidence.

---

## Phase 1 — Scope resolution

Determine **what** is being run and **why it is safe to run now**. Every run is
one of:

- A **spike** from the de-risking backlog in
  [`docs/specs/hardware-first-development.md`](../../../docs/specs/hardware-first-development.md)
  (e.g. *LUN acceptance*, *Eject / rebind*, *Boot time*, *Parse stability*,
  *SEI / HUD*, *WiFi TX cap*, *microSD contention*, *disk.img sizing*). The spike
  MUST have a written **pass/fail predicate** and the *smallest* throwaway probe
  that answers it.
- A **migration step** from
  [`docs/specs/migration.md`](../../../docs/specs/migration.md) (the in-place
  M-series rollout / v1 decommissioning).
- An **ad-hoc diagnostic** — a read-only capture to root-cause a hardware fault.

Refuse to run if:

- A spike has **no written pass/fail predicate**. Frame it first (the spike loop
  in `hardware-first-development.md` §3).
- A spike/step that this one **gates on** has not PASSed. Honor the ordering in
  `hardware-first-development.md` §5 (e.g. don't build past *LUN acceptance* until
  it PASSes).
- The work is a **long buildout on an unproven hardware assumption** — the exact
  failure mode the hardware-first methodology exists to prevent. Spike it first.

This skill is the only sanctioned path to the device and enforces the
hardware-first gates; it does not replace them.

---

## Phase 2 — Arm the dead-man switch

Before any potentially-disruptive remote command, arm a
self-rebooting timer on the device. If our test wedges or
locks us out, the device reboots itself in 3 minutes.

```bash
ssh ... bash <<'EOF'
sudo systemd-run --on-active=180 --unit=b1-deadman \
    --description="B-1 hardware-test dead-man switch (3 min)" \
    /sbin/reboot
echo "Dead-man armed at $(date -Is); device will reboot in 180s if not cancelled"
EOF
```

Record the start time. The skill's internal timer must
remember to cancel the dead-man if the test completes:

```bash
ssh ... 'sudo systemctl stop b1-deadman.timer 2>/dev/null; sudo systemctl reset-failed b1-deadman.timer 2>/dev/null; echo dead-man-cancelled'
```

**If the test takes > 150 seconds**, re-arm the dead-man
periodically (every 120 seconds, e.g., between sub-steps).
Better to re-arm too often than miss a wedge.

---

## Phase 3 — Snapshot first, mutate after

For any step that modifies files outside our own
`/home/pi/teslausb-b1/` sandbox, snapshot first:

```bash
ssh ... bash <<EOF
sudo cp -a /etc/systemd/system/gadget_web.service \
          /etc/systemd/system/gadget_web.service.b1-backup-\$(date +%Y%m%d-%H%M%S)
EOF
```

For `~/TeslaUSB` v1 decommissioning specifically (see
[`migration.md`](../../../docs/specs/migration.md) M-series):

```bash
ssh ... 'sudo tar -czf /home/pi/v1-backup-$(date +%Y%m%d).tar.gz \
    --exclude=/home/pi/ArchivedClips \
    /etc/ /home/pi/TeslaUSB 2>&1 | tail -30'
scp pi@cybertruckusb.local:/home/pi/v1-backup-*.tar.gz \
    ~/.copilot/session-state/<sid>/files/
ssh ... 'sha256sum /home/pi/v1-backup-*.tar.gz'
sha256sum ~/.copilot/session-state/<sid>/files/v1-backup-*.tar.gz
```

Sha256 sums MUST match. Otherwise the `scp` was corrupt;
retry.

---

## Phase 4 — Execute

For each step of the spike probe (or migration step):

1. Print the step description to the user.
2. Run the command via the SSH wrapper:
   ```bash
   ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
       -o ConnectTimeout=10 -o BatchMode=yes \
       pi@cybertruckusb.local \
       'set -euo pipefail; <command>'
   ```
3. **Liveness check after every step:**
   ```bash
   ssh ... 'echo alive && uptime'
   ```
   If this fails:
   - Wait 30 seconds (transient network blip).
   - Retry once.
   - If still failing, STOP. Wait for the dead-man reboot
     (180 seconds from arming). Try again.
   - If still failing after dead-man, **escalate to operator
     immediately** with the full transcript. Do not attempt
     further commands.
4. Log every command + exit code + (truncated) stdout/stderr
   to `~/.copilot/session-state/<sid>/files/hw-<spike>-<timestamp>.log`.
5. Re-arm dead-man if elapsed > 120 s since last arm.

---

## Phase 5 — Verify post-conditions

Every run has explicit post-conditions: for a **spike**, the **pass/fail
predicate** framed in Phase 1; for a **migration step**, its "verify after"
checks in [`migration.md`](../../../docs/specs/migration.md). Each one MUST pass
before the run is marked done. Common verifications:

| Verification | Command |
|---|---|
| SSH alive | `ssh ... 'echo alive'` |
| WiFi alive | `ssh ... 'nmcli -t -f STATE general && nmcli -t -f DEVICE,STATE device | grep wlan0'` |
| Boot success | `ssh ... 'systemctl is-system-running'` (`running` or `degraded` OK; `maintenance` = bad) |
| Service expected state | `ssh ... 'systemctl is-active <unit>; systemctl is-enabled <unit>'` |
| Free space reclaimed | `ssh ... 'df -h /home/pi'` |
| Specific file present | `ssh ... 'sha256sum <path>'` |
| Specific file ABSENT | `ssh ... '! test -e <path> || (echo FAIL: file still present; exit 1)'` |
| Process running | `ssh ... 'pgrep -f <binary> >/dev/null && echo running'` |
| Process NOT running | `ssh ... '! pgrep -f <binary>'` |

If ANY verification fails, the run is FAILED. Do not mark it
done. Stop and report. Do not start the next spike or step.

---

## Phase 6 — Cancel dead-man + capture journal

```bash
ssh ... 'sudo systemctl stop b1-deadman.timer 2>/dev/null; echo dead-man-cancelled'
ssh ... 'sudo journalctl --since "10 min ago" --no-pager' \
    > ~/.copilot/session-state/<sid>/files/hw-<spike>-journal-<timestamp>.log
```

---

## Phase 7 — Report

Append to `~/.copilot/session-state/<sid>/files/hw-results.md`:

```markdown
## Hardware run: <spike-or-step name>

- **Started:** <ISO>
- **Ended:** <ISO>
- **Target:** cybertruckusb.local (pi)
- **Kind:** spike | migration step | diagnostic
- **Pass/fail predicate:** <the predicate framed in Phase 1>
- **Result:** PASS / FAIL / INCONCLUSIVE

### Steps
| # | Description | Status | Notes |
|---|---|---|---|
| 1 | snapshot | ✅ | sha256 match |
| 2 | <probe step> | ✅ | <observed signal> |
| ... | | | |

### Proven parameters (on PASS)
| Parameter | Measured value |
|---|---|
| <e.g. TX cap Mbps / boot seconds / dropout window / disk.img size> | <value> |

### Verifications
| Check | Result |
|---|---|
| SSH alive (final) | ✅ |
| WiFi alive (final) | ✅ |
| Boot status | running |
| Journal contains no error | ✅ |
| Specific post-conditions | ✅ |

### Logs
- Command log: `hw-<spike>-<timestamp>.log`
- Journal: `hw-<spike>-journal-<timestamp>.log`
- Snapshots: `<list>`

### Next action
- [✅ if PASS] **Fold the proven parameters back into the owning spec** (and mark
  the corresponding `SPEC.md` §9 unknown resolved), per
  `hardware-first-development.md` §3–§4. Discard the throwaway probe. Unblock the
  dependent buildout.
- [⚠️ if INCONCLUSIVE] Refine the predicate / add instrumentation and re-run.
  Never downgrade "inconclusive" to "probably fine."
- [❌ if FAIL] STOP. Surface failure to operator. Do not build on the assumption;
  pivot to the documented alternative or escalate for an architecture decision.
```

If PASS, surface to user: "<spike> PASSed. I'll record the proven parameters in
`<owning spec>` and mark `SPEC.md` §9 #<n> resolved. Proceed?"

If FAIL, surface to user with full transcript and **do not suggest a workaround**.
The operator decides whether to rollback (via the `.b1-backup` snapshots and the
`v1-backup` tarball) or to re-frame the architecture. A FAIL caught in a spike is
a cheap win, not a setback (`hardware-first-development.md` §1).

---

## Refuse-to-proceed conditions

Hard stops where the skill SHALL NOT do the operation:

- The step touches `/etc/ssh/`, `/etc/NetworkManager/`,
  `/etc/wpa_supplicant/`, `/etc/sudoers`, or
  `/etc/systemd/system/sshd*` AND operator has not explicitly
  confirmed the change in THIS session's prior turn.
- The step proposes to edit `/boot/firmware/cmdline.txt`
  or `config.txt` without a `.b1-backup-<timestamp>` step
  immediately preceding it.
- The step proposes to run `systemctl mask` without first
  running `systemctl disable` and rebooting (two-stage
  reversibility — disable lets us re-enable, mask is harder
  to recover from accidentally).
- The step proposes to `rm -rf` outside `/home/pi/teslausb-b1/`
  or `/home/pi/v1-backup-*` without explicit operator
  confirmation in the prior turn.
- The dead-man wrapper isn't installed (cannot arm timer).
- Three consecutive steps have logged SSH retry warnings —
  the network is unreliable, stop before something breaks.
- The request is a **long buildout on an unproven hardware
  assumption** whose gating spike has not PASSed — spike it
  first (`hardware-first-development.md`).

In all these cases: surface the refusal with the specific rule
that blocked, ask the operator to either confirm or amend the
plan, then resume only after explicit confirmation.

---

## Notes on ordering & cadence

The spike order, gating, and "don't build until PASS" rules live in
[`hardware-first-development.md`](../../../docs/specs/hardware-first-development.md)
§5 — that doc is the single source of truth for *which* spike runs *when*. In
short:

- Run spikes in the gated order: `LUN acceptance → {Eject/rebind, Boot time} →
  {Parse stability, SEI / HUD} → {WiFi TX cap, microSD contention} → disk.img
  sizing`. **LUN acceptance is make-or-break** for the whole S1 architecture.
- A spike is **time-boxed** (≤ ~half a day of device time). Overrunning the box
  is itself a signal — stop, re-frame, or escalate.
- Re-run a spike whenever its inputs change (new firmware, new SD card, new kernel
  module config). A mid-build hardware surprise is a **new spike**, not a reason to
  push through on assumption.
- Soak/longevity runs (e.g. 24 h then 72 h) gate anything that must survive
  continuous car-write + Pi-side I/O over time.

General rule: no **long buildout** starts until its gating spike has PASSed with
**captured parameters**, and non-hardware logic has green automated tests on the
dev box.
