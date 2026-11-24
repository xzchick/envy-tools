# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/xzchick/envy-tools/security/advisories/new)
rather than a public issue. You'll get an acknowledgement, and a fix or an explanation, as
soon as practical.

## Design notes

- **Where it downloads from:** HTTPS only, and only GitHub hosts, checked again on every redirect.
  The app list read from this repository is validated entry by entry, and it can only name
  repositories owned by `xzchick`. A tampered list can't point downloads anywhere else.
- **Integrity:** every bundle's SHA-256 must match the release's `SHA256SUMS.txt` before it is
  unpacked. If `cosign` is present, the Sigstore signature on that file is verified against the
  app's own `release.yml` workflow identity. `--require-signature` makes that mandatory.
- **Unpacking:** absolute paths, `..` components, device files and links that resolve outside
  the destination are refused. On Python versions that have it, tar's `data` filter is applied
  as well.
- **Atomic installs:** downloads and unpacking happen in a temporary folder, and the app is
  moved into place only after every check passes.
- **No elevation:** everything is installed per user. ENVY Tools never asks for administrator
  rights.
- **Launchers:** values reach PowerShell (Windows shortcuts) through environment variables, not
  by building a script string. `.desktop` and macOS launcher scripts quote the path.
