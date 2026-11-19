# Troubleshooting OVMS Manager

Fixes for problems specific to OVMS Manager. Problems common to every [Monad](https://github.com/VarCode-ai/monad) app (installation, Qt
not starting, the NPU, non-Intel GPUs) are in the
[Monad troubleshooting guide](https://github.com/VarCode-ai/monad/blob/HEAD/docs/troubleshooting.md), and quick tips are in the
[README](../README.md#troubleshooting).

## Installation

### `No module named pip`

An existing or interrupted Python environment may be missing pip. Run the updated `./install.sh`
again: it checks pip even in reused environments and restores it with `ensurepip`.
To repair an environment created by an older installer manually:

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/ovms-manager/venv/bin/python" -m ensurepip --upgrade
./install.sh
```

If `ensurepip` is unavailable, install your distribution's venv/ensurepip package for the selected
Python. If dependency installation then reports unavailable wheels for your Python version, use
another installed interpreter, for example `PYTHON=python3.12 ./install.sh`.

### Disk usage without models

The installer prints the size of the Python environments and the installed application in GiB.
To measure an existing installation (including the optional Converter environment):

```bash
du -sk "${XDG_DATA_HOME:-$HOME/.local/share}/ovms-manager" | awk '{printf "%.2f GiB\n", $1 / 1048576}'
```

This excludes models in `~/models`, Podman/Docker container images and the pip download cache,
which are stored separately. The base environment installs PySide6 and requests; `--with-optimum`
adds a separate environment with optimum-intel, OpenVINO and PyTorch CPU, requiring a few more GB.

## Containers

### Docker: `permission denied while trying to connect to the Docker daemon socket`

Your user may not use Docker. Either add it to the `docker` group (this grants root-equivalent access)
and log in again, or use rootless Docker or Podman:

```bash
sudo usermod -aG docker "$USER"
```

### GPU not available inside the container with rootless Podman

On some distributions the render node is only accessible through a supplementary group, which rootless
containers drop by default. Keep your groups in the container:

```bash
OVMS_EXTRA_RUN_ARGS="--group-add keep-groups" ovms-manager
```

To make it permanent, add `export OVMS_EXTRA_RUN_ARGS="--group-add keep-groups"` to `~/.profile` and log
in again: the menu entry inherits the variables of your session.

### NPU missing from the OVMS device list on Fedora

If you selected the NPU but the OVMS log lists only CPU and GPU, check the audit log on the host:

```bash
sudo ausearch -m AVC -ts recent -i | grep accel0
```

If the entry shows that SELinux denied `ovms` read or write access to `accel0`,
enable device access for containers and save the setting across reboots:

```bash
sudo setsebool -P container_use_devices on
```

Restart the server from OVMS Manager. Look for `NPU` in the available devices line and run a model request.
The setting applies to all containers on the host. If the previous setting was off, restore it with
`sudo setsebool -P container_use_devices off`.

### SELinux denials

The models folder is mounted with the shared SELinux label (`:z`), which works on Fedora and RHEL. If you
moved `OPENVINO_MODELS_DIR` to a location with a special label (another disk, an NFS share), relabel it
or check `sudo ausearch -m avc -ts recent`.

### Files in `~/models` that you cannot delete

With rootless Podman, files written by the container belong to a sub-UID of your user. The **Delete**
button of the Local library page handles this. From a terminal use:

```bash
podman unshare rm -rf ~/models/<folder>
```

## Getting help

When you open an [issue](https://github.com/VarCode-ai/ovms-manager/issues), include your distribution and
version, the output of the installer, the console at the bottom of the window, and the **Container log**.
