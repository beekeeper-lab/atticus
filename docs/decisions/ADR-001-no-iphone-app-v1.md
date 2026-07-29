# ADR-001 — No iPhone app in v1

**Status:** Accepted
**Date:** 2026-07-28

## Context

The original framing assumed an iPhone app was the necessary bridge between the
NotePin S and a git repo — that a background app would need to detect the BLE
sync and capture the audio file.

Two findings invalidate that assumption.

**1. An app cannot intercept another app's BLE sync.** iOS CoreBluetooth has no
promiscuous or sniffing mode; an app sees only GATT connections it establishes
itself. The Plaud app's downloaded audio sits in its sandbox container. This is
not a permission that can be requested — there is no API surface for it.

**2. The NotePin S does not need a phone.** It has a 2.4 GHz Wi-Fi radio. When
charging and idle it connects to a configured network and uploads recordings
directly to Plaud Cloud. Combined with the official `@plaud-ai/cli`, which
returns a 24-hour presigned URL for the original audio, Forge can retrieve
recordings with the phone entirely absent from the data path.

A custom app *is* possible via Plaud's official Embedded iOS SDK
(`PlaudDeviceAgent.connectBleDevice / getFileList / exportAudio`) — the app
becomes the BLE peer rather than an eavesdropper. So the question is not
feasibility but whether it earns its cost.

## Decision

**No iPhone app in v1.** Audio reaches Forge via Plaud Cloud. `ios/` stays
empty.

## Rationale

The app buys nothing v1 requires:

- **Latency:** it would reduce minutes to seconds. For "research this and write
  it up," minutes are irrelevant.
- **Reliability:** two independent transports (Wi-Fi-while-charging and
  BLE-to-app) already cover each other.
- **Control:** the CLI is an official, OAuth-authenticated, supported surface.

And it costs real things:

- A Mac, or a Mac-free CI path that is workable for building and miserable for
  BLE debugging.
- $99/yr Apple Developer membership.
- **Unbinding the pin from the official Plaud app.** Plaud devices bind to one
  application at a time. The custom app would replace it, forfeiting the
  official app's sync, firmware updates, and Wi-Fi configuration UI.
- Ongoing maintenance against an SDK in beta.

That last cost is the sharp one: building the app doesn't add a path, it
*replaces* the working ones.

## Trigger conditions

Revisit only if either fires:

1. **T-14 rules out Plaud Cloud.** If the audio you intend to record cannot
   transit Plaud's servers, every zero-touch path in v1 is disqualified and the
   app becomes the only design that works. This is the likely trigger.
2. **T-33 and T-34 both show unacceptable latency.** If Wi-Fi sync only fires on
   an infrequent charge cycle *and* BLE sync dies when the app is suspended,
   the pipeline may be too slow to be useful.

Note that trigger 1 is a judgment call available today — it does not need
hardware. Answer it early; it is the difference between a week of Linux work
and a Swift project.

## Consequences

- v1 is Linux-only and Mac-free.
- Plaud Cloud becomes a hard dependency and a single point of failure.
- Recorded audio transits a third party. Accepted for personal use; explicitly
  reopened by trigger 1 for client or privileged content.
- W8 in the spec preserves the app design in enough detail to start quickly if
  a trigger fires.
