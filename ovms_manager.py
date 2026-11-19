#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""OVMS Manager — a desktop front end for OpenVINO Model Server, the Hugging Face OpenVINO catalog and Optimum."""
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from PySide6.QtCore import (QAbstractTableModel, QLibraryInfo, QLocale, QModelIndex, QPropertyAnimation, QRectF, QSize,
                            QSortFilterProxyModel, Qt, QThread, QTimer, QTranslator, QUrl, Signal, Property,
                            QEasingCurve)
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
                               QTableView, QVBoxLayout, QWidget, QListWidget, QListWidgetItem, QDialog,
                               QScrollArea)

APP_ID = "ovms-manager"  # = name of the .desktop file created by install.sh
APP_VERSION = "1.1.1"    # compared with the latest GitHub release for update notifications
APP_REPO = "VarCode-ai/ovms-manager"  # GitHub repository whose releases are checked for updates
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(APP_DIR, "ui_kit.py")):  # running from the repository: the kit is in ./common
    sys.path.insert(0, os.path.join(APP_DIR, "common"))
import ui_kit as kit  # noqa: E402  (shared UI kit of the Monad apps: themes, fonts, icons, command palette)
# Folder holding the OpenVINO models (shared with Lectern). Override with $OPENVINO_MODELS_DIR.
MODELS_DIR = os.path.abspath(os.path.expanduser(os.environ.get("OPENVINO_MODELS_DIR") or "~/models"))
_HOME = os.path.expanduser("~")
# shown in the UI with the home folder abbreviated to ~
MODELS_LABEL = "~" + MODELS_DIR[len(_HOME):] if MODELS_DIR.startswith(_HOME + os.sep) else MODELS_DIR
DATA_DIR = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ovms_manager")
CACHE_FILE = os.path.join(CACHE_DIR, "hf_catalog.json")
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
CACHE_TTL = 6 * 3600
HF_API = "https://huggingface.co/api/models"
HF_EXPAND = ["downloads", "likes", "lastModified", "pipeline_tag", "tags", "library_name"]

OVMS_IMAGE = "docker.io/openvino/model_server:latest-gpu"
CONTAINER_NAME = "ovms-server"
FALLBACK_MODEL_NAME = "ov_model"   # served name when the folder name gives nothing usable
OVMS_PORT = 8000
# 127.0.0.1 and not "localhost": localhost resolves to ::1 (IPv6) first, and rootless podman's port
# forwarding resets IPv6 connections ("connection reset"), while IPv4 works.
OVMS_HOST = "127.0.0.1"
OVMS_URL = f"http://{OVMS_HOST}:{OVMS_PORT}"
OPEN_WEBUI_URL = os.environ.get("OPEN_WEBUI_URL") or "http://localhost:3000"


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------
# User-visible text is written in English and translated through locale/<language>.json, a plain
# {"English text": "translation"} map. N_() only marks text defined before the language is known; it is
# translated with tr() where it is shown. tools/check_translations.py checks that the catalogs are complete.
def N_(text):
    return text


_catalog = {}


def tr(text):
    return _catalog.get(text, text)


def setup_language(app, choice):
    """Load the catalog for `choice` ('' = system language) and Qt's own translations (standard buttons and
    dialogs). Return the language code in use."""
    global _catalog
    lang = choice or QLocale.system().name().split("_")[0]
    try:
        with open(os.path.join(APP_DIR, "locale", f"{lang}.json"), encoding="utf-8") as f:
            _catalog = json.load(f)
    except (OSError, ValueError):
        _catalog, lang = {}, "en"
    QLocale.setDefault(QLocale(lang) if choice else QLocale.system())
    qt = QTranslator(app)
    if lang != "en" and qt.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
    return lang


UI_LANGUAGES = [("", N_("System language")), ("en", "English"), ("it", "Italiano")]


# ---------------------------------------------------------------------------
# Container runtime: Podman or Docker
# ---------------------------------------------------------------------------
def detect_runtime():
    """'podman' or 'docker' ($OVMS_CONTAINER_RUNTIME wins, then Podman, then Docker); None if neither."""
    forced = os.environ.get("OVMS_CONTAINER_RUNTIME", "").strip()
    for name in ([forced] if forced else ["podman", "docker"]):
        if shutil.which(name):
            return name
    return None


RUNTIME = detect_runtime()
RUNTIME_NAME = (RUNTIME or "podman").capitalize()
# Hostname under which a containerized Open WebUI reaches this machine. With Docker on Linux, Open WebUI
# must be started with --add-host=host.docker.internal:host-gateway (as its own docs suggest).
HOST_ALIAS = "host.docker.internal" if RUNTIME == "docker" else "host.containers.internal"
CONTAINER_URL = f"http://{HOST_ALIAS}:{OVMS_PORT}/v3"
# Extra `run` options for every OVMS container, e.g. "--group-add keep-groups" for GPU access with rootless
# Podman on distributions where the render node is only accessible through a supplementary group
EXTRA_RUN_ARGS = shlex.split(os.environ.get("OVMS_EXTRA_RUN_ARGS", ""))
RUNTIME_HINT = N_("Neither Podman nor Docker was found. Install one of them, e.g.  sudo apt install podman  ·  "
                   "sudo dnf install podman  ·  sudo pacman -S podman  ·  sudo zypper install podman")


def runtime_cmd(*args):
    return [RUNTIME or "podman", *args]


def device_groups(flags):
    """--group-add options for the groups owning the device nodes in `flags` (render, video, accel…).
    Needed with Docker, where the container runs with the user's uid but not with its groups."""
    gids = set()
    for i, flag in enumerate(flags[:-1]):
        if flag != "--device":
            continue
        node = flags[i + 1].split(":")[0]
        for path in (glob.glob(os.path.join(node, "*")) if os.path.isdir(node) else [node]):
            try:
                gids.add(os.stat(path).st_gid)
            except OSError:
                pass
    return [x for gid in sorted(gids) for x in ("--group-add", str(gid))]


def container_run(*args, devices=(), detach=False, name=None, ports=(), as_owner=False, deployment=None):
    """`<runtime> run` command for the OVMS image with ~/models mounted on /models.
    Volumes use the shared SELinux label (:z): with the private one (:Z) every new container would
    relabel ~/models and take access away from the server container that is already running.
    With Docker the container runs as the current user, so the files it writes in ~/models stay yours
    (rootless Podman already maps them to you)."""
    cmd = runtime_cmd("run", "-d" if detach else "--rm")
    if name:
        cmd += ["--name", name]
    for port in ports:
        cmd += ["-p", port]
    cmd += ["-v", f"{MODELS_DIR}:/models:z", *devices]
    if deployment is not None:
        cmd += ["-v", f"{deployment}:/deployment:z"]
    if RUNTIME == "docker":
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}", *device_groups(list(devices))]
    elif as_owner:
        # rootless Podman: root in the container is your user on the host, so it can write in ~/models and
        # rewrite config.json whoever created them (you, or the container user of an earlier run). The image's
        # own user (ovms, uid 5000) is a stranger on the host: with a ~/models that is not world-writable
        # (the usual 755) it cannot even create ~/models/OpenVINO, and downloads fail with Permission denied
        cmd += ["--user", "0:0"]
    return cmd + EXTRA_RUN_ARGS + [OVMS_IMAGE, *args]


# ---------------------------------------------------------------------------
# Optimum (model conversion)
# ---------------------------------------------------------------------------
# Runs optimum-cli removing from sys.path the entries that point to the same folder. In Fedora's virtual
# environments lib64/ is a link to lib/ but both end up in sys.path: optimum finds
# optimum/commands/register/register_openvino.py twice and fails with "conflicting subparser: openvino".
OPTIMUM_WRAPPER = ("import os,sys;seen=set();"
                   "sys.path[:]=[p for p in sys.path if not((r:=os.path.realpath(p)) in seen or seen.add(r))];"
                   "from optimum.commands.optimum_cli import main;sys.argv=['optimum-cli',*sys.argv[1:]];main()")
OPTIMUM_CANDIDATES = (
    os.path.join(os.path.dirname(sys.executable), "optimum-cli"),               # same environment as the app
    os.path.join(DATA_DIR, APP_ID, "optimum-venv", "bin", "optimum-cli"),       # install.sh --with-optimum
)


def find_optimum():
    return next((p for p in OPTIMUM_CANDIDATES if os.access(p, os.X_OK)), None) or shutil.which("optimum-cli")


def optimum_command(args):
    """Command running `optimum-cli <args>` with the Python of its environment and the sys.path fix."""
    optimum_bin = find_optimum()
    if not optimum_bin:
        return ["optimum-cli", *args]  # fails with "command not found", shown in the console
    # in a virtual environment the right Python sits next to optimum-cli (more reliable than the shebang,
    # which pip replaces with "#!/bin/sh" when the path is very long)
    bin_dir = os.path.dirname(optimum_bin)
    for name in ("python", "python3"):
        python = os.path.join(bin_dir, name)
        if os.access(python, os.X_OK):
            return [python, "-c", OPTIMUM_WRAPPER, *args]
    return [optimum_bin, *args]


NAV_PAGES = (N_("Server"), N_("Explore Hugging Face"), N_("Local library"), N_("Converter"))

# The converter runs third-party tools with optional usage statistics: switch them off. Hugging Face honors
# HF_HUB_DISABLE_TELEMETRY / DO_NOT_TRACK; OpenVINO's model conversion sends anonymous statistics unless the user
# opted out or it runs in CI (openvino_telemetry's disable_in_ci), so CI=true declines them for this process only.
NO_TELEMETRY_ENV = {**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "CI": "true"}

HARDWARE_MODES = {
    "GPU": {
        "title": N_("Intel GPU"), "icon": "gpu", "desc": N_("Fastest generation"),
        "device": "GPU",
        "nodes": ["/dev/dri"],
        "probe": "gpu",
    },
    "NPU": {
        "title": N_("Intel NPU"), "icon": "spark", "desc": N_("Cool and quiet, ideal on battery"),
        "device": "NPU",
        "nodes": ["/dev/accel:/dev/accel", "/dev/dri"],
        "probe": "npu",
    },
    "MULTI": {
        "title": N_("Hybrid GPU + CPU"), "icon": "layers", "desc": N_("Parallel MULTI throughput"),
        "device": "MULTI:GPU,CPU",
        "nodes": ["/dev/dri"],
        "probe": "gpu",
    },
    "CPU": {
        "title": N_("CPU only"), "icon": "chip", "desc": N_("Works everywhere"),
        "device": "CPU",
        "nodes": [],
        "probe": None,
    },
}


