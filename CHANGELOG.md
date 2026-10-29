# Changelog

All notable changes to OVMS Manager are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

OVMS Manager is a [Monad](https://github.com/VarCode-ai/monad) app. Until 1.0.0 it lived in the Monad
repository, then named `ai-app-local-openvino`, together with Lectern; its history was kept when it moved here.

## [Unreleased]

## [1.1.1] — 2026-10-29

### Fixed

- Downloads with rootless Podman failed with "failed to make directory '/models/OpenVINO': Permission denied"
  when `~/models` was not writable by everyone (the usual case, e.g. a folder created with the default 755
  permissions). The download container now runs as your user, like the config.json updates already did, so
  it can write in `~/models` and the downloaded files belong to you.

## [1.1.0] — 2026-10-27

### Monad

- OVMS Manager has its own repository, as one of the apps of [Monad](https://github.com/VarCode-ai/monad).
  The shared UI kit is in `common/`, and the update check now looks at the releases of this repository.
- New icon in the style of the Monad family, and the sidebar says which family it belongs to.

### Downloads

- AppImage: one file with Python, Qt and OpenVINO inside, for any x86-64 distribution with glibc 2.34 or
  newer. It does not include the Converter: use the installer with `--with-optimum` for it.
- Archive for the installer: `OVMS-Manager-<version>.tar.gz` unpacks into a folder ready for `./install.sh`,
  for those who prefer an installed app without cloning the repository.
- A *Release* workflow builds both for every tag and publishes them in the GitHub release, with
  `SHA256SUMS` and the notes of the version from this changelog.

### Privacy

- The Converter switches off the optional usage statistics of Hugging Face downloads
  (`HF_HUB_DISABLE_TELEMETRY`, `DO_NOT_TRACK`) and declines those of OpenVINO's model conversion.

## [1.0.0] — 2026-10-23

First public release.

- A desktop front end for OpenVINO Model Server:
  - Browse the OpenVINO models on Hugging Face and download them.
  - Serve them through an OpenAI-compatible API on the Intel CPU, GPU or NPU.
  - Convert other models with Optimum.
  - Runs the server with Podman or Docker.
- Light and dark themes that follow the system, command palette (Ctrl+K), bundled Inter font, RAM fit
  indicator, WCAG 2.2 AA contrast and a *Reduce motion* option.
- English and Italian interface, per-user installer for every major glibc distribution, automatic fallback
  from GPU or NPU to the CPU, and update notifications that can be turned off.

[Unreleased]: https://github.com/VarCode-ai/ovms-manager/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/VarCode-ai/ovms-manager/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/VarCode-ai/ovms-manager/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/VarCode-ai/ovms-manager/releases/tag/v1.0.0
