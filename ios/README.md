# ios

**Empty by design.** See [ADR-001](../docs/decisions/ADR-001-no-iphone-app-v1.md).

v1 needs no iPhone app: the NotePin S has its own Wi-Fi and reaches Plaud Cloud
without a phone, and Forge pulls from there with the official CLI.

An app is possible — Plaud ships an official Embedded iOS SDK, and a custom app
can be the BLE peer via `PlaudDeviceAgent.connectBleDevice / getFileList /
exportAudio`. It cannot eavesdrop on the official app's sync; no iOS API allows
that.

## Build this only if a trigger fires

1. Plaud Cloud is ruled out for the audio being recorded (SPEC T-14), or
2. Both Wi-Fi and BLE sync prove too slow (SPEC T-33, T-34).

Trigger 1 is answerable today without hardware, and is the likely one.

## What it would cost

Apple Developer membership ($99/yr), a Mac or a Mac-free CI path, and
**unbinding the pin from the official Plaud app** — Plaud devices bind to one
application at a time. The custom app replaces the official one rather than
supplementing it, forfeiting its sync, firmware updates, and Wi-Fi setup UI.

Full task breakdown in SPEC §5, W8.