def served_name_for(model_rel_path):
    """Name the model shows up with in Open WebUI: the folder name without the '-ov' suffix.
    e.g. 'OpenVINO/Qwen3.5-4B-int8-ov' -> 'Qwen3.5-4B-int8'"""
    name = os.path.basename(model_rel_path.rstrip("/"))
    name = re.sub(r"-ov$", "", name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or FALLBACK_MODEL_NAME


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def save_state(**values):
    # Settings edits and server starts can come from different workers or windows.
    try:
        with file_lock(Path(CACHE_DIR) / "state.lock"), tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmp:
            state = load_state()
            state.update(values)
            pending = Path(tmp) / "state.json"
            pending.write_text(json.dumps(state))
            pending.replace(STATE_FILE)
    except OSError:
        pass


def device_flags(modes):
    nodes = dict.fromkeys(node for mode in modes for node in HARDWARE_MODES[mode]["nodes"])
    return [arg for node in nodes for arg in ("--device", node)]


def deployment_names(paths):
    """Keep existing API IDs where possible, including when two repositories have the same basename."""
    names = {}
    reserved = {served_name_for(path) for path in paths}
    for path in paths:
        base = served_name_for(path)
        name = base
        suffix = 2
        while name in names.values() or (name != base and name in reserved):
            name = f"{base}-{suffix}"
            suffix += 1
        names[path] = name
    return names


# Text-format protobuf strings may contain JSON, braces and text that looks like another field.
_GRAPH_TOKENS = re.compile(r"""\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[\w.]+|[^\s]""")
_GRAPH_DEVICES = {
    "mediapipe.LLMCalculatorOptions": "device",
    "mediapipe.ImageGenCalculatorOptions": "device",
    "mediapipe.EmbeddingsCalculatorOVOptions": "target_device",
    "mediapipe.RerankCalculatorOVOptions": "target_device",
    "mediapipe.S2tCalculatorOptions": "target_device",
    "mediapipe.T2sCalculatorOptions": "target_device",
}


def graph_with_device(graph, device, *, embedding_max_length=None):
    """Select the device and supply a default embedding length when the graph omits it."""
    tokens = [m for m in _GRAPH_TOKENS.finditer(graph) if not m[0].startswith("#")]
    words = [m[0] for m in tokens]
    edits = []
    for i, word in enumerate(words):
        field = _GRAPH_DEVICES.get(word)
        if not field or words[i + 1:i + 2] != ["]"]:
            continue
        start = i + (3 if words[i + 2:i + 3] == [":"] else 2)
        if start >= len(words) or words[start] not in ("{", "<"):
            continue
        found_device = False
        needs_length = word == "mediapipe.EmbeddingsCalculatorOVOptions" and embedding_max_length is not None
        depth = 1
        for j in range(start + 1, len(tokens)):
            if words[j] in ("}", ">"):
                depth -= 1
                if depth == 0:
                    pos = tokens[start].end()
                    fields = []
                    if not found_device:
                        fields.append(f'{field}: "{device}"')
                    if needs_length:
                        fields.append(f"max_length: {embedding_max_length}")
                    if fields:
                        edits.append((pos, pos, "\n  " + "\n  ".join(fields) + "\n"))
                    break
            elif words[j] in ("{", "<"):
                depth += 1
            elif depth == 1 and words[j:j + 2] == [field, ":"] and j + 2 < len(tokens):
                value = tokens[j + 2]
                edits.append((value.start(), value.end(), f'"{device}"'))
                found_device = True
            elif depth == 1 and words[j:j + 2] == ["max_length", ":"]:
                needs_length = False
    for start, end, value in sorted(edits, reverse=True):
        graph = graph[:start] + value + graph[end:]
    return graph, len(edits)


def prepare_deployment(folder, models, task):
    """Build private runtime files. OVMS sees the weights in /models and these files in /deployment."""
    configs, chat_models = [], []
    names = deployment_names(models)
    for index, (rel, mode) in enumerate(models.items()):
        name = names[rel]
        device = HARDWARE_MODES[mode]["device"]
        task.log(tr("Starting server · {model} on {device} · name: {name}").format(
            model=rel, device=device, name=name), "head")
        source = Path(MODELS_DIR) / rel
        base = Path("/models") / rel
        cfg = {"name": name, "base_path": str(base)}
        graph = source / "graph.pbtxt"
        subconfig = source / "subconfig.json"
        xml = list(source.glob("*.xml"))
        graph_text = ""
        # OVMS uses 1000 tokens in its NPU example for this Qwen embedding model.
        qwen_npu_embeddings = device == "NPU" and source.name.casefold() == "qwen3-embedding-0.6b-int8-ov"
        embedding_max_length = 1000 if qwen_npu_embeddings else None
        if graph.is_file():
            # Graph paths and relative model paths are resolved against the original model directory.
            graph_text, count = graph_with_device(graph.read_text(), device, embedding_max_length=embedding_max_length)
            if not count and not subconfig.is_file():
                raise ValueError(tr("Cannot set the accelerator for the graph in {model}.").format(model=rel))
            graph_file = f"graph-{index}.pbtxt"
            (folder / graph_file).write_text(graph_text)
            cfg["graph_path"] = f"/deployment/{graph_file}"
            if subconfig.is_file():
                # Older graphs delegate inference to classic models defined in their subconfig.
                sub = json.loads(subconfig.read_text())
                for item in sub["model_config_list"]:
                    model = item["config"]
                    model["target_device"] = device
                    model["base_path"] = str(base / model.get("base_path", model["name"]))
                sub_file = f"subconfig-{index}.json"
                (folder / sub_file).write_text(json.dumps(sub))
                cfg["subconfig"] = f"/deployment/{sub_file}"
        elif (xml and (source / "config.json").is_file()) or (source / "model_index.json").is_file():
            # Let OVMS infer the task from local Hugging Face metadata and generate its graph.
            # The temporary model view shares the weights and keeps generated files out of the library.
            view = folder / str(index)
            view.mkdir()
            for entry in source.iterdir():
                if entry.name not in ("graph.pbtxt", "subconfig.json"):
                    (view / entry.name).symlink_to(base / entry.name)
            length_args = ["--max_length", str(embedding_max_length)] if qwen_npu_embeddings else []
            ret = task.run_cmd(container_run("--configure", "--model_path", f"/deployment/{index}",
                                             "--model_name", name, "--target_device", device, *length_args,
                                             deployment=folder, as_owner=True))
            if ret:
                raise ValueError(tr("Could not configure {model} for {device}.").format(model=rel, device=device))
            cfg["base_path"] = f"/deployment/{index}"
            graph_text = (view / "graph.pbtxt").read_text()
        else:
            cfg["target_device"] = device
            if len(xml) == 1:
                cfg["base_path"] = str(base / xml[0].name)
        configs.append({"config": cfg})
        if "LLMCalculator" in graph_text:
            chat_models.append(name)
    (folder / "config.json").write_text(json.dumps({"model_config_list": configs}, indent=2))
    # Rootless Podman serves as the image user. A restrictive host umask must still allow it
    # to read the generated files through the bind mount.
    for root, _, files in os.walk(folder):
        Path(root).chmod(0o755)
        for filename in files:
            path = Path(root) / filename
            if not path.is_symlink():
                path.chmod(0o644)
    return list(names.values()), chat_models


# PCI classes (lspci -n): 03xx = display controller, 1200 = processing accelerator (NPU)
PCI_CLASSES = {"gpu": ("03",), "npu": ("1200",)}
DEVICE_NODES = {"gpu": "/dev/dri", "npu": "/dev/accel"}
INTEL_VENDOR = "8086"


def pci_devices():
    """(vendor, class) of every PCI device, e.g. ('8086', '0300'); None if they cannot be listed."""
    pci = []
    try:
        for dev in os.listdir("/sys/bus/pci/devices"):
            base = f"/sys/bus/pci/devices/{dev}"
            with open(f"{base}/vendor") as fv, open(f"{base}/class") as fc:
                pci.append((fv.read().strip()[2:].lower(), fc.read().strip()[2:6].lower()))
        return pci
    except OSError:
        pass
    try:
        out = subprocess.run(["lspci", "-n"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        m = re.match(r"\S+ ([0-9a-f]{4}): ([0-9a-f]{4}):", line)
        if m:
            pci.append((m.group(2), m.group(1)))
    return pci or None


def selinux_restricts_container_devices():
    selinux = Path("/sys/fs/selinux")
    try:
        return (selinux.joinpath("enforce").read_text().strip() == "1"
                and selinux.joinpath("booleans/container_use_devices").read_text().split()[:1] == ["0"])
    except OSError:
        return False


def detect_accelerators():
    """Detect Intel GPU/NPU. Return {kind: 'ok' | 'hidden' | 'other' | 'missing'}.

    'hidden' = the Intel hardware is on the PCI bus but its /dev node is not visible to this process
    (typical when the app runs in a sandbox such as bwrap/flatpak): the container cannot get it.
    'other' = there is a /dev node but no Intel device of that kind (e.g. an AMD or NVIDIA GPU, which
    OpenVINO does not support).
    """
    pci = pci_devices()
    result = {}
    for kind, prefixes in PCI_CLASSES.items():
        node = DEVICE_NODES[kind]
        has_node = os.path.isdir(node) and bool(os.listdir(node))
        intel = None if pci is None else any(v == INTEL_VENDOR and c.startswith(prefixes) for v, c in pci)
        if has_node:
            result[kind] = "other" if intel is False else "ok"
        else:
            result[kind] = "hidden" if intel else "missing"
    return result


# Category -> (label, task for `ovms --pull`, HF pipeline_tags)
CATEGORIES = {
    "llm": (N_("LLM / Chat"), "text_generation", {"text-generation"}),
    "vlm": (N_("Vision-Language"), "text_generation", {"image-text-to-text"}),
    "embed": (N_("Embeddings"), "embeddings", {"feature-extraction", "sentence-similarity"}),
    "rerank": (N_("Rerank"), "rerank", {"text-ranking"}),
    "image": (N_("Image generation"), "image_generation", {"text-to-image", "image-to-image"}),
    "asr": (N_("Speech-to-Text"), "speech2text", {"automatic-speech-recognition"}),
    "tts": (N_("Text-to-Speech"), "text2speech", {"text-to-speech"}),
    "cv": (N_("Computer Vision"), None, {"image-classification", "object-detection", "image-segmentation"}),
    "other": (N_("Other"), None, set()),
}

QUANT_COLORS = {
    "INT4": "#22c55e", "INT8": "#38bdf8", "FP16": "#a78bfa", "FP32": "#f472b6",
    "NF4": "#facc15", "MXFP4": "#fb923c", "W8A16": "#2dd4bf", "—": "#64748b",
}

# Explanations shown in the guide and in tooltips. Sizes are indicative for a 7-billion-parameter model.
QUANT_INFO = {
    "FP16": (N_("16 bit · ≈ 2 bytes/parameter · 7B ≈ 14 GB"),
             N_("Half-precision floating-point weights: quality practically identical to the original, "
             "but it takes the most memory and is the slowest to generate.")),
    "INT8": (N_("8 bit · ≈ 1 byte/parameter · 7B ≈ 7–8 GB"),
             N_("Weights compressed to 8-bit integers: half the memory of FP16 with almost no quality loss. "
             "A good compromise when memory is enough.")),
    "INT4": (N_("4 bit · ≈ 0.5 bytes/parameter · 7B ≈ 4–5 GB"),
             N_("4-bit weights: a quarter of the memory of FP16 and faster generation on Intel GPUs and NPUs, "
             "because LLMs are limited by memory bandwidth. Slight quality loss, "
             "more noticeable on small models (< 3B).")),
    "OTHER": ("NF4 · MXFP4 · W8A16 · BF16 · FP32",
              N_("NF4/MXFP4: alternative 4-bit formats. W8A16: 8-bit weights, 16-bit compute. "
              "BF16/FP32: full precision, large files. “—”: quantization not stated in the repo name.")),
}
QUANT_VARIANT_INFO = {
    "CW": N_("channel-wise: one scale per channel instead of per group of weights. The variant meant for the NPU."),
    "GQ": N_("group quantization: weights are quantized in groups (e.g. 128) with one scale each. More accurate."),
    "SYM": N_("symmetric: range centered on zero. Simpler and faster to compute."),
    "ASYM": N_("asymmetric: range with an offset. A bit more accurate, a bit more expensive."),
}


def quant_tooltip(quant, variant=""):
    key = quant if quant in QUANT_INFO else "OTHER"
    size, text = QUANT_INFO[key]
    label = tr("Other") if quant == "OTHER" else quant
    tip = f"<b>{label}</b> — {tr(size)}<br>{tr(text)}"
    if variant in QUANT_VARIANT_INFO:
        tip += f"<br><br><b>{variant}</b>: {tr(QUANT_VARIANT_INFO[variant])}"
    return f"<div style='max-width:340px'>{tip}</div>"


# ---------------------------------------------------------------------------
# Palette & style
# ---------------------------------------------------------------------------
ACCENT = "electric"   # OVMS Manager's accent: luminous electric blue
C = kit.T             # tokens of the current theme (light or dark), updated in place by the kit
THEME = None          # kit.ThemeManager, created in main()
THEMES = [("system", N_("System")), ("light", N_("Light")), ("dark", N_("Dark"))]


def app_qss(t):
    """OVMS Manager's own rules on top of the kit's base style. Calm layout: the sidebar is a notch dimmer, cards
    are plain surfaces without heavy borders, the console stays a dark terminal in both themes."""
    r = kit.RADIUS
    return kit.base_qss(t) + f"""
QMainWindow, #root, QDialog {{ background: {t['bg']}; }}
#sidebar {{ background: {t['sidebar']}; border: none; }}
#brand {{ font-size: 16px; font-weight: 750; }}
#brandSub {{ color: {t['muted']}; font-size: 11px; }}
#navList {{ background: transparent; border: none; outline: none; }}
#navList::item {{ padding: 9px 12px; margin: 2px 8px; border-radius: {r['md']}px; color: {t['muted']}; }}
#navList::item:hover {{ background: {t['hover']}; color: {t['text']}; }}
#navList::item:selected {{ background: {t['accent_soft']}; color: {t['accent_text']}; font-weight: 600; }}
#pageTitle {{ font-size: 24px; font-weight: 750; }}
#pageSub {{ color: {t['muted']}; font-size: 13px; }}
#card {{ background: {t['surface']}; border: none; border-radius: {r['xl']}px; }}
#cardTitle {{ font-size: 14px; font-weight: 650; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: {t['border_strong']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; }}
QTableView {{ background: {t['surface']}; border: none; border-radius: {r['xl']}px; gridline-color: transparent;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']};
    alternate-background-color: {t['surface2']}; outline: none; }}
QTableView::item {{ padding: 6px; border: none; }}
QHeaderView::section {{ background: {t['surface']}; color: {t['muted']}; border: none;
    border-bottom: 1px solid {t['border']}; padding: 10px 8px; font-weight: 650; font-size: 11px; }}
QListWidget#lib {{ background: transparent; border: none; outline: none; }}
QListWidget#lib::item {{ background: {t['surface']}; border: 1px solid transparent; border-radius: {r['lg']}px;
    padding: 14px; margin: 3px 0; }}
QListWidget#lib::item:hover {{ border-color: {t['border']}; }}
QListWidget#lib::item:selected {{ border-color: {t['accent']}; background: {t['accent_soft']}; color: {t['text']}; }}
#consoleWrap {{ background: {t['console_bg']}; border-radius: {r['lg']}px; }}
#consoleTitle {{ color: {t['console_text']}; font-weight: 650; }}
#consoleLast {{ color: {t['console_muted']}; }}
QPlainTextEdit#console {{ background: {t['console_bg']}; border: none; color: {t['console_text']};
    font-family: "{kit.FONTS['mono']}"; font-size: 12px; padding: 2px 6px; }}
#consoleWrap QPushButton#ghost {{ color: {t['console_muted']}; }}
#consoleWrap QPushButton#ghost:hover {{ background: #24221f; color: {t['console_text']}; }}
#consoleWrap QPushButton#icon:hover {{ background: #24221f; }}
QSplitter::handle {{ background: transparent; }}
#statusBar {{ background: {t['sidebar']}; border-top: 1px solid {t['border']}; }}
"""


class ThemedLabel(QLabel, kit.Themed):
    """Label whose rich text depends on theme colors: `render()` is called again at every theme change."""

    def __init__(self, render, name=None):
        super().__init__()
        self.render = render
        if name:
            self.setObjectName(name)
        self.follow_theme()

    def restyle(self):
        self.setText(self.render())


# ---------------------------------------------------------------------------
# Hugging Face logic
# ---------------------------------------------------------------------------
QUANT_RE = re.compile(r"(?<![a-z0-9])(int4|int8|fp16|fp32|bf16|nf4|mxfp4|w8a16|w4a16)(?![a-z0-9])", re.I)
QUANT_VARIANT_RE = re.compile(r"(?<![a-z0-9])(cw|gq|sym|asym|awq)(?![a-z0-9])", re.I)


def detect_quant(repo_id, tags=()):
    """Infer the quantization from the repo name (e.g. '...-int4-cw-ov' -> ('INT4', 'CW'))."""
    name = repo_id.split("/")[-1]
    m = QUANT_RE.search(name)
    if m:
        variant = QUANT_VARIANT_RE.search(name, m.end())
        return m.group(1).upper(), (variant.group(1).upper() if variant else "")
    for t in tags:
        if QUANT_RE.fullmatch(t):
            return t.upper(), ""
    return "—", ""


def detect_category(repo_id, pipeline_tag):
    for key, (_, _, tags) in CATEGORIES.items():
        if pipeline_tag in tags:
            return key
    n = repo_id.lower()
    if re.search(r"diffusion|dreamshaper|flux|sdxl|lcm_|sana", n):
        return "image"
    if re.search(r"whisper", n):
        return "asr"
    if re.search(r"embed|bge-|e5-|minilm", n):
        return "embed"
    if re.search(r"rerank", n):
        return "rerank"
    if re.search(r"bert|resnet|yolo|vit-|mobilenet|detr", n):
        return "cv"
    if pipeline_tag is None:
        return "llm"  # most OpenVINO repos without a tag are LLMs
    return "other"


def normalize(raw, official):
    repo_id = raw["id"]
    quant, variant = detect_quant(repo_id, raw.get("tags", []))
    base_model = next((t.split(":", 1)[1] for t in raw.get("tags", []) if t.startswith("base_model:")
                       and not t.startswith("base_model:quantized:")), "")
    return {
        "id": repo_id,
        "author": repo_id.split("/")[0],
        "name": repo_id.split("/")[-1],
        "quant": quant,
        "variant": variant,
        "category": detect_category(repo_id, raw.get("pipeline_tag")),
        "pipeline": raw.get("pipeline_tag") or "",
        "downloads": raw.get("downloads", 0) or 0,
        "likes": raw.get("likes", 0) or 0,
        "updated": raw.get("lastModified") or raw.get("createdAt") or "",
        "base_model": base_model,
        "official": official,
    }


def hf_get(params):
    query = list(params.items()) + [("expand[]", e) for e in HF_EXPAND]
    r = requests.get(HF_API, params=query, timeout=30)
    r.raise_for_status()
    return r.json()


def read_cached_catalog():
    """Catalog saved by the last successful download; None if missing or unreadable."""
    try:
        with open(CACHE_FILE) as f:
            models = json.load(f)
        return models if isinstance(models, list) else None
    except (OSError, ValueError):
        return None


def fetch_official(force=False):
    if not force and os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
        cached = read_cached_catalog()
        if cached is not None:
            return cached, True
    raw = hf_get({"author": "OpenVINO", "sort": "downloads", "direction": "-1", "limit": "1000"})
    models = [normalize(m, True) for m in raw]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(models, f)
    return models, False


def fetch_community(search):
    params = {"filter": "openvino", "sort": "downloads", "direction": "-1", "limit": "300"}
    if search:
        params["search"] = search
    return [normalize(m, False) for m in hf_get(params) if not m["id"].startswith("OpenVINO/")]


def human(n):
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(n)


def human_size(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
        b /= 1024
    return f"{b:.1f} PB"


def rel_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return tr("today")
    if days < 30:
        return tr("yesterday") if days == 1 else tr("{n} days ago").format(n=days)
    if days < 365:
        return tr("1 month ago") if days < 60 else tr("{n} months ago").format(n=days // 30)
    return tr("1 year ago") if days < 730 else tr("{n} years ago").format(n=days // 365)


# ---------------------------------------------------------------------------
# Worker threads
# ---------------------------------------------------------------------------
class Task(QThread):
    """Runs `fn(task)` in the background. `fn` uses task.log() / task.run_cmd() — the UI updates only via signals."""
    line = Signal(str, str)          # text, level
    progress = Signal(int, str)      # percentage (-1 = indeterminate), text
    result = Signal(object)
    failed = Signal(str)

    PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.output_tail = []

    def log(self, text, level="info"):
        self.line.emit(text, level)

    def run_cmd(self, cmd, display=None, env=None):
        """Run cmd showing its output in the console; `display` replaces the command shown in the log.
        The last lines stay in self.output_tail to recognize known errors."""
        self.log("$ " + (display or " ".join(cmd)), "cmd")
        self.output_tail = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    env=env)
        except FileNotFoundError:
            self.log(tr("Command not found: {command}").format(command=cmd[0]), "err")
            return 127
        last_pct = None
        for raw in iter(proc.stdout.readline, ""):
            text = raw.strip()
            if not text:
                continue
            self.output_tail = (self.output_tail + [text])[-200:]
            m = self.PCT_RE.search(text)
            if m:
                pct = min(100, int(float(m.group(1))))
                self.progress.emit(pct, text[:120])
                if pct == last_pct or pct % 10:
                    continue  # don't flood the log with progress bars
                last_pct = pct
            self.log(text)
        proc.stdout.close()
        proc.wait()
        return proc.returncode

    def run(self):
        try:
            self.result.emit(self.fn(self))
        except Exception as e:  # noqa: BLE001 - show any error in the UI
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------
class StatusPill(QWidget):
    """Status indicator with a pulsing halo (steady with reduced motion). The color is a token name ("ok",
    "warn"…), resolved at paint time, so the pill follows theme changes."""
    reduce_motion = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glow = 0.0
        self.key = "muted"
        kit.signals.changed.connect(self.update)
        self.text = tr("Checking…")
        self.setFixedHeight(34)
        self.setMinimumWidth(170)
        self.anim = QPropertyAnimation(self, b"glow", self)
        self.anim.setDuration(1400)
        self.anim.setStartValue(0.0)
        self.anim.setKeyValueAt(0.5, 1.0)
        self.anim.setEndValue(0.0)
        self.anim.setEasingCurve(QEasingCurve.InOutSine)
        self.anim.setLoopCount(-1)

    def get_glow(self):
        return self._glow

    def set_glow(self, v):
        self._glow = v
        self.update()

    glow = Property(float, get_glow, set_glow)

    @property
    def color(self):
        return QColor(C[self.key])

    def set_state(self, text, key, pulsing):
        self.text, self.key = text, key
        bold = QFont(self.font())
        bold.setBold(True)
        self.setMinimumWidth(max(170, QFontMetrics(bold).horizontalAdvance(text) + 52))  # dot + padding
        pulsing = pulsing and not self.reduce_motion
        if pulsing and self.anim.state() != QPropertyAnimation.Running:
            self.anim.start()
        elif not pulsing:
            self.anim.stop()
            self._glow = 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        bg = QColor(self.color)
        bg.setAlphaF(0.12)
        p.setPen(QPen(QColor(self.color.red(), self.color.green(), self.color.blue(), 110), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        cx, cy = r.left() + 18, r.center().y()
        halo = QColor(self.color)
        halo.setAlphaF(0.35 * self._glow)
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        rad = 5 + 5 * self._glow
        p.drawEllipse(QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad))
        p.setBrush(self.color)
        p.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))
        p.setPen(self.color.lighter(130))
        f = self.font()
        f.setBold(True)
        p.setFont(f)
        p.drawText(r.adjusted(34, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text)


class BadgeDelegate(QStyledItemDelegate):
    """Draws the quantization as a colored badge."""

    def paint(self, painter, option, index):
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(C["accent_soft"]))
        text = index.data(Qt.DisplayRole) or ""
        if not text:
            return
        base = text.split()[0]
        color = QColor(QUANT_COLORS.get(base, "#64748b"))
        light = C["scheme"] == "light"
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        f = QFont(option.font)
        f.setBold(True)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.5))
        painter.setFont(f)
        w = painter.fontMetrics().horizontalAdvance(text) + 18
        h = 22
        r = QRectF(option.rect.left() + 8, option.rect.center().y() - h / 2, w, h)
        bg = QColor(color)
        bg.setAlphaF(0.16)
        painter.setBrush(bg)
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 140), 1))
        painter.drawRoundedRect(r, 11, 11)
        painter.setPen(color.darker(170) if light else color.lighter(115))
        painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()


