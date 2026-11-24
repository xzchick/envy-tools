# Changelog

All notable changes to this project. This project adheres to semantic versioning.

## Unreleased

- Short names: `install integrity`, `crypto` or `"ops desk"` work. Exact names still win
  (`vault` is Vault, not Evidence Vault), and an ambiguous name lists the choices

## v0.1.0 — 2026-11-24

- Initial release: find, install, update, open and remove every ENVY app on macOS, Windows
  and Linux, from a native window or the `envy-tools` command
- Installs each app's standalone release bundle for this OS and CPU, verified against the
  release's SHA-256 checksums (and its Sigstore signature when `cosign` is installed)
- Launchers: an app in ~/Applications (macOS), a Start Menu shortcut (Windows), an app-menu
  entry (Linux), plus a command on macOS and Linux
- The app list updates itself from this repository, with a built-in copy for offline use
- Standalone bundles for macOS, Windows and Linux; Sigstore-signed checksums and a CycloneDX SBOM
