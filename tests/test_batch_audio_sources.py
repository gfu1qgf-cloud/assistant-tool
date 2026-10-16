import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.audio_sources import pool_voice, resolve_voice
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, add_pool_paths
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.pools import apply_pairs, pairing_plan, new_copy
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.engine import choose_music, render_static_text, probe, resolve_tools
from app_plugins.builtin.batch_text_video.playback import preview_key, PlaybackWorker


class AudioSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def fixture(self, root):
        state = fresh_state()
        pools = [create_pool(state, name, "voice") for name in ("背景人声", "另一个人声池")]
        for index, pool in enumerate(pools):
            for number in range(2):
                path = Path(root) / f"voice-{index}-{number}.wav"
                path.write_bytes(b"fixture")
                add_pool_paths(pool, [path])
        video = Path(root) / "video.mp4"
        video.write_bytes(b"fixture")
        state["copy_pool"] = [new_copy(), new_copy(), new_copy()]
        state["backgrounds"] = [str(video)]
        apply_pairs(state, pairing_plan(state["copy_pool"], state["backgrounds"]))
        state["settings"].update(use_voice=True, voice_pool_mode="rotate", voice_pool_id=pools[0]["id"])
        return state, pools

    def test_default_pool_resolves_consistently_without_consuming_or_rewriting(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            before = copy.deepcopy(state)
            self.assertEqual([resolve_voice(j, state) for j in state["jobs"]],
                             [pools[0]["items"][i % 2]["path"] for i in range(3)])
            self.assertEqual(resolve_voice(state["copy_pool"][1], state), resolve_voice(state["jobs"][1], state))
            self.assertEqual(state, before)
            state["settings"]["voice_pool_mode"] = "single"
            self.assertTrue(all(resolve_voice(j, state) == pools[0]["items"][0]["path"] for j in state["jobs"]))

    def test_per_row_file_and_no_voice_override_default(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            job = state["jobs"][0]
            job.update(voice_override=True, voice_path=pools[1]["items"][1]["path"])
            self.assertEqual(resolve_voice(job, state), pools[1]["items"][1]["path"])
            job["voice_path"] = ""
            self.assertEqual(resolve_voice(job, state, discover=True), "")
            state["settings"]["use_voice"] = False
            job["voice_path"] = pools[1]["items"][1]["path"]
            self.assertEqual(resolve_voice(job, state), "")

    def test_task_source_and_missing_pool_are_not_silently_guessed(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            job = state["jobs"][0]
            job.update(task_voice_path="original.wav", voice_path="replacement.wav")
            state["settings"]["voice_pool_mode"] = "task"
            self.assertEqual(resolve_voice(job, state), "original.wav")
            state["settings"].update(voice_pool_mode="rotate", voice_pool_id="missing")
            with self.assertRaises(ValueError):
                resolve_voice(job, state)
            with self.assertRaises(ValueError):
                pool_voice(state, create_pool(state, "空池", "voice")["id"])
            Path(pools[0]["items"][0]["path"]).unlink()
            with self.assertRaises(FileNotFoundError):
                pool_voice(state, pools[0]["id"])

    def test_legacy_settings_load_without_touching_state_file(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            for key in ("voice_pool_id", "voice_pool_mode", "music_enabled"):
                del state["settings"][key]
            text = json.dumps(state)
            store.path.write_text(text, encoding="utf8")
            loaded = store.load()
            self.assertEqual((loaded["settings"]["voice_pool_id"], loaded["settings"]["voice_pool_mode"],
                              loaded["settings"]["music_enabled"]), ("", "task", True))
            self.assertEqual(store.path.read_text("utf8"), text)

    def test_disabled_music_does_not_prepare_or_consume_pool(self):
        state = fresh_state()
        state["settings"].update(music_enabled=False, music_pool_id="missing")
        with patch("app_plugins.builtin.batch_text_video.engine.clean_music") as prepare:
            self.assertIsNone(choose_music(state, 10, "unused", "ffmpeg", "ffprobe", None, print))
            prepare.assert_not_called()

    def test_preview_key_tracks_actual_voice_file(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            job = state["jobs"][0]
            before = preview_key(job, state)
            Path(pools[0]["items"][0]["path"]).write_bytes(b"new voice contents")
            self.assertNotEqual(before, preview_key(job, state))
            state["settings"]["voice_pool_id"] = pools[1]["id"]
            self.assertNotEqual(before, preview_key(job, state))

    def test_pairing_rows_expose_all_named_pools_without_selecting_top_pool(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            state["settings"].update(voice_pool_mode="task", voice_pool_id="")
            dialog = PairingDialog(state["copy_pool"], state["backgrounds"], state=state)
            combo = dialog.table.cellWidget(1, 4)
            index = combo.findData("pool:" + pools[1]["id"])
            self.assertGreaterEqual(index, 2)
            combo.setCurrentIndex(index)
            self.assertEqual(dialog.pairs[1]["voice_path"], pools[1]["items"][1]["path"])
            self.assertTrue(dialog.pairs[1]["voice_override"])
            self.assertIn("另一个人声池", combo.currentText())
            dialog.deleteLater()

    def test_pairing_bulk_update_and_missing_pool_are_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            dialog = PairingDialog(state["copy_pool"], state["backgrounds"], state=state)
            dialog.apply_row_voices("pool:" + pools[1]["id"], [0, 2])
            self.assertEqual(dialog.pairs[0]["voice_path"], pools[1]["items"][0]["path"])
            self.assertEqual(dialog.pairs[2]["voice_path"], pools[1]["items"][0]["path"])
            before = copy.deepcopy(dialog.pairs)
            Path(pools[1]["items"][1]["path"]).unlink()
            with self.assertRaises(FileNotFoundError):
                dialog.apply_row_voices("pool:" + pools[1]["id"], [0, 1])
            self.assertEqual(dialog.pairs, before)
            dialog.deleteLater()

    def test_empty_pool_row_choice_restores_last_accepted_selection(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            empty = create_pool(state, "空的人声池", "voice")
            dialog = PairingDialog(state["copy_pool"], state["backgrounds"], state=state)
            combo = dialog.table.cellWidget(0, 4)
            combo.setCurrentIndex(combo.findData("pool:" + pools[1]["id"]))
            before = copy.deepcopy(dialog.pairs[0])
            combo.setCurrentIndex(combo.findData("pool:" + empty["id"]))
            self.assertEqual(dialog.pairs[0], before)
            self.assertEqual(combo.currentData(), "pool:" + pools[1]["id"])
            self.assertIn("为空", dialog.status.text())
            dialog.deleteLater()

    def test_main_cards_bind_pool_activate_voice_and_stay_out_of_canvas_layers(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            state, pools = self.fixture(root)
            state["settings"].update(use_voice=False, voice_pool_mode="task", voice_pool_id="")
            store = Store(Path(root) / "store")
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            card = dialog.audio_cards["voice"]
            card.pool.setCurrentIndex(card.pool.findData(pools[1]["id"]))
            card.pool.activated.emit(card.pool.currentIndex())
            self.assertTrue(dialog.controls["use_voice"].isChecked())
            self.assertEqual(card.mode.currentData(), "rotate")
            self.assertEqual(dialog.active_voice(dialog.state["jobs"][1]), pools[1]["items"][1]["path"])
            self.assertIn("音频设置", [section.toggle.text() for section in dialog.project_sections.sections.values()])
            self.assertTrue(all(layer["kind"] not in {"voice", "music"} for layer in dialog.read_settings()["layers"]))
            self.assertTrue(dialog.persist())
            self.assertEqual(store.load()["settings"]["voice_pool_id"], pools[1]["id"])
            self.assertEqual(store.load()["resource_pools"], state["resource_pools"])
            dialog.close()
            dialog.deleteLater()

    def test_profiles_keep_independent_audio_bindings(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            state, pools = self.fixture(root)
            store = Store(Path(root) / "store")
            state = store.save(state, 0)
            first = state["active_profile"]
            second = store.create_profile(state, "其他配置")
            other = second["active_profile"]
            store.select_profile(second, first)
            dialog = BatchTextVideoDialog(store=store)
            dialog.switch_profile(other)
            self.assertEqual(dialog.controls["voice_pool_id"].currentData(), "")
            self.assertFalse(dialog.controls["use_voice"].isChecked())
            dialog.switch_profile(first)
            self.assertEqual(dialog.controls["voice_pool_id"].currentData(), pools[0]["id"])
            self.assertEqual(dialog.controls["voice_pool_mode"].currentData(), "rotate")
            dialog.close()
            dialog.deleteLater()

    def test_legacy_completed_voice_pair_is_not_invalidated_by_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            source = state["copy_pool"][0]
            source["voice_path"] = pools[0]["items"][0]["path"]
            job = state["jobs"][0]
            job.update(voice_path=source["voice_path"], status="已完成")
            job.pop("task_voice_path", None)
            self.assertEqual(apply_pairs(state, pairing_plan(state["copy_pool"], state["backgrounds"])), (0, 0))
            self.assertEqual(job["status"], "已完成")

    def test_real_ffmpeg_preview_and_export_follow_same_named_voice_pool(self):
        ffmpeg, ffprobe = resolve_tools()
        def run(*args):
            result = subprocess.run([ffmpeg, "-y", "-v", "error", *args], capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf8", "replace"))
        with tempfile.TemporaryDirectory() as root:
            state, pools = self.fixture(root)
            video = state["backgrounds"][0]
            run("-f", "lavfi", "-i", "color=c=blue:s=160x240:r=10:d=0.6", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", video)
            for number, item in enumerate(pools[0]["items"]):
                run("-f", "lavfi", "-i", f"sine=frequency=440:duration={1.2+number*.8}", "-c:a", "pcm_s16le", item["path"])
            state["settings"].update(aspect="source", loop_transition="none", overwrite=True, music_enabled=False)
            job = state["jobs"][1]
            job.update(title="Test", body="A", name="export", task_dir=root)
            before = copy.deepcopy(state)
            target, plan = render_static_text(job, state, Path(root) / "cache")
            self.assertIsNone(plan)
            self.assertAlmostEqual(probe(target, ffprobe)["duration"], 2, delta=.12)
            key = preview_key(job, state)
            results = []
            worker = PlaybackWorker(job, state, Path(root) / "cache", key)
            worker.ready.connect(results.append)
            worker.run()
            self.assertEqual(len(results), 1)
            self.assertNotIn("error", results[0], results[0])
            self.assertAlmostEqual(results[0]["duration"], 2, delta=.12)
            self.assertEqual(state, before)
            worker.deleteLater()


if __name__ == "__main__":
    unittest.main()