def card(title=None, subtitle=None):
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 18, 20, 20)
    lay.setSpacing(12)
    if title:
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    return frame, lay


def quant_guide():
    """Card explaining quantization."""
    frame, lay = card(tr("What is quantization?"),
                      tr("An LLM is made of billions of numbers (the “weights”). Quantizing means storing them with "
                         "fewer bits: the model takes less memory and generates faster, in exchange for a small loss "
                         "of precision. In OpenVINO the weights are compressed, while computation stays "
                         "floating-point."))
    grid = QGridLayout()
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(10)
    grid.setColumnStretch(2, 1)
    for r, (q, (size, text)) in enumerate(QUANT_INFO.items()):
        color = QColor(QUANT_COLORS.get(q, C["muted"]))
        color = (color.darker(170) if C["scheme"] == "light" else color).name()
        badge = QLabel(tr("Other") if q == "OTHER" else q)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedWidth(64)
        badge.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:10px; "
                            f"padding:2px 6px; font-weight:700; font-size:11px;")
        sz = QLabel(tr(size))
        sz.setObjectName("hint")
        desc = QLabel(tr(text))
        desc.setWordWrap(True)
        grid.addWidget(badge, r, 0, Qt.AlignTop)
        grid.addWidget(sz, r, 1, Qt.AlignTop)
        grid.addWidget(desc, r, 2)
    lay.addLayout(grid)
    variants = QLabel(f"<b>{tr('Name suffixes')}</b> &nbsp;·&nbsp; " + " &nbsp;·&nbsp; ".join(
        f"<b>{k}</b> {tr(v).split(':')[0]}" for k, v in QUANT_VARIANT_INFO.items()))
    variants.setObjectName("hint")
    variants.setWordWrap(True)
    lay.addWidget(variants)
    tip = QLabel(f"<span style='color:{C['accent_text']}'><b>{tr('Tip:')}</b></span> " + tr(
        "for chat on an Intel GPU or NPU start from <b>INT4</b> (on the NPU prefer the <b>INT4 CW</b> variants). "
        "Move to <b>INT8</b> if answers seem less accurate and you have enough memory; <b>FP16</b> is only worth "
        "it for maximum fidelity."))
    tip.setWordWrap(True)
    lay.addWidget(tip)
    return frame


