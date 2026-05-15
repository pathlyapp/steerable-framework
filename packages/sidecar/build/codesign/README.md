# Sidecar code signing

The portable Python runtime shipped under `packages/sidecar/dist/python-runtime/`
contains real Mach-O / PE binaries (the CPython interpreter, dylib bindings, and
all wheels with native extensions). They MUST be signed alongside the host
application before distribution:

* macOS — every `*.dylib`, `*.so`, and the `python3` Mach-O binary need an
  Apple Developer ID Application signature with a hardened runtime, then
  bundled into the host app and notarized via `notarytool`.
* Windows — every `*.exe`, `*.dll`, and `*.pyd` should be signed with an EV
  authenticode certificate via `signtool`.

The host application (`deeppath-agent`) MUST consume the signed bundles —
re-signing during electron-builder pack is the simplest invariant.

## Helper scripts

| Script | Platform | Purpose |
| --- | --- | --- |
| `sign_macos.sh` | macOS | codesign every Mach-O / dylib / so under a runtime dir, then verify. |
| `sign_windows.ps1` | Windows | signtool every exe / dll / pyd under a runtime dir. |
| `notarize_macos.sh` | macOS | submit a stapled zip to notarytool. |

All scripts are non-interactive and accept paths via env / CLI so they can be
called from CI matrix jobs.

## CI workflow

`.github/workflows/sidecar-codesign.yml` orchestrates:

1. Run after `sidecar-build.yml` finishes successfully.
2. Download the per-platform artifact.
3. Decrypt secrets (`APPLE_*`, `WINDOWS_PFX_BASE64`, `WINDOWS_PFX_PASSWORD`).
4. Run the platform-specific signer.
5. Upload the **signed** runtime as a new artifact for the desktop app to
   consume.
6. macOS only: also run `notarize_macos.sh` and staple the result.

Secrets required (configure in GitHub → Settings → Secrets):

* `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD`
* `MACOS_CERT_BASE64`, `MACOS_CERT_PASSWORD`
* `WINDOWS_PFX_BASE64`, `WINDOWS_PFX_PASSWORD`
