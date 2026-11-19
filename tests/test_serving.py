# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-model deployment and Server page regressions. Run with python -m unittest discover -s tests."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ovms_manager as ovms


def graph(options, fields):
    return f'node {{ node_options {{ [type.googleapis.com/mediapipe.{options}] {{ {fields} }} }} }}'


class ServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = ovms.QApplication.instance() or ovms.QApplication([])
        with patch.object(ovms, "load_state", return_value={}):
            ovms.init_theme(cls.app)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.models = self.root / "models"
        self.models.mkdir()
        patches = patch.multiple(ovms, MODELS_DIR=str(self.models), DATA_DIR=str(self.root),
                                 STATE_FILE=str(self.root / "state.json"), CACHE_DIR=str(self.root))
        patches.start()
        self.addCleanup(patches.stop)
        self.task = Mock()

    def add_model(self, name, **files):
        path = self.models / name
        path.mkdir(parents=True)
        for filename, content in files.items():
            (path / filename).write_text(content)
        return path

    def window(self):
        patches = patch.multiple(ovms.OVMSManager, load_catalog=Mock(), check_server_status=Mock())
        patches.start()
        self.addCleanup(patches.stop)
        win = ovms.OVMSManager()
        self.addCleanup(win.close)
        return win

    def test_deployment_devices_ids_and_original_files(self):
        embeddings = graph("EmbeddingsCalculatorOVOptions", 'models_path: "." target_device: "CPU" pooling: MEAN')
        whisper = graph("S2tCalculatorOptions", """plugin_config: '{"label":"target_device: }"}'""")
        self.add_model("search/shared-ov", **{"graph.pbtxt": embeddings})
        audio = self.add_model("audio/shared", **{"graph.pbtxt": whisper})
        self.add_model("shared-2", **{"ir.xml": ""})
        modes = {"search/shared-ov": "GPU", "audio/shared": "NPU", "shared-2": "CPU"}

        output = self.root / "deployment"
        output.mkdir()
        ids, chats = ovms.prepare_deployment(output, modes, self.task)
        self.assertEqual(ids, ["shared", "shared-3", "shared-2"])
        self.assertEqual(chats, [])
        config = json.loads((output / "config.json").read_text())
        search, asr, classic = [entry["config"] for entry in config["model_config_list"]]
        self.assertEqual(search["base_path"], "/models/search/shared-ov")
        self.assertEqual((output / Path(search["graph_path"]).name).read_text(),
                         embeddings.replace('target_device: "CPU"', 'target_device: "GPU"'))
        asr_graph = (output / Path(asr["graph_path"]).name).read_text()
        self.assertIn('target_device: "NPU"', asr_graph)
        self.assertIn('{"label":"target_device: }"}', asr_graph)
        self.assertEqual((audio / "graph.pbtxt").read_text(), whisper)
        self.assertEqual(classic, {
            "name": "shared-2", "base_path": "/models/shared-2/ir.xml", "target_device": "CPU"})
        self.assertEqual(ovms.device_flags(modes.values()), ["--device", "/dev/dri", "--device", "/dev/accel:/dev/accel"])
        self.assertEqual(ovms.device_flags(["CPU"]), [])

    def test_qwen_embedding_npu_length(self):
        rel = "OpenVINO/Qwen3-Embedding-0.6B-int8-ov"
        original = graph("EmbeddingsCalculatorOVOptions", 'models_path: "." target_device: "CPU"')
        source = self.add_model(rel, **{"graph.pbtxt": original})
        output = self.root / "deployment"
        output.mkdir()
        for mode, existing, expected in (("NPU", "", ["1000"]), ("NPU", "max_length: 512", ["512"]),
                                         ("GPU", "", [])):
            with self.subTest(mode=mode, existing=existing):
                contents = original.replace("models_path:", existing + " models_path:")
                (source / "graph.pbtxt").write_text(contents)
                ovms.prepare_deployment(output, {rel: mode}, self.task)
                deployed = (output / "graph-0.pbtxt").read_text()
                self.assertEqual(re.findall(r"max_length: (\d+)", deployed), expected)
                self.assertIn(f'target_device: "{mode}"', deployed)
                self.assertEqual((source / "graph.pbtxt").read_text(), contents)

    def test_graph_generation_uses_local_metadata_and_selected_device(self):
        source = self.add_model("converted", **{"config.json": "{}", "openvino_model.xml": ""})
        output = self.root / "deployment"
        output.mkdir()

        def configure(cmd):
            self.assertIn("--configure", cmd)
            self.assertEqual(cmd[cmd.index("--target_device") + 1], "GPU")
            self.assertEqual(cmd[cmd.index("--model_path") + 1], "/deployment/0")
            view = output / "0"
            self.assertEqual((view / "config.json").readlink(), Path("/models/converted/config.json"))
            (view / "graph.pbtxt").write_text(graph("LLMCalculatorOptions", 'device: "GPU"'))
            return 0

        self.task.run_cmd.side_effect = configure
        ids, chats = ovms.prepare_deployment(output, {"converted": "GPU"}, self.task)
        self.assertEqual(ids, ["converted"])
        self.assertEqual(chats, ids)
        self.assertFalse((source / "graph.pbtxt").exists())

    def test_saved_rows_start_and_reconfigure(self):
        self.add_model("search", **{"graph.pbtxt": graph("EmbeddingsCalculatorOVOptions", "")})
        self.add_model("voice", **{"graph.pbtxt": graph("S2tCalculatorOptions", "")})
        ovms.save_state(served_name="voice")
        win = self.window()
        self.assertEqual(win.model_rows[0][1].currentData(), "voice")
        win.btn_add_model.click()
        for row, mode in zip(win.model_rows, ("NPU", "GPU")):
            row[2].setCurrentIndex(row[2].findData(mode))
        win.refresh_local_models()
        self.assertEqual([(m.currentData(), hw.currentData()) for _, m, hw in win.model_rows],
                         [("voice", "NPU"), ("search", "GPU")])
        win.chk_local.setChecked(True)
        win.chk_debug.setChecked(True)
        self.task.run_cmd.return_value = 0

        # Run the actual button handlers, stubbing only the runtime, worker scheduling and network wait.
        with patch.object(ovms, "RUNTIME", "podman"), patch.object(win, "_wait_until_ready") as wait, \
                patch.object(win, "run_task", side_effect=lambda job, *args: job(self.task)), \
                patch.object(ovms.subprocess, "run", return_value=Mock(returncode=0, stdout="ovms-server\n")):
            win.btn_start.click()
            self.assertEqual(self.task.run_cmd.call_args_list[0].args[0], ["podman", "stop", "ovms-server"])
            self.assertEqual(self.task.run_cmd.call_args_list[1].args[0], ["podman", "rm", "ovms-server"])
            cmd = self.task.run_cmd.call_args.args[0]
            self.assertIn("127.0.0.1:8000:8000", cmd)
            self.assertIn("DEBUG", cmd)
            self.assertIn("/dev/dri", cmd)
            self.assertIn("/dev/accel:/dev/accel", cmd)
            wait.assert_called_once_with(self.task, ["voice", "search"], npu_requested=True)
            win._remove_server_row(win.model_rows[0][0])
            win.btn_start.click()
            cmd = self.task.run_cmd.call_args.args[0]
            self.assertNotIn("/dev/accel:/dev/accel", cmd)
            deployed = json.loads((self.root / ovms.APP_ID / "deployment" / "config.json").read_text())
            self.assertEqual([entry["config"]["name"] for entry in deployed["model_config_list"]], ["search"])
            self.assertEqual(ovms.load_state()["server_rows"], [["search", "GPU"]])

    def test_prepare_failure_keeps_running_server(self):
        bad = self.add_model("broken", **{"graph.pbtxt": "node { calculator: 'Unknown' }"})
        win = self.window()
        with patch.object(ovms, "RUNTIME", "podman"), patch.object(win, "_remove_server") as remove, \
                patch.object(win, "run_task", side_effect=lambda job, *args: job(self.task)):
            with self.assertRaisesRegex(ValueError, "broken"):
                win.start_server()
            remove.assert_not_called()
            self.task.run_cmd.assert_not_called()
            self.assertTrue((bad / "graph.pbtxt").exists())

    def test_ready_wait_tracks_every_model(self):
        win = self.window()
        running = Mock(stdout="true 0")
        logs = Mock(stdout="")
        with patch.object(ovms.subprocess, "run", side_effect=[running, logs, running, logs]), \
                patch.object(win.stop_wait, "wait", return_value=False), \
                patch.object(win, "_probe_ovms", side_effect=[(True, ["a"], ["a"]), (True, ["b"], [])]) as probe:
            win._wait_until_ready(self.task, ["a", "b"])
            self.assertEqual(probe.call_count, 2)
            messages = [call.args[0] for call in self.task.log.call_args_list]
            self.assertTrue(any("model name: a" in line for line in messages))
            self.assertTrue(any("model name: b" in line for line in messages))

    def test_selinux_hint_for_missing_requested_npu(self):
        win = self.window()
        for requested, devices, restricted, expected in (
                (True, "CPU, GPU", True, True),
                (True, "CPU, NPU.0", True, False),
                (False, "CPU, GPU", True, False),
                (True, "CPU, GPU", False, False)):
            with self.subTest(requested=requested, devices=devices, restricted=restricted):
                self.task.log.reset_mock()
                logs = Mock(stdout=f"[info] Available devices for Open VINO: {devices}\n")
                with patch.object(ovms, "selinux_restricts_container_devices", return_value=restricted), \
                        patch.object(ovms.subprocess, "run", side_effect=[Mock(stdout="false 139"), logs]):
                    win._wait_until_ready(self.task, ["search"], npu_requested=requested)
                hints = [c for c in self.task.log.call_args_list if "setsebool -P" in c.args[0]]
                self.assertEqual(len(hints), int(expected))

    def test_non_chat_api_checks_each_model(self):
        win = self.window()
        win.served_names, win.chat_names = ["search", "voice", "pending"], []
        listed = Mock(ok=True)
        listed.json.return_value = {"data": [{"id": "search"}]}
        with patch.object(ovms.requests, "get", side_effect=[listed, Mock(status_code=200), Mock(status_code=503)]) as get, \
                patch.object(ovms.requests, "post") as post, \
                patch.object(win, "run_task", side_effect=lambda job, *args: job(self.task)):
            win.test_api()
            self.assertEqual([c.args[0].removeprefix(ovms.OVMS_URL) for c in get.call_args_list],
                             ["/v3/models", "/v2/models/voice/ready", "/v2/models/pending/ready"])
            self.task.log.assert_any_call("voice is ready.", "ok")
            self.task.log.assert_any_call("pending is unavailable.", "err")
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
