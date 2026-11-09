import copy
import json
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
import numpy as np
import yaml
from PIL import Image
import test_fusion as fixtures
from funscripter.diagnostic import create_diagnostic
from funscripter.diagnostic_render import DiagnosticRenderer
from funscripter.fuse_project import fuse_project
from funscripter.media import sha256
from funscripter.media_sync import alignment_checks
from funscripter.models import DiagnosticProfile
from funscripter.revisions import archive_revision, validate_external

ROOT = Path(__file__).resolve().parents[1]
FONT = Path("C:/Windows/Fonts/msyh.ttc")


def profile(**changes):
    return DiagnosticProfile(font_path=str(FONT), **changes)


class SyncTests(unittest.TestCase):
    def test_known_offset_noise_and_silence_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            rate = 8000
            values = (np.random.default_rng(42).normal(0,0.1,rate*2)*32767).astype("<i2")
            source = directory / "source.wav"
            with wave.open(str(source),"wb") as stream:
                stream.setparams((1,2,rate,0,"NONE","not compressed"))
                stream.writeframes(values.tobytes())
            decoded = directory / "decoded.f32"
            (values.astype("<f4")/32768).tofile(decoded)
            report = alignment_checks(source,decoded,profile())
            self.assertTrue(report["three_anchor_alignment_passed"])
            self.assertEqual(report["decoded_tail_delta_samples"],0)
            np.concatenate([np.zeros(320,dtype="<f4"),values.astype("<f4")/32768]).tofile(decoded)
            report = alignment_checks(source,decoded,profile())
            self.assertFalse(report["three_anchor_alignment_passed"])
            self.assertAlmostEqual(report["anchor_checks"][1]["lag_ms"],40.0)
            with wave.open(str(source),"wb") as stream:
                stream.setparams((1,2,rate,0,"NONE","not compressed"))
                stream.writeframes(b"\0"*rate*4)
            report = alignment_checks(source,decoded,profile())
            self.assertTrue(all(row["status"]=="uninformative_low_energy" for row in report["anchor_checks"]))

    def test_video_profile_rejects_invalid_dimensions_and_rates(self):
        for changes in [{"width":641},{"fps":0},{"audio_bitrate":"192k; rm -rf"},{"max_alignment_error_ms":200.0}]:
            with self.assertRaises(ValueError):
                profile(**changes)


class DiagnosticRevisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FusionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.project = self.fixture.project_dir
        planning, fusion_profile = self.fixture.publish_inputs()
        self.draft = self.root / "fusion"
        fuse_project(self.project,planning,self.draft,fusion_profile)

    def test_external_original_and_baseline_preserved_without_merge(self):
        edited = self.root / "edited.funscript"
        data = json.loads((self.draft / "track.funscript").read_text(encoding="utf-8"))
        data["actions"][1]["pos"] -= 1
        data["metadata"] = {"editor":"OFS", "note":"中文保留"}
        original = b"\xef\xbb\xbf" + json.dumps(data,ensure_ascii=False,indent=1).encode("utf-8")
        edited.write_bytes(original)
        before = sha256(self.draft / "track.funscript")
        output = self.root / "revision"
        result = archive_revision(self.project,self.draft,edited,output,elapsed_seconds=90.0,notes="模拟外部修订归档")
        self.assertEqual((output / "external_original.funscript").read_bytes(),original)
        self.assertEqual(sha256(output / "baseline.funscript"),before)
        self.assertEqual(sha256(self.draft / "track.funscript"),before)
        self.assertTrue(result["external_format_valid"])
        self.assertTrue(result["format_fields_changed"])
        self.assertFalse(result["protection"]["automatic_merge_allowed"])
        self.assertEqual(result["protection"]["scope"],"entire_external_revision")
        with self.assertRaisesRegex(ValueError,"new directory"):
            archive_revision(self.project,self.draft,edited,output)

    def test_invalid_empty_and_duplicate_timestamp_revision_is_kept_for_review(self):
        edited = self.root / "invalid.funscript"
        edited.write_bytes(b"")
        output = self.root / "invalid-archive"
        result = archive_revision(self.project,self.draft,edited,output)
        self.assertFalse(result["external_format_valid"])
        self.assertEqual((output / "external_original.funscript").read_bytes(),b"")
        raw = json.dumps({"actions":[{"at":0,"pos":50},{"at":0,"pos":40}]}).encode()
        report,_ = validate_external(raw,10000,self.fixture.project.config.device)
        self.assertIn("non_increasing_time",report["errors"])
        report,_ = validate_external(b'{"actions":[{"at":true,"pos":50}]}',10000,self.fixture.project.config.device)
        self.assertFalse(report["format_valid"])
        report,_ = validate_external(b'{"actions":[{"at":0,"pos":50}],"inverted":"false"}',10000,self.fixture.project.config.device)
        self.assertFalse(report["format_valid"])

    @unittest.skipUnless(FONT.is_file(),"Chinese test font unavailable")
    def test_chinese_text_special_characters_and_frame_time_are_literal(self):
        config = profile()
        plan = json.loads((self.draft / "segment_plan.json").read_text(encoding="utf-8"))
        self.fixture.timeline.cues[0].text_raw = '中文 [x] : \" % $ ; 换行\n第二行'
        renderer = DiagnosticRenderer(self.fixture.timeline,plan,self.draft / "preview.png",config,"test-revision")
        frame,info = renderer.frame(0)
        self.assertEqual(frame.size,(1280,720))
        self.assertEqual(info["time_ms"],0)
        self.assertEqual(info["cue_ids"],[self.fixture.cues[0].cue_id])
        _,info = renderer.frame(15)
        self.assertEqual(info["time_ms"],1500)

    @unittest.skipUnless(FONT.is_file(),"Chinese test font unavailable")
    def test_missing_tool_fails_before_publishing_output(self):
        path = self.root / "video.yaml"
        path.write_text(yaml.safe_dump(profile(ffmpeg="missing-ffmpeg-for-test").model_dump(mode="json")),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"executable not found"):
            create_diagnostic(self.project,self.draft,self.root / "diagnostic",path)
        self.assertFalse((self.root / "diagnostic").exists())

    @unittest.skipUnless(FONT.is_file(),"Chinese test font unavailable")
    def test_actual_encoding_binding_probe_and_revision_staleness(self):
        ffmpeg = os.environ.get("FUNSCRIPTER_TEST_FFMPEG") or shutil.which("ffmpeg") or str(ROOT / "workspace/tools/ffmpeg-binaries/ffmpeg.exe")
        ffprobe = os.environ.get("FUNSCRIPTER_TEST_FFPROBE") or shutil.which("ffprobe") or str(ROOT / "workspace/tools/ffmpeg-binaries/ffprobe.exe")
        if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
            self.skipTest("FFmpeg/FFprobe integration tools unavailable")
        config = profile(ffmpeg=ffmpeg,ffprobe=ffprobe,width=640,height=360,fps=5)
        path = self.root / "video.yaml"
        path.write_text(yaml.safe_dump(config.model_dump(mode="json")),encoding="utf-8")
        output = self.root / "diagnostic"
        result = create_diagnostic(self.project,self.draft,output,path)
        self.assertEqual(result["script_sha256"],sha256(output / "track.funscript"))
        self.assertEqual(sha256(output / "track.funscript"),sha256(self.draft / "track.funscript"))
        sync = json.loads((output / "sync_report.json").read_text(encoding="utf-8"))
        self.assertTrue(sync["container_checks_passed"])
        self.assertEqual(sync["ofs_compatibility"],"not_tested")
        self.assertEqual(len(sync["anchor_checks"]),3)
        with Image.open(output / "frame-000025.png") as image:
            self.assertEqual(image.size,(640,360))
        edited = self.root / "edited.funscript"
        data = json.loads((output / "track.funscript").read_text(encoding="utf-8"))
        data["actions"][1]["pos"] -= 1
        edited.write_text(json.dumps(data),encoding="utf-8")
        archived = archive_revision(self.project,self.draft,edited,self.root / "revision",diagnostic=output)
        self.assertEqual(archived["diagnostic_curve_status"],"obsolete_for_external_revision")
        self.assertTrue((output / "track.mp4").is_file())


if __name__=="__main__":
    unittest.main()
