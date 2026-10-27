# Troubleshooting OVMS Manager

Fixes for problems specific to OVMS Manager. Problems common to every [Monad](https://github.com/VarCode-ai/monad) app (installation, Qt
not starting, the NPU, non-Intel GPUs) are in the
[Monad troubleshooting guide](https://github.com/VarCode-ai/monad/blob/HEAD/docs/troubleshooting.md), and quick tips are in the
[README](../README.md#troubleshooting).

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
