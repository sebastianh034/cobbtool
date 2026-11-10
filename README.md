# COBB AccessPort Marriage Toolkit — Step-by-Step Guide

This is the user guide for `tools/cobbtool.py`. It reads and changes the
**AccessPort's own marriage record** over USB: the pair of EEPROM fields that say
"this unit is installed on vehicle X" or "this unit is not installed".

It needs no car, no soldering, and no COBB account. It does **not** reflash a car
ECU, remove a tune from a car, or contact COBB's servers.

> Scope: use this on hardware you own. Marriage is COBB's licensing mechanism.
> Changing the local record does not change COBB's cloud record.

---

## Contents

1. [Compatibility](#1-compatibility)
2. [What you need](#2-what-you-need)
3. [Before you start](#3-before-you-start)
4. [Step by step](#4-step-by-step)
5. [What the tool checks before writing](#5-what-the-tool-checks-before-writing)
6. [Errors and what to do](#6-errors-and-what-to-do)
7. [Recovery](#7-recovery)
8. [Files and backups](#8-files-and-backups)
9. [Limits](#9-limits)

---

## 1. Compatibility

### Supported

The tool targets the **AP3 family**. Compatibility is decided by probing the
unit, not by its part number.

| Unit | Status |
|---|---|
| `AP3-SUB-003`, firmware `v1.7.4.2-24844` | **Validated** — the unit this tool was built and tested on |
| Other AP3 part numbers | **Experimental** — allowed only if the capability probe passes |
| AP2, AP4, non-COBB hardware | **Not supported** |

The AP3 SKUs share the same rootfs/userland; `ap-app` and `ap-data` differ per
product. That is why the tool checks what the unit actually exposes instead of
keeping a part-number allowlist.

### What "compatible" means here

Before offering a write, the tool runs a probe on the connected unit. It requires:

| Probe check | Why it matters |
|---|---|
| `/user/ap-data` exists | `mapnames`, `backup`, `user.rom` live here |
| `/user/ap-user` exists | staging and `settings` live here |
| `/ap-app/bin/readeeprom` is executable | ground-truth EEPROM read |
| `/ap-app/bin/writeeeprom` is executable | COBB's own EEPROM writer |
| `writeeeprom` carries `--state` | the state argument the tool calls |
| `writeeeprom` carries `--vehicle` | the vehicle argument the tool calls |
| `/ap-app/bin/diagnostic_file` is executable | the eval trigger |
| The probe payload actually ran | proves this boot's eval path works |
| `writeeeprom` carries `cm1_state` | TCM support; informational |

If any required check is missing, the tool prints
`Capability preflight FAILED` and **disables unmarry/re-marry**. Status and
backup still work.

### What the probe does not prove

The probe proves the flags and files exist. It does **not** prove that
`writeeeprom` gives those flags the same meaning on every firmware, that `?` is
the empty marker, or that the EEPROM layout is identical. Those are the
remaining unknowns on an unvalidated AP3. The tool's other checks narrow the
risk; they do not eliminate it.

---

## 2. What you need

### Hardware

- A COBB AccessPort **AP3**.
- A **data** USB cable (not charge-only).
- A computer with a USB-A or USB-C port that can power the AP.
- The AP's screen must be on. No car and no OBD connection are required.

### Software

- **Python 3.9 or newer** (`python3 --version`).
- **[pyusb](https://pypi.org/project/pyusb/)**.
- **libusb 1.0**.
- This repository. Do not copy `cobbtool.py` by itself: it imports the shared
  `core/` modules and locates them relative to the repo.

If `pip` refuses with an "externally managed environment" error (common with
Homebrew Python), use a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install pyusb
```

#### macOS

```sh
brew install libusb
python3 -m pip install pyusb
```

No driver or kernel extension is needed. If USB access is denied, run it from
Terminal rather than an IDE sandbox.

#### Windows

```bat
python -m pip install pyusb libusb
```

Then bind **WinUSB** to the AccessPort once per PC with
[Zadig](https://zadig.akeo.ie):

1. Run Zadig as Administrator.
2. Plug in the AccessPort.
3. **Options → List All Devices**.
4. Pick the entry with vendor ID **1A84**.
5. Set the target driver to **WinUSB** and click **Replace Driver**.

To use COBB Accessport Manager again later, reinstall its driver (or revert in
Zadig). Close Accessport Manager before running this tool — it can claim the
device.

#### Linux

```sh
sudo apt install libusb-1.0-0     # Debian/Ubuntu; use your distro's package name
python3 -m pip install pyusb
```

If you get a permission error, add a udev rule for vendor ID `1A84` or run the
tool with the privileges your distribution needs for raw USB access.

### Device state

- Plug the USB cable in **before** the AP finishes booting, so it comes up in
  the USB "PC mode" the tool can talk to.
- Close COBB Accessport Manager and anything else that may hold the USB device.
- Do not unplug during a probe, write, or reboot.

---

## 3. Before you start

Run this checklist once:

- [ ] Python, pyusb, and libusb are installed.
- [ ] The AP is on USB with its screen on.
- [ ] Accessport Manager is closed.
- [ ] You know this unit's **vehicle code** (for example
      `SUBA_US_WRXM_10`). The backup records it; if the unit is already
      uninstalled and you have no backup, you will have to type it for re-marry.
- [ ] You are ready to leave the AP plugged in for several minutes.
- [ ] You have read the limits in [section 9](#9-limits).

**Back up while the unit is installed.** That is the only time the live EEPROM
still contains the vehicle code the tool needs to put back.

---

## 4. Step by step

### Step 1 — open a terminal in the repository

```sh
cd /path/to/accessport_full
```

Every script locates the repo itself, so this is only for convenience.

### Step 2 — start the tool

```sh
python3 tools/cobbtool.py
```

### Step 3 — read the startup output

You should see something like:

```
============================================================
 COBB AccessPort Marriage Toolkit
============================================================

Connected: AP3-SUB-003  serial SUB0000001  fw v1.7.4.2-24844
State: Installed   Vehicle: 2010 USDM Impreza WRX MT
[init] captured this unit's mapnames (180B) as restore reference.
[init] probing this unit's paths, vendor binaries and eval path (~1 min) ...
[init] capabilities: data=/user/ap-data  user=/user/ap-user  readeeprom=yes  writeeeprom=yes  mapnames-in-diagnostic=yes  tcm=no  (payload executed on this unit)
[init] AP3-SUB-003 / v1.7.4.2-24844 — validated unit.
[init] live EEPROM: AP_STATE=1 AP_VEHICLE=SUBA_US_WRXM_10
```

What the lines mean:

- **Connected / State / Vehicle** — cached device info from the last boot.
- **captured mapnames** — the tool saved the unit's current `mapnames` as its
  restore reference. If this fails, stop; it will not write safely.
- **probing** — the capability check. This takes about a minute.
- **capabilities** — the probe result. `mapnames-in-diagnostic=no` is
  informational; the payload running at all is the stronger proof.
- **validated unit** or **not in the validated set** — the latter means the
  probe passed but this exact part/firmware has not been tested on hardware.

If you see `Capability preflight FAILED`, the unit is read/backup-only. Do not
try to force a write. See [Errors](#6-errors-and-what-to-do).

### Step 4 — show status (menu 1)

```
 1) Show status
 2) Back up this unit  (do this first!)
 3) Unmarry  (state -> Not installed, then reboot)
 4) Re-marry (state -> Installed, then reboot)
 5) Quit
```

Type `1` and press Return. You get part number, serial, firmware, state, and
vehicle. It then asks:

```
  Deep-verify live EEPROM now? (slow ~1min) [y/N]:
```

Say `y` to read the EEPROM directly. This is the ground truth; the status from
`Connected` is only what the device cached at boot.

### Step 5 — back up (menu 2)

Type `2`. The tool saves the unit's state-critical files and a manifest to:

```
units/<serial>/
```

Wait for:

```
[backup] saved N files + manifest.json
[backup] vehicle code on record: SUBA_US_WRXM_10
```

Check that line. If it says `(unknown)`, the tool has no code to re-marry with.
It will ask you to type one before unmarry, or you can stop and find the code.

New backups store files under `units/<serial>/ap-data/` and
`units/<serial>/ap-user/`, so matching filenames cannot overwrite each other.
Each download must match its stat size and the device’s MD5 acknowledgement;
framed responses also require complete frame lengths. A failed download leaves the previous
backup intact. Fallback filename backups remain available for recovery but do
not authorize unmarry. Run menu 2 again to replace an older flat backup.
`user.rom` and `user.md5sum` are excluded because their stat operation can wedge
USB on newer firmware; they are not required for this EEPROM state operation.

Do not delete `units/`. It contains your recovery data.

### Step 6 — unmarry (menu 3)

Type `3`. The tool:

1. Requires a complete directory-discovered backup matching this unit’s serial, part,
   firmware, and captured mapnames; rechecks every saved file’s size and SHA-256.
2. Reads the live EEPROM (~1 minute) and refuses an unrecognized state.
3. Prompts for a vehicle code if the EEPROM has none, and aborts on a blank.
4. Shows the exact command and asks:

```
    This writes the device EEPROM and reboots. Proceed? [type YES]:
```

Type `YES` (exact, uppercase) and press Return. Then:

5. Stages a clean copy of `mapnames` and verifies it read back.
6. Runs the cleanup dry run: `mount`, `cp`, `cmp`, `sync` over the real paths.
   If that fails, it aborts **before** the EEPROM write.
7. Runs COBB's `writeeeprom --state=0 --vehicle=?`, restores clean `mapnames`,
   syncs, and reboots.
8. Waits for the unit to come back (~1–2 minutes).
9. Re-reads the EEPROM and verifies the change.

Success looks like:

```
[toggle] back online (v1.7.4.2-24844). Device now reports: Not installed / (none)
[toggle] verifying live EEPROM (~1 min) ...
[toggle] verified: AP_STATE=0
```

If the unit has a TCM pairing, the tool also clears the TCM state. If the
capability probe says TCM support exists but `readeeprom` does not report
`AP_CM1_STATE`, the tool refuses to write rather than guess whether a TCM is
married. The post-reboot verify checks the TCM state too.

Total time is usually **4–6 minutes** per toggle.

### Step 7 — re-marry (menu 4)

Type `4`. The tool uses the vehicle code from `units/<serial>/manifest.json`
(falling back to the backup file, then asking you). If the EEPROM snapshot
recorded a married TCM (`AP_CM1_STATE=1`) and a TCM vehicle code, it restores
that too. A TCM code without a married snapshot is not written.

Confirm with `YES`. Same staging, dry run, write, reboot, and verify sequence.
Success looks like:

```
[toggle] verified: AP_STATE=1 AP_VEHICLE=SUBA_US_WRXM_10
```

If you are re-marrying to a different vehicle, type that vehicle's code when
asked. Only use a code that is valid for that unit.

### Step 8 — quit (menu 5)

Type `5`. The next write will re-read the EEPROM, so you can also quit and
re-run later.

### Step 9 — restore a left-over payload (only if needed)

If a previous run failed between the write and the cleanup, `mapnames` can still
contain a command payload. The tool detects this at startup and stops before its
probe (which would evaluate it). Restore a known-clean copy without triggering
the diagnostic task:

```sh
python3 tools/cobbtool.py --restore-mapnames units/<serial>/ap-data/mapnames
```

Exit `0` means the read-back matched. Exit `1` means no unit was found, or the
upload was sent but could not be verified (including an empty read-back), so run
status before any write. Exit `2` means the file was rejected (empty, a payload,
or from another unit's directory).

For the validated SUB-003, `backup/ap-data/mapnames` is a known-clean copy.
Do **not** use a `mapnames` from a different unit or vehicle.

Then start the tool normally.

---

## 5. What the tool checks before writing

Every toggle goes through these gates, in order:

1. **Capability preflight** — paths, binaries, flags, and a working eval path.
2. **Live EEPROM read** — refuses to write blind if `readeeprom` returns nothing.
3. **Known state** — only `-1` through `5` are accepted.
4. **Verified backup** for unmarry — matching ownership/firmware and file hashes;
   legacy or fallback backups require a fresh backup before writing.
5. **Recoverable vehicle codes** — requires a code for each married ECU/TCM.
   An unknown TCM state or missing married TCM code blocks the operation.
6. **Explicit confirmation** — type `YES`.
7. **Staged clean `mapnames` read back** — aborts if the stage did not land.
8. **Cleanup dry run** — `mount`/`cp`/`cmp`/`sync` over the real paths; aborts before
   the EEPROM write if the restore path is broken.
9. **Write + restore + sync + reboot** in one payload. Cleanup runs even if the
   EEPROM command fails; reboot requires both a successful write and verified cleanup.
10. **Reconnect to the original serial** and **post-reboot EEPROM verification**.
    Initial selection requires one connected unit and the supported bulk USB layout.

The write itself is COBB's own `writeeeprom` binary. The tool does not invent an
EEPROM encoding.

---

## 6. Errors and what to do

### Startup and USB

**`No AccessPort found (USB VID 0x1A84).`**

The computer cannot see the unit.

- Reseat the cable; try another port and a known data cable.
- Make sure the AP is on and finished booting.
- Close Accessport Manager.
- Windows: confirm the WinUSB driver is bound to VID `1A84` (Zadig).
- Linux: check USB permissions.
- If a previous command left the endpoint wedged, try
  `python3 tools/carscan.py reset`, then replug.

**`USB error: ...`**

The libusb backend is missing or the device is claimed.

- macOS: `brew install libusb`.
- Windows: `python -m pip install pyusb libusb`.
- Linux: install libusb 1.0 and check permissions.
- Close other software using the AP.

**`Could not read this unit's mapnames; cannot proceed safely.`**

The tool cannot read the restore reference, so it will not write.

- Replug, wait for boot, retry.
- Run `python3 tools/carscan.py reset` if the endpoint is wedged.
- If it persists, stop. Do not use a different tool to force a write.

**`This unit's mapnames already contains a command payload.`**

A previous run left a payload behind. The tool stops before the probe.

- Restore a clean copy with `--restore-mapnames` (see [Recovery](#7-recovery)).
- Do not run the normal menu first; the probe would evaluate the payload.

### Capability preflight

**`Capability preflight FAILED. Missing: ...`**

The unit does not expose the chain the tool relies on. The missing item is
listed (for example `writeeeprom --state`, `diagnostic_file`, or `/user/ap-data`).

- Status and backup still work.
- Do not attempt unmarry/re-marry on this unit with this tool.
- Record the part number, firmware, and the missing list; that is the evidence
  needed to decide whether the unit is supportable.

**`mapnames-in-diagnostic=no`** (in the capabilities line)

The grep did not find the name in the diagnostic script, but the payload ran.
This is informational on its own; the probe still passed or failed on the
required checks.

### Reads and state

**`Could not read live EEPROM; refusing to write blind.`**

`readeeprom` returned nothing usable.

- Replug and retry.
- Check that the unit still shows its normal screen.
- Do not toggle without a live read; the tool will not let you.

**`Unrecognised AP_STATE=...`**

The EEPROM state is outside the values this tool knows (`-1` to `5`).

- Do not write. Record the value and the part/firmware.
- The unit may be in a firmware state this tool was not designed for.

**`No backup on file for this unit. Back it up first (menu 2) so re-marry is possible.`**

Unmarry is blocked until a backup exists.

- Run menu 2 while the unit is still installed.
- If the unit is already uninstalled, you can still back it up, but the vehicle
  code may be missing; be ready to type it.

**`No usable AP_VEHICLE in the EEPROM. Enter this unit's vehicle code ...`**

The unit has no recoverable code. Without one, unmarry is not reversible.

- Type the code and keep it; it is stored for re-marry.
- Leave it blank to abort.
- Do not guess. Find the code in the backup, on the unit's records, or from the
  vehicle/COBB account.

**`Invalid vehicle code '...'. Aborting.`**

The code contains characters the tool will not put on a command line, or it is
`?`/empty.

- Use the unit's real code (letters, digits, `_`, `.`, `-`).
- Do not include quotes, spaces, `;`, `$`, or backticks.

### During the write

**`The write was not started (staging or cleanup gate failed). ...`**

The staged clean `mapnames` did not read back, or the cleanup dry run failed.
If the failure was before the trigger, the EEPROM was not written.

- Replug and retry.
- If it repeats, stop: the unit's restore path or task trigger is not behaving
  like the tested unit. Do not bypass the check.
- If the unit is reachable, run status and a deep EEPROM read before retrying.

**Trigger write errored**

The trigger frame did not get an ACK, so the tool cannot know whether the unit
received it. It restores clean `mapnames` best-effort and waits for a reboot.

- If the unit reboots, the tool verifies the EEPROM as usual.
- If it stays up, the tool reconnects and verifies/disarms mapnames. It exits
  nonzero if it cannot prove the restore.

**`Device did not come back in time. Check the screen; it may still be booting.`**

The write and reboot were fired, but the tool did not see the unit re-enumerate.

- The tool requires the unit to disappear once before accepting a reconnect,
  so it will not mistake the pre-reboot device for the new one.
- If the unit is still reachable, the tool tries to restore clean `mapnames`
  (disarm a missed trigger). Check status before retrying.
- If the unit is not reachable, the tool exits nonzero. Do not power-cycle;
  reconnect, then check status. The startup guard catches a leftover payload.
- Wait another minute or two; watch the screen.
- If it is up, run the tool again and use menu 1 (status) and the deep EEPROM
  read. Do not immediately toggle again.
- If it never comes up, see [Recovery](#7-recovery).

**`Could not verify: readeeprom returned nothing.`**

The unit came back but the verification read failed. The write may or may not
have taken.

- Re-run the tool and check status plus the deep EEPROM read.
- Back up again before any further write.

**`VERIFY FAILED: the EEPROM does not show the change we just wrote.`**

The write did not take, or something reverted it.

- Do not assume the operation worked.
- Re-run status and the deep EEPROM read; back up again.
- Do not loop the toggle. Record the output and investigate first.

### Backups

**`[backup] WARNING: at least one directory could not be listed ...`**

The device would not give a directory listing, so the tool fell back to names
known from an AP3-SUB-003. The backup may be incomplete.

- Retry the backup.
- Do not rely on this backup for re-marry on a different SKU.

**`[backup] WARNING: no live EEPROM snapshot ...`**

The probe did not return EEPROM fields. Re-marry will fall back to the backup
file or ask for the code.

- Retry the probe/backup.
- If it persists, treat the unit as read-only and investigate before unmarry.

---

## 7. Recovery

### The unit boots and the tool connects

If the startup guard reports a left-over payload:

```sh
python3 tools/cobbtool.py --restore-mapnames units/<serial>/ap-data/mapnames
```

If that backup file is missing, use `backup/ap-data/mapnames` for the validated
SUB-003. Use only a copy from the same unit/vehicle. Then start the tool
normally and check status.

If the guard is clean but the previous verify failed, re-run status and the
deep EEPROM read. Back up again before any write. Do not toggle repeatedly.

### The unit boots but USB does not see it

- Replug; try another cable and port.
- Close Accessport Manager.
- `python3 tools/carscan.py reset`, then retry.
- Windows: re-check the WinUSB binding.

### The unit does not reach its menu

This is not the `mapnames` file. `mapnames` is evaluated by the diagnostic task,
not by `init` at boot; a leftover payload is a latent risk, not a boot loop. A
unit that will not reach its menu after a write is most likely reacting to the
EEPROM contents or a partial write.

- Power-cycle once and watch the screen.
- If it still does not reach a usable state, try COBB Accessport Manager /
  the vendor's firmware update path **if the unit enumerates**.
- If it does not enumerate, COBB support / RMA is the fallback.
- The car is not affected: this tool never writes the car ECU.

### The unit has no power

The tool does not write the bootloader or rootfs. Check the cable, port, and
power source first.

### Is it bricked?

Not in the "bootloader is gone" sense. The tool writes:
- the AP's EEPROM state/vehicle through COBB's own `writeeeprom`, and
- the AP's `mapnames` file.

It does not write rootfs, the bootloader, or the car. The realistic bad outcome
is "powers on but the app does not reach a usable state." Whether AP Manager can
recover that depends on the unit enumerating; that is not established for every
firmware.

---

## 8. Files and backups

Each unit gets a directory:

```
units/
  SUB0000001/
    manifest.json
    mapnames
    backup
    settings
    ...
```

`manifest.json` records:

- serial, part, firmware
- the state/vehicle reported at backup time
- the vehicle code found in the `backup` file
- the **live EEPROM snapshot** (`AP_STATE`, `AP_VEHICLE`, and TCM fields)
- the saved file list, byte sizes, SHA-256 hashes, completeness flag, and timestamp

Re-marry reads the EEPROM snapshot first, then the backup file's vehicle code,
then prompts.

Do not edit `manifest.json` unless you know what you are doing. Do not delete
`units/<serial>/ap-data/mapnames`; it is the clean restore copy for that unit.

---

## 9. Limits

- **Only one validated unit.** `AP3-SUB-003` / `v1.7.4.2-24844`. Every other
  AP3 is probe-based and experimental.
- **Flags are not semantics.** The probe proves `--state`/`--vehicle` exist. It
  cannot prove a different firmware encodes them the same way.
- **No car ECU work.** This clears the AP's local pairing record. It does not
  flash the car back to stock. If the car still has a COBB tune, it still has it.
- **No cloud change.** COBB's licensing record is separate.
- **TCM re-marry depends on the captured code.** If the unit had a TCM and the
  code was not captured, re-marry cannot restore the TCM pairing.
- **Runtime.** The startup probe, each live EEPROM read, and each verification
  take about a minute. A toggle is typically 4–6 minutes.
- **USB write primitive is reconstructed.** It is reliable for the small writes
  this tool performs, but it is not a vendor-documented API.
- **Do not unplug during a probe, write, or reboot.**