def show_quant_guide(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("Quantization guide"))
    dlg.setStyleSheet(f"QDialog {{ background: {C['bg']}; }}")
    dlg.resize(760, 600)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(20, 20, 20, 16)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    scroll.setWidget(quant_guide())
    lay.addWidget(scroll, 1)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(button(tr("Got it"), "primary", dlg.accept))
    lay.addLayout(row)
    dlg.exec()


def guide_button(parent):
    return kit.IconButton("info", tr("What is quantization?"), "chip", lambda: show_quant_guide(parent), size=16)


def page_header(title, subtitle):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(4)
    t = QLabel(title)
    t.setObjectName("pageTitle")
    s = QLabel(subtitle)
    s.setObjectName("pageSub")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def scrollable(page):
    """Page that scrolls when the window is too short, instead of squeezing its cards."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    area.setWidget(page)
    return area


def button(text, kind=None, slot=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


# ---------------------------------------------------------------------------
# Catalog table model
# ---------------------------------------------------------------------------
class CatalogModel(QAbstractTableModel):
    HEADERS = [N_("Model"), N_("Quantization"), N_("Type"), N_("Author"), N_("Downloads"), N_("Likes"),
               N_("Updated")]

    def __init__(self):
        super().__init__()
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return tr(self.HEADERS[section])
        return None

    def data(self, index, role=Qt.DisplayRole):
        m = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return [
                m["name"],
                f"{m['quant']} {m['variant']}".strip() if m["quant"] != "—" else "—",
                tr(CATEGORIES[m["category"]][0]),
                ("✓ " if m["official"] else "") + m["author"],
                "⬇ " + human(m["downloads"]),
                "♥ " + human(m["likes"]),
                rel_time(m["updated"]),
            ][col]
        if role == Qt.UserRole:  # sort key
            return [m["name"].lower(), m["quant"], m["category"], m["author"].lower(),
                    m["downloads"], m["likes"], m["updated"]][col]
        if role == Qt.UserRole + 1:
            return m
        if role == Qt.ForegroundRole:
            if col == 3 and m["official"]:
                return QBrush(QColor(C["accent"]))
            if col >= 4:
                return QBrush(QColor(C["muted"]))
        if role == Qt.FontRole and col == 0:
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ToolTipRole:
            return quant_tooltip(m["quant"], m["variant"]) if col == 1 else m["id"]
        return None


class CatalogFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.search = ""
        self.quants = set()      # empty = all
        self.category = "llm"    # None = all
        self.setSortRole(Qt.UserRole)

    def filterAcceptsRow(self, row, parent):
        m = self.sourceModel().rows[row]
        if self.category and m["category"] != self.category:
            return False
        return self.matches(m)

    def matches(self, m):
        """Search + quantization, without the type filter (also used for the counts)."""
        if self.quants:
            q = m["quant"] if m["quant"] in ("INT4", "INT8", "FP16") else "OTHER"
            if q not in self.quants:
                return False
        if self.search:
            hay = f"{m['id']} {m['base_model']}".lower()
            return all(tok in hay for tok in self.search.lower().split())
        return True


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class OVMSManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OVMS Manager · OpenVINO Model Server")
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)
        self.tasks = set()   # every live thread (for shutdown)
        self.jobs = set()    # operations visible to the user (progress bar)
        self.official, self.community = [], []
        self.selected_model = None
        self.stop_wait = threading.Event()
        state = load_state()
        self.saved_rows = state.get("server_rows")
        legacy = state.get("served_name")
        self.served_names = state.get("served_names", [legacy] if legacy else [])
        self.chat_names = state.get("chat_names", [legacy] if legacy and detect_category(legacy, None) == "llm" else [])
        self.deployment_dir = Path(DATA_DIR) / APP_ID / "deployment"
        self.server_lock_path = self.deployment_dir.with_name("server.lock")
        self.local_only = bool(state.get("local_only", False))
        # new releases on GitHub: checked at most once a day, in the background (command palette to turn it off)
        self.updates = kit.UpdateChecker(self, APP_VERSION, lambda k, d=None: load_state().get(k, d),
                                         lambda k, v: save_state(**{k: v}), APP_REPO)
        self.palette_dlg = kit.CommandPalette(self, self._palette_commands, tr("Type a command or a model…"),
                                              tr("No results"))
        QShortcut(QKeySequence("Ctrl+K"), self, self.palette_dlg.open_palette)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)
        body.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(28, 24, 28, 14)
        right.setSpacing(12)
        body.addLayout(right, 1)
        self.update_banner = kit.UpdateBanner(tr("{app} {version} is available.").replace("{app}", "OVMS Manager"),
                                              tr("What's new"), tr("Skip this version"), tr("Later"),
                                              self.updates.skip)
        self.updates.found.connect(self.update_banner.show_release)
        right.addWidget(self.update_banner)

        self.stack = QStackedWidget()
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self._build_console())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setChildrenCollapsible(False)
        self.toggle_console(False)  # progressive disclosure: only the last line until it is needed
        right.addWidget(self.splitter, 1)

        self.stack.addWidget(scrollable(self._build_server_page()))
        self.stack.addWidget(self._build_explore_page())
        self.stack.addWidget(self._build_library_page())
        self.stack.addWidget(scrollable(self._build_converter_page()))
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        outer.addWidget(self._build_statusbar())

        self.refresh_local_models()
        if RUNTIME is None:
            self.log(tr(RUNTIME_HINT), "err")
        for kind, status in detect_accelerators().items():
            if status == "hidden":
                self.log(tr("Intel {device} present but {node} is not accessible: the app seems to run in a sandbox, "
                            "the container will not be able to use it.").format(device=kind.upper(),
                                                                                node=DEVICE_NODES[kind]), "warn")
        self.status_timer = QTimer(self, interval=4000, timeout=self.check_server_status)
        self.status_timer.start()
        self.check_server_status()
        self.load_catalog()
        QTimer.singleShot(4000, self.updates.check)
        self.update_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=self.updates.check)
        self.update_timer.start()

    def check_updates_now(self):
        def done(release):
            self.updates.finished.disconnect(done)
            if release is None:
                self.log(tr("{app} {version} is the latest version (or GitHub could not be reached).")
                         .format(app="OVMS Manager", version=APP_VERSION), "ok")
            else:
                self.log(tr("{app} {version} is available.").replace("{app}", "OVMS Manager")
                         .format(version=release.version) + f" {release.url}", "ok")
        self.updates.finished.connect(done)
        self.updates.check(force=True)

    def toggle_update_checks(self):
        self.updates.set_enabled(not self.updates.enabled)
        self.log(tr("Automatic update checks: {state}.").format(
            state=tr("on") if self.updates.enabled else tr("off")), "ok")

    # --- Layout ----------------------------------------------------------------
    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(248)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(8, 20, 8, 14)
        lay.setSpacing(4)

        brand = QHBoxLayout()
        brand.setContentsMargins(12, 0, 8, 16)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(QIcon(os.path.join(APP_DIR, f"{APP_ID}.svg")).pixmap(30, 30))
        brand.addWidget(logo)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        b1 = QLabel("OVMS Manager")
        b1.setObjectName("brand")
        b2 = QLabel(f"{kit.FAMILY_NAME} · OpenVINO")  # the family the app belongs to
        b2.setObjectName("brandSub")
        txt.addWidget(b1)
        txt.addWidget(b2)
        brand.addLayout(txt)
        brand.addStretch()
        lay.addLayout(brand)
        search = kit.IconButton("search", tr("Search…"), "ghost", lambda: self.palette_dlg.open_palette(), size=16)
        search.setToolTip(tr("Command palette (Ctrl+K)"))
        search.setStyleSheet("text-align: left; padding-left: 21px;")
        lay.addWidget(search)
        lay.addSpacing(4)

        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setCursor(Qt.PointingHandCursor)
        self.nav.setIconSize(QSize(18, 18))
        self.nav_icons = ("server", "explore", "library", "converter")
        for label in NAV_PAGES:
            self.nav.addItem(QListWidgetItem(tr(label)))
        kit.signals.changed.connect(self._refresh_nav_icons)
        self._refresh_nav_icons()
        lay.addWidget(self.nav, 1)

        for icon_name, tip, combo in (("globe", tr("Interface language"), "cb_ui_lang"),
                                      ("theme", tr("Theme"), "cb_theme")):
            row = QHBoxLayout()
            row.setContentsMargins(10, 0, 10, 6)
            row.setSpacing(6)
            pic = QLabel()

            def draw(pic=pic, n=icon_name):
                pic.setPixmap(kit.icon(n, C["muted"], 16).pixmap(16, 16))
            draw()
            kit.signals.changed.connect(draw)
            pic.setToolTip(tip)
            row.addWidget(pic)
            cb = QComboBox()
            cb.setToolTip(tip)
            row.addWidget(cb, 1)
            setattr(self, combo, cb)
            lay.addLayout(row)
        for code, name in UI_LANGUAGES:
            self.cb_ui_lang.addItem(tr(name), code)
        self.cb_ui_lang.setCurrentIndex(max(0, self.cb_ui_lang.findData(load_state().get("ui_language", ""))))
        self.cb_ui_lang.currentIndexChanged.connect(self._on_ui_language)
        for code, name in THEMES:
            self.cb_theme.addItem(tr(name), code)
        self.cb_theme.setCurrentIndex(max(0, self.cb_theme.findData(load_state().get("theme", "system"))))
        self.cb_theme.currentIndexChanged.connect(self._on_theme)
        lay.addSpacing(6)

        foot, flay = card()
        flay.setContentsMargins(14, 12, 10, 12)
        flay.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(QLabel(f"<b>{tr('Endpoint')}</b>"))
        top.addStretch()
        top.addWidget(kit.IconButton("copy", tooltip=tr("Copy the endpoint URL"), size=16,
                                     slot=lambda: self._copy(f"{OVMS_URL}/v3")))
        flay.addLayout(top)
        ep = ThemedLabel(lambda: f"<span style='color:{C['accent_text']}'>{OVMS_HOST}:{OVMS_PORT}/v3</span>")
        ep.setTextInteractionFlags(Qt.TextSelectableByMouse)
        flay.addWidget(ep)
        flay.addWidget(kit.Chip("lock", tr("Local · OpenAI API"), "neutral",
                                tr("The models run on this computer. Runtime: {runtime}").format(runtime=RUNTIME_NAME)))
        lay.addWidget(foot)
        return side

    def _refresh_nav_icons(self):
        for i, name in enumerate(self.nav_icons):
            self.nav.item(i).setIcon(kit.icon(name, C["muted"], 18))

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.lbl_activity.setText(tr("Copied: {text}").format(text=text))

    def _on_ui_language(self, _index):
        save_state(ui_language=self.cb_ui_lang.currentData())
        QMessageBox.information(self, "OVMS Manager", tr("The new language will be used the next time you start "
                                                         "{app}.").format(app="OVMS Manager"))

    def _on_theme(self, _index):
        save_state(theme=self.cb_theme.currentData())
        if THEME:
            THEME.set_preference(self.cb_theme.currentData())

    def _build_console(self):
        """Console as progressive disclosure: collapsed it shows only the last line; it opens with a click, and
        by itself when an error arrives."""
        wrap = QFrame()
        wrap.setObjectName("consoleWrap")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.btn_console = kit.IconButton("chevron_up", tooltip=tr("Show or hide the console"), size=16,
                                          color_key="console_muted", slot=lambda: self.toggle_console())
        head.addWidget(self.btn_console)
        t = QLabel(tr("Console"))
        t.setObjectName("consoleTitle")
        head.addWidget(t)
        self.console_last = QLabel("")
        self.console_last.setObjectName("consoleLast")
        self.console_last.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        head.addWidget(self.console_last, 1)
        head.addWidget(button(tr("Clear"), "ghost", lambda: (self.console.clear(), self.console_last.clear())))
        lay.addLayout(head)
        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        lay.addWidget(self.console, 1)
        self.console_open = True
        return wrap

    def toggle_console(self, show=None):
        show = not self.console_open if show is None else show
        self.console_open = show
        self.console.setVisible(show)
        self.console_last.clear()  # open, the console shows everything; closed, only the last line
        self.btn_console.set_icon_name("chevron_down" if show else "chevron_up")
        total = sum(self.splitter.sizes()) or 800
        self.splitter.setSizes([total - 220, 220] if show else [total - 44, 44])

    def _build_statusbar(self):
        bar = QFrame()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(34)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        self.lbl_activity = QLabel(tr("Ready"))
        self.lbl_activity.setObjectName("hint")
        lay.addWidget(self.lbl_activity, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.setTextVisible(False)
        self.progress.hide()
        lay.addWidget(self.progress)
        return bar

    # --- Server page -----------------------------------------------------------
    def _build_server_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        head = QHBoxLayout()
        head.addWidget(page_header(tr("OVMS server"), tr("Serve your local models together on one endpoint.")), 1)
        self.chip_model = kit.Chip("server", tr("No model served"), "neutral", tr("Models configured on the running server"))
        head.addWidget(self.chip_model, 0, Qt.AlignTop)
        head.addSpacing(6)
        self.pill = StatusPill()
        head.addWidget(self.pill, 0, Qt.AlignTop)
        lay.addLayout(head)

        c1, l1 = card(tr("Models to serve"), tr("Choose a model and its accelerator on each row."))
        self.model_rows = []
        self.refreshing_rows = False
        self.local_models = []
        self.accelerators = detect_accelerators()
        self.empty_models = QLabel(tr("No models — download one from Explore Hugging Face"))
        self.empty_models.setObjectName("hint")
        l1.addWidget(self.empty_models)
        self.rows_layout = QVBoxLayout()
        l1.addLayout(self.rows_layout)
        actions = QHBoxLayout()
        self.btn_add_model = button(tr("Add model"), slot=lambda: self._add_server_row())
        actions.addWidget(self.btn_add_model)
        actions.addStretch()
        actions.addWidget(kit.IconButton("refresh", tr("Refresh"), slot=self.refresh_local_models))
        actions.addWidget(kit.IconButton("explore", tr("Find models"), slot=lambda: self.nav.setCurrentRow(1)))
        l1.addLayout(actions)
        lay.addWidget(c1)

        c3, l3 = card(tr("Controls"))
        ctrl = QHBoxLayout()
        self.btn_start = kit.IconButton("play", tr("Start server"), "primary", self.start_server)
        self.btn_stop = kit.IconButton("stop", tr("Stop"), "danger", self.stop_server)
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_stop)
        self.chk_debug = QCheckBox(tr("Verbose log"))
        self.chk_debug.setToolTip(tr("Start OVMS with --log_level DEBUG: useful to see where loading gets stuck"))
        ctrl.addWidget(self.chk_debug)
        self.chk_local = QCheckBox(tr("Localhost only"))
        self.chk_local.setChecked(self.local_only)
        self.chk_local.setToolTip(tr("Publish port {port} on 127.0.0.1 only, so other computers on the network "
                                     "cannot use the API (it has no authentication).\nLeave it off if Open WebUI "
                                     "runs in a container and reaches the server through {host}.")
                                  .format(port=OVMS_PORT, host=HOST_ALIAS))
        self.chk_local.toggled.connect(lambda on: save_state(local_only=on))
        ctrl.addWidget(self.chk_local)
        ctrl.addStretch()
        l3.addLayout(ctrl)
        tools = QHBoxLayout()  # second row: the page stays usable in narrow windows
        tools.addWidget(kit.IconButton("globe", "Open WebUI", slot=lambda: webbrowser.open(OPEN_WEBUI_URL)))
        tools.addWidget(kit.IconButton("flask", tr("Test API"), slot=self.test_api))
        tools.addWidget(kit.IconButton("log", tr("Container log"), slot=self.show_container_logs))
        tools.addStretch()
        l3.addLayout(tools)
        lay.addWidget(c3)

        c4, l4 = card(tr("Connect Open WebUI"))
        guide = ThemedLabel(lambda: tr(
            "In Open WebUI open <b>Admin Settings → Connections → OpenAI API</b>, press <b>+</b> and enter:"
            "<br>• URL: {url} &nbsp;{container_note}"
            "<br>• API key: any text (e.g. <i>none</i>)"
            "<br>• The model shows up with the name of its folder (e.g. <b>Qwen3.5-4B-int8</b>). "
            "If it does not, add it by hand in the “Model IDs” field of the connection.").format(
            url=f"<span style='color:{C['accent_text']}'>{OVMS_URL}/v3</span>",
            container_note=f"<span style='color:{C['muted']}'>" + tr("(if Open WebUI runs in a container: {url})")
            .format(url=f"<span style='color:{C['accent_text']}'>{CONTAINER_URL}</span>") + "</span>"))
        guide.setWordWrap(True)
        guide.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l4.addWidget(guide)
        lay.addWidget(c4)
        lay.addStretch()
        return page

    # --- Explore page ----------------------------------------------------------
    def _build_explore_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(page_header(tr("Explore Hugging Face"),
                                   tr("Models already converted to OpenVINO IR and quantized, ready to download.")), 1)
        self.btn_refresh_hf = kit.IconButton("refresh", tr("Refresh catalog"), slot=lambda: self.load_catalog(force=True))
        head.addWidget(self.btn_refresh_hf, 0, Qt.AlignTop)
        lay.addLayout(head)

        # Filter bar
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search by name or base model (e.g. qwen 7b, llama, phi)…"))
        search_action = self.search.addAction(kit.icon("search"), QLineEdit.LeadingPosition)
        kit.signals.changed.connect(lambda: search_action.setIcon(kit.icon("search")))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        bar.addWidget(self.search, 1)
        self.cb_category = QComboBox()
        self.cb_category.addItem(tr("All types"), None)
        for key, (label, _, _) in CATEGORIES.items():
            self.cb_category.addItem(tr(label), key)
        self.cb_category.setCurrentIndex(1)
        self.cb_category.currentIndexChanged.connect(lambda _: self._apply_filters())
        bar.addWidget(self.cb_category)
        self.chk_community = QCheckBox(tr("Community"))
        self.chk_community.setToolTip(tr("Include community repos tagged 'openvino'"))
        self.chk_community.toggled.connect(self._on_community_toggled)
        bar.addWidget(self.chk_community)
        lay.addLayout(bar)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        lbl = QLabel(tr("Quantization"))
        lbl.setObjectName("hint")
        chips.addWidget(lbl)
        self.quant_chips = {}
        for q in ("INT4", "INT8", "FP16", "OTHER"):
            b = button(tr("Other") if q == "OTHER" else q, "chip")
            b.setCheckable(True)
            b.setToolTip(quant_tooltip(q))
            b.toggled.connect(lambda _: self._apply_filters(auto=True))
            self.quant_chips[q] = b
            chips.addWidget(b)
        chips.addWidget(guide_button(self))
        chips.addStretch()
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("hint")
        chips.addWidget(self.lbl_count)
        lay.addLayout(chips)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(12)

        self.catalog = CatalogModel()
        self.proxy = CatalogFilter()
        self.proxy.setSourceModel(self.catalog)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setFont(kit.tabular(self.table.font()))  # numbers line up in the columns
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, Qt.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setItemDelegateForColumn(1, BadgeDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        for col, w in ((1, 118), (2, 128), (3, 130), (4, 90), (5, 62), (6, 110)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            hh.resizeSection(col, w)
        hh.setHighlightSections(False)
        hh.setMinimumSectionSize(56)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.viewport().installEventFilter(self)
        self.table.selectionModel().currentRowChanged.connect(self._on_select)
        self.table.doubleClicked.connect(lambda _: self.download_selected())
        self.empty_hint = QLabel(self.table.viewport())
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setObjectName("muted")
        self.empty_hint.hide()
        split.addWidget(self.table)
        split.addWidget(self._build_detail_panel())
        split.setStretchFactor(0, 1)
        split.setSizes([800, 300])
        lay.addWidget(split, 1)

        custom = QHBoxLayout()
        custom.setSpacing(10)
        lbl = QLabel(tr("Custom repo"))
        lbl.setObjectName("hint")
        custom.addWidget(lbl)
        self.entry_custom_dl = QLineEdit()
        self.entry_custom_dl.setPlaceholderText(tr("e.g. {repo}").format(repo="OpenVINO/Qwen2.5-7B-Instruct-int4-ov"))
        self.entry_custom_dl.returnPressed.connect(self.download_custom)
        custom.addWidget(self.entry_custom_dl, 1)
        self.btn_download_custom = kit.IconButton("download", tr("Download"), slot=self.download_custom)
        custom.addWidget(self.btn_download_custom)
        lay.addLayout(custom)
        return page

    def _build_detail_panel(self):
        panel, lay = card()
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(340)
        self.d_title = QLabel(tr("Select a model"))
        self.d_title.setObjectName("cardTitle")
        self.d_title.setWordWrap(True)
        self.d_title.setStyleSheet("font-size: 16px;")
        self.d_repo = QLabel("")
        self.d_repo.setObjectName("hint")
        self.d_repo.setWordWrap(True)
        self.d_repo.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.d_badges = QLabel("")
        self.d_info = QLabel(tr("Pick a model in the table to see its details.\nDouble-click to download it."))
        self.d_info.setObjectName("muted")
        self.d_info.setWordWrap(True)
        self.d_grid = QGridLayout()
        self.d_grid.setHorizontalSpacing(14)
        self.d_grid.setVerticalSpacing(8)
        self.d_grid.setColumnStretch(1, 1)
        self.chip_ram = kit.Chip("memory", "", "neutral", tr("Estimate from the repository size: the model needs "
                                                            "about this much memory, plus some room for the context."))
        self.chip_ram.hide()
        for w in (self.d_title, self.d_repo, self.d_badges, self.d_info):
            lay.addWidget(w)
        lay.addLayout(self.d_grid)
        lay.addWidget(self.chip_ram, 0, Qt.AlignLeft)
        lay.addStretch()

        self.btn_download = kit.IconButton("download", tr("Download with OVMS"), "primary", self.download_selected)
        self.btn_download.setEnabled(False)
        self.btn_hf = kit.IconButton("external", tr("Open on Hugging Face"), slot=self.open_on_hf)
        self.btn_hf.setEnabled(False)
        lay.addWidget(self.btn_download)
        lay.addWidget(self.btn_hf)

        return panel

    # --- Library page ----------------------------------------------------------
    def _build_library_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(page_header(tr("Local library"), tr("Models in {folder}").format(folder=MODELS_LABEL)), 1)
        head.addWidget(kit.IconButton("refresh", tr("Refresh"), slot=self.refresh_local_models), 0, Qt.AlignTop)
        lay.addLayout(head)

        self.lib_list = QListWidget()
        self.lib_list.setObjectName("lib")
        self.lib_list.setSpacing(2)
        self.lib_list.itemDoubleClicked.connect(self._use_library_model)
        lay.addWidget(self.lib_list, 1)

        actions = QHBoxLayout()
        self.lbl_lib_total = QLabel("")
        self.lbl_lib_total.setObjectName("hint")
        actions.addWidget(self.lbl_lib_total)
        actions.addStretch()
        actions.addWidget(kit.IconButton("folder", tr("Open folder"), slot=self.open_library_folder))
        actions.addWidget(kit.IconButton("play", tr("Use in server"), "primary", slot=
                                 lambda: self._use_library_model(self.lib_list.currentItem())))
        actions.addWidget(kit.IconButton("delete", tr("Delete"), "danger", self.delete_library_model))
        lay.addLayout(actions)
        return page

    # --- Converter page --------------------------------------------------------
    def _build_converter_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(page_header(tr("Optimum converter"),
                                  tr("Export and quantize a Hugging Face PyTorch model to OpenVINO IR.")))

        c, l = card(tr("Source"))
        l.addWidget(QLabel(tr("PyTorch model ID (Hugging Face)")))
        self.entry_hf_id = QLineEdit("Qwen/Qwen2.5-7B-Instruct")
        self.entry_hf_id.textChanged.connect(self._suggest_output_name)
        l.addWidget(self.entry_hf_id)
        lay.addWidget(c)

        c, l = card(tr("Weight precision"))
        seg = QHBoxLayout()
        seg.setSpacing(8)
        self.prec_group = QButtonGroup(self)
        for prec, desc in (("int4", N_("Recommended for GPU/NPU")), ("int8", N_("Balanced")),
                           ("fp16", N_("Best quality"))):
            b = button(f"{prec.upper()}  ·  {tr(desc)}", "chip")
            b.setCheckable(True)
            b.setProperty("prec", prec)
            b.setMinimumHeight(38)
            self.prec_group.addButton(b)
            seg.addWidget(b)
            if prec == "int4":
                b.setChecked(True)
        self.prec_group.buttonToggled.connect(lambda *_: self._suggest_output_name())
        for b in self.prec_group.buttons():
            b.setToolTip(quant_tooltip(b.property("prec").upper()))
        seg.addStretch()
        seg.addWidget(guide_button(self))
        l.addLayout(seg)
        lay.addWidget(c)

        c, l = card(tr("Advanced options"))
        self.chk_trust = QCheckBox(tr("Allow custom code from the repository (--trust-remote-code)"))
        self.chk_trust.setToolTip(tr("Some models with new architectures ship Python code that must run to load "
                                     "them.\nOnly enable it for repositories you trust: that code runs on your "
                                     "computer."))
        l.addWidget(self.chk_trust)
        hint = QLabel(tr("Only needed for some models with non-standard architectures. The code is downloaded "
                         "from the repository and executed on your computer: enable it only if you trust the "
                         "author."))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        l.addWidget(hint)
        lay.addWidget(c)

        c, l = card(tr("Destination"))
        l.addWidget(QLabel(tr("Folder name in {folder}").format(folder=MODELS_LABEL)))
        self.entry_output_name = QLineEdit()
        l.addWidget(self.entry_output_name)
        self._suggest_output_name()
        lay.addWidget(c)

        row = QHBoxLayout()
        if not find_optimum():
            missing = QLabel(tr("optimum-cli not found: run <b>install.sh --with-optimum</b> to enable "
                                "conversion."))
            missing.setObjectName("hint")
            missing.setWordWrap(True)
            row.addWidget(missing, 1)
        row.addStretch()
        self.btn_convert = kit.IconButton("converter", tr("Convert with Optimum"), "primary", self.start_conversion)
        self.btn_convert.setMinimumWidth(240)
        row.addWidget(self.btn_convert)
        lay.addLayout(row)
        lay.addStretch()
        return page

    # --- UI helpers ------------------------------------------------------------
    def log(self, text, level="info"):
        colors = {"info": C["console_text"], "ok": C["console_ok"], "err": C["console_err"],
                  "warn": C["console_warn"], "cmd": C["console_cmd"], "head": C["console_head"]}
        ts = datetime.now().strftime("%H:%M:%S")
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if hasattr(self, "console_last") and not self.console_open:
            self.console_last.setText(f"<span style='color:{colors.get(level, colors['info'])}'>{safe}</span>")
            if level == "err" and not self.console_open:
                self.toggle_console(True)
        weight = "font-weight:700;" if level in ("head", "ok", "err") else ""
        self.console.appendHtml(f"<span style='color:{C['console_muted']}'>{ts}</span>&nbsp; "
                                f"<span style='color:{colors.get(level, colors['info'])};{weight}'>{safe}</span>")

    def run_task(self, fn, label, buttons=(), on_done=None):
        for b in buttons:
            b.setEnabled(False)
        task = Task(fn, self)
        self.tasks.add(task)
        self.jobs.add(task)
        self.lbl_activity.setText(f"⏳ {label}…")
        self.progress.setRange(0, 0)
        self.progress.show()

        def on_progress(pct, text):
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            self.lbl_activity.setText(f"⏳ {label} — {pct}%")

        def finish():
            self.tasks.discard(task)
            self.jobs.discard(task)
            for b in buttons:
                b.setEnabled(True)
            if not self.jobs:
                self.progress.hide()
                self.lbl_activity.setText(tr("Ready"))
            task.deleteLater()

        task.line.connect(self.log)
        task.progress.connect(on_progress)
        task.failed.connect(lambda msg: self.log(tr("Error: {error}").format(error=msg), "err"))
        if on_done:
            task.result.connect(on_done)
        task.finished.connect(finish)
        task.start()
        return task

    def require_runtime(self):
        """False (with an explanation in the console) if neither Podman nor Docker is installed."""
        if RUNTIME:
            return True
        self.log(tr(RUNTIME_HINT), "err")
        return False

    # --- Local models ----------------------------------------------------------
    def scan_local_models(self):
        found = []
        for root, dirs, files in os.walk(MODELS_DIR):
            dirs.sort()
            if root == MODELS_DIR:
                continue  # the repository root only holds config.json and the model folders
            if any(f.endswith(".xml") for f in files) or "graph.pbtxt" in files or "model_index.json" in files:
                found.append(os.path.relpath(root, MODELS_DIR))
                dirs[:] = []  # don't descend into subfolders (e.g. unet/, text_encoder/)
        return found

    def _add_server_row(self, path=None, mode=None):
        selected = {model.currentData() for _, model, _ in self.model_rows}
        path = path or next((p for p in self.local_models if p not in selected), None)
        if path is None:
            return
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        model = QComboBox()
        model.setAccessibleName(tr("Model"))
        model.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for rel in self.local_models:
            model.addItem(rel, rel)
        if path not in self.local_models:
            model.addItem(tr("{model} (missing)").format(model=path), path)
        model.setCurrentIndex(model.findData(path))
        layout.addWidget(model, 1)
        hardware = QComboBox()
        hardware.setAccessibleName(tr("Hardware accelerator"))
        first_ok = None
        for key, hw in HARDWARE_MODES.items():
            status = self.accelerators.get(hw["probe"], "ok")
            label = {"ok": tr("detected"), "hidden": tr("present · /dev not accessible"),
                     "other": tr("not Intel · unsupported"), "missing": tr("not detected")}[status]
            hardware.addItem(kit.icon(hw["icon"], C["muted"], 16), f"{tr(hw['title'])} · {label}", key)
            hardware.setItemData(hardware.count() - 1, tr(hw["desc"]), Qt.ToolTipRole)
            if first_ok is None and status == "ok":
                first_ok = key
        hardware.setCurrentIndex(hardware.findData(mode if mode in HARDWARE_MODES else first_ok))
        layout.addWidget(hardware)
        layout.addWidget(kit.IconButton("delete", tooltip=tr("Remove from server configuration"),
                                        slot=lambda: self._remove_server_row(row)))
        self.model_rows.append((row, model, hardware))
        self.rows_layout.addWidget(row)
        model.currentIndexChanged.connect(self._save_server_rows)
        hardware.currentIndexChanged.connect(self._save_server_rows)
        self._save_server_rows()

    def _remove_server_row(self, row):
        self.model_rows = [entry for entry in self.model_rows if entry[0] is not row]
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._save_server_rows()

    def _save_server_rows(self):
        rows = [[m.currentData(), hw.currentData()] for _, m, hw in self.model_rows]
        self.btn_add_model.setEnabled(bool(set(self.local_models) - {path for path, _ in rows}))
        if not self.refreshing_rows and rows != self.saved_rows:
            self.saved_rows = rows
            save_state(server_rows=rows)

    def _use_server_model(self, rel):
        if not any(model.currentData() == rel for _, model, _ in self.model_rows):
            self._add_server_row(rel)
        self.nav.setCurrentRow(0)

    def refresh_local_models(self):
        found = self.scan_local_models()
        self.local_models = found
        self.empty_models.setVisible(not found)
        rows = self.saved_rows
        if rows is None:
            first = next((rel for rel in found if served_name_for(rel) in self.served_names), next(iter(found), None))
            rows = [(first, None)] if first else []
        self.refreshing_rows = True
        for row, _, _ in self.model_rows.copy():
            self._remove_server_row(row)
        for path, mode in rows:
            self._add_server_row(path, mode)
        self.refreshing_rows = False
        if found or self.saved_rows is not None:
            self._save_server_rows()
        else:
            self.btn_add_model.setEnabled(False)

        self.lib_list.clear()
        total = 0
        for rel in found:
            path = os.path.join(MODELS_DIR, rel)
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs
                       if not os.path.islink(os.path.join(r, f)))
            total += size
            quant, variant = detect_quant(rel)
            badge = f"{quant} {variant}".strip()
            item = QListWidgetItem(f"{rel}\n{human_size(size)}   ·   {badge}")
            item.setData(Qt.UserRole, rel)
            item.setToolTip(path)
            self.lib_list.addItem(item)
        self.lbl_lib_total.setText(tr("{n} models · {size} used").format(n=len(found), size=human_size(total)))

    def _use_library_model(self, item):
        if not item:
            return
        self._use_server_model(item.data(Qt.UserRole))

    def open_library_folder(self):
        item = self.lib_list.currentItem()
        path = os.path.join(MODELS_DIR, item.data(Qt.UserRole)) if item else MODELS_DIR
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def delete_library_model(self):
        item = self.lib_list.currentItem()
        if not item:
            QMessageBox.information(self, tr("Delete model"), tr("Select a model in the list first."))
            return
        rel = item.data(Qt.UserRole)
        path = os.path.realpath(os.path.join(MODELS_DIR, rel))
        if not path.startswith(os.path.realpath(MODELS_DIR) + os.sep):
            QMessageBox.warning(self, tr("Delete model"), tr("Invalid path, outside {folder}:\n{path}")
                                .format(folder=MODELS_DIR, path=path))
            return
        ok = QMessageBox.question(self, tr("Delete model"), tr("Permanently delete\n{path}?").format(path=path),
                                  QMessageBox.Yes | QMessageBox.No)
        if ok != QMessageBox.Yes:
            return

        def job(t):
            t.log(tr("Deleting {model}").format(model=rel), "head")
            errors = []

            def on_error(_func, failed_path, exc):
                exc = exc[1] if isinstance(exc, tuple) else exc  # onerror passes a tuple, onexc the exception
                errors.append((failed_path, exc))

            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=on_error)
            else:
                shutil.rmtree(path, onerror=on_error)
            if not os.path.exists(path):
                return []

            # Files written by a container may belong to the container user: with rootless Podman they show
            # up on the host with a "sub" UID (e.g. 100999) and your user cannot delete them; with Docker
            # (older downloads) they may belong to another UID. Delete them from inside a container instead.
            if any(isinstance(e, PermissionError) for _, e in errors) and RUNTIME:
                t.log(tr("Some files were created by the container: deleting them through it…"), "warn")
                if RUNTIME == "podman":
                    t.run_cmd(["podman", "unshare", "rm", "-rf", path])
                else:
                    parent, name = os.path.split(path)
                    t.run_cmd(["docker", "run", "--rm", "--user", "0", "-v", f"{parent}:/target:z",
                               "--entrypoint", "rm", OVMS_IMAGE, "-rf", f"/target/{name}"])
                if not os.path.exists(path):
                    return []
            return errors or [(path, OSError(tr("the folder still exists")))]

        def done(errors):
            if not errors:
                for row, model, _ in self.model_rows.copy():
                    if model.currentData() == rel:
                        self._remove_server_row(row)
            self.refresh_local_models()
            if not errors:
                self.log(tr("Model deleted: {model}").format(model=rel), "ok")
                return
            for failed_path, exc in errors:
                self.log(tr("Cannot delete {path}: {error}").format(
                    path=failed_path, error=getattr(exc, "strerror", None) or exc), "err")
            details = "\n".join(f"• {p}: {getattr(e, 'strerror', None) or e}" for p, e in errors[:5])
            if len(errors) > 5:
                details += "\n" + tr("… and {n} more (see the console)").format(n=len(errors) - 5)
            hint = (f"podman unshare rm -rf \"{path}\"" if RUNTIME == "podman" else f"sudo rm -rf \"{path}\"")
            QMessageBox.warning(self, tr("Deletion incomplete"), tr(
                "{model} could not be deleted completely ({n} errors).\n\n{details}\n\nFrom a terminal you can "
                "try:\n{command}").format(model=rel, n=len(errors), details=details, command=hint))

        self.run_task(job, tr("Deleting {model}").format(model=os.path.basename(rel)), on_done=done)

    # --- Server ----------------------------------------------------------------
    def check_server_status(self):
        if any(getattr(t, "is_status", False) for t in self.tasks):
            return
        if RUNTIME is None:
            self.pill.set_state(tr("Podman/Docker missing"), "warn", False)
            return

        def probe(_task):
            try:
                res = subprocess.run(runtime_cmd("ps", "--format", "{{.Names}}"),
                                     capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if res.returncode != 0:
                return "error"
            return CONTAINER_NAME in res.stdout.splitlines()

        def apply(status):
            if status is None:
                self.pill.set_state(tr("{runtime} not responding").format(runtime=RUNTIME_NAME), "warn", False)
            elif status == "error":
                self.pill.set_state(tr("{runtime} error").format(runtime=RUNTIME_NAME), "warn", False)
                self.pill.setToolTip(tr("`{command}` failed. With Docker, make sure your user may use it (docker "
                                        "group or rootless Docker).").format(command=f"{RUNTIME} ps"))
            elif status:
                self.pill.set_state(tr("RUNNING · port {port}").format(port=OVMS_PORT), "ok", True)
                label = (tr("{n} models").format(n=len(self.served_names)) if len(self.served_names) > 1
                         else next(iter(self.served_names), "—"))
                self.chip_model.set(label, kind="accent", tooltip="\n".join(self.served_names))
                return
            else:
                self.pill.set_state(tr("STOPPED"), "err", False)
            self.chip_model.set(tr("No model served"), kind="neutral", tooltip="")

        task = Task(probe, self)
        task.is_status = True
        self.tasks.add(task)
        task.result.connect(apply)
        task.finished.connect(lambda: (self.tasks.discard(task), task.deleteLater()))
        task.start()

    def start_server(self):
        if not self.btn_start.isEnabled() or not self.btn_stop.isEnabled() or not self.require_runtime():
            return
        models = {}
        for _, model, hardware in self.model_rows:
            path = model.currentData()
            if path not in self.local_models or not (Path(MODELS_DIR) / path).is_dir():
                self.log(tr("Model folder missing: {model}").format(model=path), "err")
                return
            if path in models:
                self.log(tr("Select {model} only once.").format(model=path), "err")
                return
            models[path] = hardware.currentData()
        if not models:
            self.log(tr("No model selected. Download one from the Explore section."), "err")
            return
        args = ["--rest_port", str(OVMS_PORT), "--config_path", "/deployment/config.json"]
        if self.chk_debug.isChecked():
            args += ["--log_level", "DEBUG"]
        bind = f"{OVMS_HOST}:" if self.chk_local.isChecked() else ""
        self.stop_wait.clear()

        def job(t):
            live = self.deployment_dir
            with file_lock(self.server_lock_path), tempfile.TemporaryDirectory(dir=live.parent) as tmp:
                staged = Path(tmp) / "deployment"
                staged.mkdir(mode=0o755)
                names, chats = prepare_deployment(staged, models, t)
                if self.stop_wait.is_set():
                    return
                t.log(tr("Stopping any existing container…"))
                if not self._remove_server(t):
                    return
                if live.exists():
                    live.rename(Path(tmp) / "previous")
                staged.rename(live)
                cmd = container_run(*args, devices=device_flags(models.values()), detach=True, name=CONTAINER_NAME,
                                    ports=[f"{bind}{OVMS_PORT}:{OVMS_PORT}"], deployment=live)
                if t.run_cmd(cmd) != 0:
                    t.log(tr("Error starting the container."), "err")
                    return
                self.served_names, self.chat_names = names, chats
                save_state(served_names=names, chat_names=chats)
            self._wait_until_ready(t, names, npu_requested="NPU" in models.values())

        self.run_task(job, tr("Starting server"), [self.btn_start], lambda _: self.check_server_status())

    def _remove_server(self, t):
        res = subprocess.run(runtime_cmd("ps", "-a", "--format", "{{.Names}}"), capture_output=True, text=True)
        if res.returncode:
            t.log(res.stderr.strip() or tr("{runtime} error").format(runtime=RUNTIME_NAME), "err")
            return False
        if CONTAINER_NAME in res.stdout.splitlines():
            return (t.run_cmd(runtime_cmd("stop", CONTAINER_NAME)) == 0
                    and t.run_cmd(runtime_cmd("rm", CONTAINER_NAME)) == 0)
        return True

    def _wait_until_ready(self, t, names, timeout=600, *, npu_requested=False):
        """Follow container logs until every requested model is ready or the container exits."""
        printed = 0
        last_status = 0
        start = time.time()
        pending = list(names)
        while time.time() - start < timeout and not self.stop_wait.is_set():
            # state first, then logs: if the container just died, the logs read afterwards are complete
            try:
                state = subprocess.run(runtime_cmd("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}",
                                                   CONTAINER_NAME),
                                       capture_output=True, text=True, timeout=30).stdout.split()
                # `logs` writes to stderr what the container writes to stderr: merge them
                logs = subprocess.run(runtime_cmd("logs", CONTAINER_NAME), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, timeout=30).stdout
            except subprocess.TimeoutExpired:
                state, logs = ["true"], ""
            lines = [line for line in logs.splitlines() if line.strip()]
            for line in lines[printed:]:
                low = line.lower()
                level = "err" if any(k in low for k in ("[error]", "[critical]", "error:", "exception")) else "info"
                t.log("  " + line.strip(), level)
                if npu_requested and "available devices for open vino:" in low:
                    devices = low.partition("available devices for open vino:")[2]
                    if not re.search(r"\bnpu\b", devices) and selinux_restricts_container_devices():
                        t.log(tr("OVMS cannot see the selected NPU. Check for SELinux denials on accel0 with "
                                 "sudo ausearch -m AVC -ts recent -i. If SELinux denied OVMS access, run "
                                 "sudo setsebool -P container_use_devices on and restart the server. "
                                 "This setting persists across reboots and applies to all containers on the host."), "warn")
            printed = max(printed, len(lines))

            if not state or state[0] != "true":
                code = state[1] if len(state) > 1 else "?"
                t.log(tr("The OVMS container stopped (exit code {code}): check the errors in the log above.")
                      .format(code=code), "err")
                return

            rest_up, ready, ids = self._probe_ovms(pending)
            for name in ready:
                pending.remove(name)
                t.log(tr("Model ready in {seconds} s! OpenAI endpoint: {url} · model name: {name}").format(
                    seconds=int(time.time() - start), url=f"{OVMS_URL}/v3", name=name), "ok")
            if not pending:
                t.log(tr("In Open WebUI: Admin Settings → Connections → OpenAI API → “+”, URL {url} (if Open WebUI "
                         "runs in a container: {container_url}), any API key.").format(
                    url=f"{OVMS_URL}/v3", container_url=CONTAINER_URL), "ok")
                return

            elapsed = int(time.time() - start)
            if elapsed - last_status >= 15:
                last_status = elapsed
                if not rest_up:
                    what = tr("the REST server on port {port} is not answering yet").format(port=OVMS_PORT)
                else:
                    what = tr("waiting for {models}").format(models=", ".join(pending))
                    if ids:
                        what += " " + tr("(exposed models: {models})").format(models=", ".join(ids))
                t.log(tr("⏳ {seconds} s · {status}. You can interrupt with “Stop”.").format(seconds=elapsed,
                                                                                         status=what), "warn")

            if self.stop_wait.wait(3):
                t.log(tr("Wait interrupted."), "warn")
                return
        if not self.stop_wait.is_set():
            t.log(tr("Still unavailable after {minutes} minutes: {models}").format(
                minutes=timeout // 60, models=", ".join(pending)), "err")

    def _probe_ovms(self, names):
        """Discover generative models once, then check KServe readiness for any other IDs."""
        rest_up, ids = False, []
        try:
            r = requests.get(f"{OVMS_URL}/v3/models", timeout=3)
            rest_up = True
            if r.ok:
                ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
        except (requests.RequestException, ValueError):
            pass
        ready = []
        for name in names:
            if name in ids:
                ready.append(name)
                continue
            try:
                r = requests.get(f"{OVMS_URL}/v2/models/{name}/ready", timeout=3)
                rest_up = True
                if r.status_code == 200:
                    ready.append(name)
            except requests.RequestException:
                pass
        return rest_up, ready, ids

    def show_container_logs(self):
        if not self.require_runtime():
            return

        def job(t):
            t.log(tr("Last lines of the {container} log").format(container=CONTAINER_NAME), "head")
            res = subprocess.run(runtime_cmd("logs", "--tail", "80", CONTAINER_NAME),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in res.stdout.splitlines():
                if line.strip():
                    t.log("  " + line.strip(), "err" if "[error]" in line.lower() else "info")

        self.run_task(job, tr("Reading container log"))

    def stop_server(self):
        if not self.btn_stop.isEnabled() or not self.require_runtime():
            return
        self.stop_wait.set()  # interrupts a wait for the model in progress

        def job(t):
            t.log(tr("Stopping the server…"), "head")
            with file_lock(self.server_lock_path):
                if self._remove_server(t):
                    t.log(tr("Server stopped."), "ok")

        self.run_task(job, tr("Stopping server"), [self.btn_stop], lambda _: self.check_server_status())

    def test_api(self):
        """Test the API the way Open WebUI would: model list + a real chat request."""
        base = f"{OVMS_URL}/v3"

        names, chat_names = self.served_names.copy(), self.chat_names.copy()

        def job(t):
            t.log(tr("OVMS OpenAI API test"), "head")
            rest_up, ready, ids = self._probe_ovms(names)
            if not rest_up:
                t.log(tr("The server is not listening: start it or check “Container log”."), "err")
                return
            for name in names:
                if name in ready:
                    t.log(tr("{model} is ready.").format(model=name), "ok")
                else:
                    t.log(tr("{model} is unavailable.").format(model=name), "err")
            if not names:
                t.log(", ".join(ids) or tr("No model exposed: Open WebUI will not see anything."),
                      "ok" if ids else "warn")
            name = next((name for name in chat_names if name in ready), None)
            if not name:
                return

            payload = {"model": name, "max_tokens": 512,
                       "messages": [{"role": "user", "content": tr("Hi! Answer with a single sentence.")}]}
            t.log(tr("Sending a chat request to “{name}” (the first one can be slow)…").format(name=name))
            started = time.time()
            try:
                r = requests.post(f"{base}/chat/completions", json=payload, timeout=300)
            except requests.RequestException as e:
                t.log(f"✗ /chat/completions: {e}", "err")
                return
            if not r.ok:
                t.log(f"✗ /chat/completions → HTTP {r.status_code}: {r.text[:500]}", "err")
                return
            try:
                data = r.json()
                message = data["choices"][0]["message"]
                answer = message.get("content") or ""
                if not answer.strip() and message.get("reasoning_content"):
                    answer = tr("(reasoning only, the answer has not started yet)") + " " + message["reasoning_content"]
                usage = data.get("usage", {})
            except (ValueError, KeyError, IndexError):
                t.log("✗ " + tr("unexpected answer: {text}").format(text=r.text[:500]), "err")
                return
            t.log("✓ " + tr("answer in {seconds} s ({tokens} tokens): {answer}").format(
                seconds=f"{time.time() - started:.1f}", tokens=usage.get("completion_tokens", "?"),
                answer=answer.strip()[:300]), "ok")
            t.log(tr("The API works. In Open WebUI use {url}, or {container_url} if Open WebUI runs in a container.")
                  .format(url=f"{OVMS_URL}/v3", container_url=CONTAINER_URL), "ok")

        self.run_task(job, tr("API test"))

    # --- HF catalog ------------------------------------------------------------
    def load_catalog(self, force=False):
        def job(_t):
            return fetch_official(force)

        def done(res):
            self.official, cached = res
            src = tr("cache") if cached else "Hugging Face"
            self.log(tr("OpenVINO catalog loaded from {source}: {n} models.").format(source=src, n=len(self.official)),
                     "ok")
            self._rebuild_catalog()

        def fallback(_msg):
            cached = read_cached_catalog()
            if cached is not None:
                self.official = cached
                self.log(tr("Network not available, using the cached catalog."), "warn")
            else:
                self.log(tr("Cannot load the Hugging Face catalog (offline?). Press “Refresh catalog” to retry."),
                         "warn")
            self._rebuild_catalog()

        task = self.run_task(job, tr("Loading the Hugging Face catalog"), [self.btn_refresh_hf], done)
        task.failed.connect(fallback)
        if self.chk_community.isChecked():
            self._fetch_community()

    def _fetch_community(self):
        query = self.search.text().strip()

        def done(rows):
            self.community = rows
            self._rebuild_catalog()
            found = (tr("Community: {n} OpenVINO repos found for “{query}”") if query else
                     tr("Community: {n} OpenVINO repos found")).format(n=len(rows), query=query)
            self.log(found + " · " + tr("{n} visible with the current filters.").format(n=self.proxy.rowCount()),
                     "ok")

        self.run_task(lambda _t: fetch_community(query), tr("Searching community repos"), on_done=done)

    def _rebuild_catalog(self):
        rows = self.official + (self.community if self.chk_community.isChecked() else [])
        self.catalog_loaded = True
        self.catalog.set_rows(rows)
        self._apply_filters(auto=True)

    def _on_community_toggled(self, on):
        if on:
            self._fetch_community()
        else:
            self._rebuild_catalog()

    def _on_search(self, text):
        self.proxy.search = text.strip()
        self._apply_filters(auto=True)
        if self.chk_community.isChecked():
            # debounce: query HF only when the user stops typing
            if not hasattr(self, "_search_timer"):
                self._search_timer = QTimer(self, singleShot=True, interval=600, timeout=self._fetch_community)
            self._search_timer.start()

    def _apply_filters(self, auto=False):
        """auto=True when search/data change: if the selected type has no results
        but other types do, switch to "All types" automatically."""
        self.proxy.quants = {q for q, b in self.quant_chips.items() if b.isChecked()}
        counts = {}
        for m in self.catalog.rows:
            if self.proxy.matches(m):
                counts[m["category"]] = counts.get(m["category"], 0) + 1
        total = sum(counts.values())
        category = self.cb_category.currentData()
        if auto and category and not counts.get(category) and total:
            self.cb_category.blockSignals(True)
            self.cb_category.setCurrentIndex(0)
            self.cb_category.blockSignals(False)
            category = None
        for i in range(self.cb_category.count()):
            key = self.cb_category.itemData(i)
            label = tr(CATEGORIES[key][0]) if key else tr("All types")
            self.cb_category.setItemText(i, f"{label}  ({counts.get(key, 0) if key else total})")
        self.proxy.category = category
        self.proxy.invalidate()
        visible = self.proxy.rowCount()
        if visible == 0:
            if total:
                self.empty_hint.setText(tr("No results in “{type}”.\n{n} models match in other types: choose "
                                           "“All types”.").format(type=tr(CATEGORIES[category][0]), n=total))
            elif self.catalog.rowCount():
                self.empty_hint.setText(tr("No model matches the search or the filters."))
            elif getattr(self, "catalog_loaded", False):
                self.empty_hint.setText(tr("The catalog could not be loaded. Check your connection and press "
                                           "“Refresh catalog”."))
            else:
                self.empty_hint.setText(tr("Loading the catalog…"))
            self.empty_hint.setGeometry(self.table.viewport().rect().adjusted(20, 20, -20, -20))
            self.empty_hint.show()
        else:
            self.empty_hint.hide()
        # redundant columns hidden to leave room for the model name
        self.table.setColumnHidden(2, self.proxy.category is not None)
        self.table.setColumnHidden(3, not self.chk_community.isChecked())
        self._fit_name_column()
        self.lbl_count.setText(tr("{n} of {total} models").format(n=self.proxy.rowCount(),
                                                                   total=self.catalog.rowCount()))

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == event.Type.Resize:
            self._fit_name_column()
            self.empty_hint.setGeometry(obj.rect().adjusted(20, 20, -20, -20))
        return super().eventFilter(obj, event)

    def _fit_name_column(self):
        """The Model column takes the free space, but never less than 260px (then the scrollbar appears)."""
        hh = self.table.horizontalHeader()
        others = sum(hh.sectionSize(c) for c in range(1, self.catalog.columnCount()) if not hh.isSectionHidden(c))
        hh.resizeSection(0, max(260, self.table.viewport().width() - others))

    def _palette_commands(self, query):
        """What the command palette offers: pages, server actions, local models and a direct catalog search."""
        C_ = kit.Command
        go = tr("Go to")
        commands = [C_(tr(label), lambda i=i: self.nav.setCurrentRow(i), go, icon=self.nav_icons[i])
                    for i, label in enumerate(NAV_PAGES)]
        server = tr("Server")
        help_ = tr("Help")
        commands += [
            C_(tr("Check for updates"), self.check_updates_now, help_, icon="download"),
            C_(tr("Turn automatic update checks off") if self.updates.enabled else
               tr("Turn automatic update checks on"), self.toggle_update_checks, help_, icon="settings"),
            C_(tr("Start server"), self.start_server, server, icon="play"),
            C_(tr("Stop"), self.stop_server, server, icon="stop", keywords="server"),
            C_(tr("Test API"), self.test_api, server, icon="flask"),
            C_(tr("Container log"), lambda: (self.toggle_console(True), self.show_container_logs()), server, icon="log"),
            C_(tr("Copy the endpoint URL"), lambda: self._copy(f"{OVMS_URL}/v3"), server, icon="copy"),
            C_(tr("Show or hide the console"), self.toggle_console, tr("Console"), icon="chevron_up"),
            C_(tr("Refresh catalog"), lambda: self.load_catalog(force=True), tr("Explore Hugging Face"), icon="refresh"),
            C_(tr("Refresh"), self.refresh_local_models, tr("Local library"), icon="refresh"),
        ]
        for code, name in THEMES:
            commands.append(C_(tr("Theme: {name}").format(name=tr(name)),
                               lambda c=code: self.cb_theme.setCurrentIndex(self.cb_theme.findData(c)),
                               tr("Theme"), icon="theme"))
        for rel in self.local_models:
            commands.append(C_(tr("Use in server: {model}").format(model=rel),
                               lambda r=rel: self._use_server_model(r),
                               tr("Local library"), icon="server"))
        if query:
            def search(q=query):
                self.nav.setCurrentRow(1)
                self.search.setText(q)
            commands.append(C_(tr("Search the catalog: {query}").format(query=query), search,
                               tr("Explore Hugging Face"), icon="search", score=0.5))
        return commands

    def _on_select(self, current, _prev):
        self.chip_ram.hide()
        if not current.isValid():
            return
        m = self.proxy.data(current, Qt.UserRole + 1)
        self.selected_model = m
        self.d_title.setText(m["name"])
        self.d_repo.setText(m["id"])
        quant = f"{m['quant']} {m['variant']}".strip()
        qc = QUANT_COLORS.get(m["quant"], "#64748b")
        official = (f"<span style='color:{C['accent']}'>✓ {tr('Official OpenVINO')}</span>" if m["official"]
                    else f"<span style='color:{C['warn']}'>{tr('Community')}</span>")
        self.d_badges.setText(f"<span style='color:{qc};font-weight:700'>■ {quant}</span>"
                              f"&nbsp;&nbsp;·&nbsp;&nbsp;{official}")
        label, task, _ = CATEGORIES[m["category"]]
        rows = [
            (tr("Type"), tr(label) + (f" <span style='color:{C['muted']}'>({m['pipeline']})</span>"
                                      if m["pipeline"] else "")),
            (tr("OVMS task"), task or f"<span style='color:{C['warn']}'>{tr('not supported by --pull')}</span>"),
            (tr("Base model"), m["base_model"] or "—"),
            (tr("Updated"), rel_time(m["updated"])),
            (tr("Size"), tr("computing…")),
        ]
        self._detail_rows = rows
        self._render_detail()
        self.btn_download.setEnabled(task is not None)
        self.btn_hf.setEnabled(True)
        self._fetch_size(m["id"])

    def _show_ram_fit(self, size):
        """At a glance: does the model fit in this computer's memory?"""
        try:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            ram = 0
        if not size or not ram:
            self.chip_ram.hide()
            return
        ratio = size / ram
        text, kind = ((tr("Fits in your {ram} of RAM"), "ok") if ratio <= 0.6 else
                      (tr("Tight for your {ram} of RAM"), "warn") if ratio <= 0.9 else
                      (tr("Larger than your {ram} of RAM"), "err"))
        self.chip_ram.set(text.format(ram=human_size(ram)), kind=kind)
        self.chip_ram.show()

    def _render_detail(self):
        self.d_info.hide()
        while self.d_grid.count():
            self.d_grid.takeAt(0).widget().deleteLater()
        for r, (k, v) in enumerate(self._detail_rows):
            key = QLabel(k)
            key.setObjectName("muted")
            val = QLabel(v)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.d_grid.addWidget(key, r, 0, Qt.AlignTop)
            self.d_grid.addWidget(val, r, 1)

    def _fetch_size(self, repo_id):
        def job(_t):
            r = requests.get(f"{HF_API}/{repo_id}", params=[("expand[]", "usedStorage")], timeout=15)
            r.raise_for_status()
            return repo_id, r.json().get("usedStorage")

        def done(res):
            rid, size = res
            if self.selected_model and self.selected_model["id"] == rid:
                self._detail_rows[-1] = (tr("Size"), f"≈ {human_size(size)}" if size else "—")
                self._render_detail()
                self._show_ram_fit(size)

        task = Task(job, self)
        self.tasks.add(task)
        task.result.connect(done)
        task.finished.connect(lambda: (self.tasks.discard(task), task.deleteLater()))
        task.start()

    def open_on_hf(self):
        if self.selected_model:
            webbrowser.open(f"https://huggingface.co/{self.selected_model['id']}")

    def download_selected(self):
        m = self.selected_model
        if not m:
            return
        task = CATEGORIES[m["category"]][1]
        if not task:
            self.log(tr("{model}: this model type cannot be downloaded with ovms --pull.").format(model=m["id"]),
                     "warn")
            return
        self._download(m["id"], task)

    def download_custom(self):
        repo = self.entry_custom_dl.text().strip()
        if not repo:
            return
        task = CATEGORIES[detect_category(repo, None)][1] or "text_generation"
        self._download(repo, task)

    def _download(self, model_id, ovms_task):
        if not self.require_runtime():
            return

        def job(t):
            t.log(tr("Download · {model}").format(model=model_id), "head")
            t.log(tr("Task: {task} · Destination: {folder}").format(task=ovms_task, folder=MODELS_DIR))
            ret = t.run_cmd(container_run("--pull", "--source_model", model_id,
                                          "--model_repository_path", "/models", "--task", ovms_task,
                                          as_owner=True))
            if ret == 0:
                t.log(tr("Download complete! The model is in {folder}.").format(folder=MODELS_DIR), "ok")
            else:
                t.log(tr("Download error (exit code {code}).").format(code=ret), "err")
            return ret

        self.run_task(job, tr("Downloading {model}").format(model=model_id.split("/")[-1]),
                      [self.btn_download, self.btn_download_custom], lambda _: self.refresh_local_models())

    # --- Converter -------------------------------------------------------------
    def _precision(self):
        return self.prec_group.checkedButton().property("prec")

    def _suggest_output_name(self):
        if not hasattr(self, "entry_output_name"):
            return
        base = self.entry_hf_id.text().strip().split("/")[-1].lower()
        if base:
            self.entry_output_name.setText(f"{base}-{self._precision()}-ov")

    def start_conversion(self):
        hf_id = self.entry_hf_id.text().strip()
        prec = self._precision()
        out_name = self.entry_output_name.text().strip()
        if not hf_id or not out_name:
            self.log(tr("Fill in every field before starting the conversion."), "err")
            return
        dest_path = os.path.normpath(os.path.join(MODELS_DIR, out_name))
        if not dest_path.startswith(MODELS_DIR + os.sep):
            self.log(tr("The destination must be a folder inside {folder}.").format(folder=MODELS_DIR), "err")
            return
        if not find_optimum():
            self.log(tr("optimum-cli not found. Run install.sh --with-optimum (or install optimum[openvino] in the "
                        "app's environment) and restart OVMS Manager."), "err")
            return
        trust = self.chk_trust.isChecked()
        cli_args = ["export", "openvino", "--model", hf_id, "--weight-format", prec]
        if trust:
            cli_args.append("--trust-remote-code")
        cli_args.append(dest_path)
        cmd = optimum_command(cli_args)

        def job(t):
            t.log(tr("Optimum conversion · {model} → {precision}").format(model=hf_id, precision=prec.upper()),
                  "head")
            t.log(tr("Destination: {path}").format(path=dest_path))
            if trust:
                t.log(tr("Custom code from the repository allowed (--trust-remote-code)."), "warn")
            t.log(f"Python: {cmd[0]}")
            ret = t.run_cmd(cmd, display="optimum-cli " + " ".join(cli_args), env=NO_TELEMETRY_ENV)
            if ret == 0:
                t.log(tr("Conversion completed successfully!"), "ok")
                return
            output = "\n".join(t.output_tail)
            if "trust_remote_code" in output and not trust:
                t.log(tr("Conversion failed: {model} contains custom Python code. If you trust the repository, "
                         "enable “Allow custom code” in Advanced options and try again.").format(model=hf_id), "err")
            elif re.search(r"not supported|unsupported|is not (yet )?supported|Unrecognized configuration", output,
                           re.IGNORECASE):
                t.log(tr("Conversion failed: this architecture is probably not supported by optimum-intel. "
                         "Look for an already converted version of the model in the Explore section."), "err")
            else:
                t.log(tr("Conversion failed (exit code {code}): check the error in the log above.").format(code=ret),
                      "err")

        self.run_task(job, tr("Optimum conversion"), [self.btn_convert], lambda _: self.refresh_local_models())

    def closeEvent(self, event):
        running = [t for t in self.jobs if t.isRunning()]
        if running:
            ok = QMessageBox.question(self, tr("Operations in progress"),
                                      tr("Some operations are still running (download/conversion). Quit anyway?"),
                                      QMessageBox.Yes | QMessageBox.No)
            if ok != QMessageBox.Yes:
                event.ignore()
                return
        self.status_timer.stop()
        for t in list(self.tasks):
            t.wait(3000)
        event.accept()
        if any(t.isRunning() for t in self.tasks):
            os._exit(0)  # threads blocked on a subprocess cannot be interrupted cleanly


def init_theme(app):
    """Fusion style, bundled fonts and the light/dark theme saved in state.json (default: follow the system)."""
    global THEME
    state = load_state()
    app.setStyle("Fusion")
    kit.load_fonts(app)
    StatusPill.reduce_motion = state.get("reduce_motion", kit.reduce_motion_default())
    THEME = kit.ThemeManager(app, ACCENT, state.get("theme", "system"), app_qss)
    return THEME


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("OVMS Manager")
    # must match the name of the .desktop file created by install.sh: that way GNOME/KDE associate
    # the window with the menu entry (right icon in the dock)
    app.setDesktopFileName(APP_ID)
    setup_language(app, load_state().get("ui_language", ""))
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{APP_ID}.svg")
    app.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon(icon_path)))
    init_theme(app)
    win = OVMSManager()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
