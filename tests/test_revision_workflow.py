import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import yaml
import test_fusion as fixtures
from funscripter.models import DeviceConfig,DiffProfile
from funscripter.revision_diff import compare_scripts,equal_on_ranges
from funscripter.revisions import archive_revision
from funscripter.revision_workflow import diff_revision,prepare_regeneration,accept_candidate,restore_revision
from funscripter.fuse_project import fuse_project
from funscripter.project import atomic_json
from funscripter.media import sha256


def data():
    return {"version":"1.0","inverted":False,"range":100,"actions":[
        {"at":i*1000,"pos":50 if i in (0,10) else (60 if i%2 else 40)} for i in range(11)]}


def raw(value):
    return json.dumps(value,ensure_ascii=False).encode()


def confirmed(**options):
    return DiffProfile(interpolation="linear",external_interpolation_confirmed=True,
        comparison_reference="synthetic linear/held-endpoint fixture only",**options)


class DifferenceTests(unittest.TestCase):
    def compare(self,left,right,profile=None,duration=10000,known=True):
        return compare_scripts(raw(left),raw(right),duration,DeviceConfig(),profile or confirmed(),known)

    def test_changed_point_protects_cross_segment_neighbor_support(self):
        base=data()
        edited=copy.deepcopy(base)
        edited["actions"][4]["pos"]=44
        report=self.compare(base,edited)
        self.assertEqual(report["classification"],"local_point_changes")
        self.assertEqual([(r["start_ms"],r["end_ms"]) for r in report["protected_ranges"]],[(3000,5000)])
        self.assertEqual(report["raw_changes"][0]["time_ms"],4000)
        self.assertFalse(equal_on_ranges(base["actions"],edited["actions"],[(4500,4900)]))
        self.assertTrue(equal_on_ranges(base["actions"],edited["actions"],[(6000,7000)]))

    def test_moved_point_uses_support_from_both_curves_and_one_ms_is_not_ignored(self):
        base=data()
        edited=copy.deepcopy(base)
        edited["actions"][4]["at"]+=1
        report=self.compare(base,edited,confirmed(time_tolerance_ms=1,timing_calibration_reference="synthetic"))
        self.assertEqual(len(report["raw_changes"]),2)
        self.assertTrue(report["timing_differences_within_configured_tolerance"])
        self.assertEqual(report["protected_ranges"][0]["start_ms"],3000)
        self.assertEqual(report["protected_ranges"][0]["end_ms"],5000)
        with self.assertRaises(ValueError):
            DiffProfile(time_tolerance_ms=1)

    def test_global_offset_scale_position_and_format_changes_protect_whole_track(self):
        base=data()
        variants=[]
        shifted=copy.deepcopy(base)
        for point in shifted["actions"]:
            point["at"]+=100
        variants.append((shifted,"global_time_offset"))
        scaled=copy.deepcopy(base)
        for point in scaled["actions"]:
            point["at"]*=2
        variants.append((scaled,"global_time_transform"))
        positions=copy.deepcopy(base)
        for point in positions["actions"]:
            point["pos"]+=5
        variants.append((positions,"global_position_transform"))
        metadata=copy.deepcopy(base)
        metadata["inverted"]=True
        variants.append((metadata,"format_fields_changed"))
        for edited,classification in variants:
            report=self.compare(base,edited,duration=20000)
            self.assertEqual(report["classification"],classification)
            self.assertEqual(report["protection_scope"],"entire_revision")
            self.assertFalse(report["automatic_merge_allowed"])

    def test_resampling_equivalent_curve_is_not_silently_unprotected(self):
        base=data()
        edited=copy.deepcopy(base)
        edited["actions"].insert(1,{"at":500,"pos":55})
        report=self.compare(base,edited)
        self.assertEqual(report["classification"],"resampled_equivalent_curve_candidate")
        self.assertEqual(report["protection_scope"],"entire_revision")

    def test_same_points_without_confirmed_playback_and_unknown_baseline_stay_protected(self):
        base=data()
        report=self.compare(base,base,DiffProfile())
        self.assertEqual(report["classification"],"byte_identical")
        self.assertFalse(report["automatic_merge_allowed"])
        self.assertEqual(report["protection_scope"],"entire_revision")
        self.assertEqual(self.compare(base,base,known=False)["classification"],"unknown_baseline")
        self.assertEqual(self.compare(base,base)["protected_ranges"],[])

    def test_invalid_and_duplicate_time_preserve_full_revision(self):
        base=data()
        edited=copy.deepcopy(base)
        edited["actions"][2]["at"]=1000
        self.assertEqual(self.compare(base,edited)["classification"],"invalid_input")
        report=compare_scripts(raw(base),b"",10000,DeviceConfig(),confirmed())
        self.assertEqual(report["protection_scope"],"entire_revision")
        for external in [b'{"actions":[{"at":0,"pos":50,"pos":40}]}',b'{"actions":[{"at":0,"pos":50}],"metadata":NaN}']:
            report=compare_scripts(raw(base),external,10000,DeviceConfig(),confirmed())
            self.assertEqual(report["classification"],"invalid_input")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.FusionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root,self.project=self.fixture.root,self.fixture.project_dir
        planning,profile=self.fixture.publish_inputs()
        self.baseline=self.root/"baseline"
        fuse_project(self.project,planning,self.baseline,profile)
        self.base=json.loads((self.baseline/"track.funscript").read_text(encoding="utf-8"))
        self.external=copy.deepcopy(self.base)
        # The fused slow curve has knots at each second; keep exact boundaries.
        point=next(p for p in self.external["actions"] if p["at"]==4000)
        point["pos"]+=2
        self.external_file=self.root/"edited.funscript"
        self.original_bytes=b"\xef\xbb\xbf"+raw(self.external)
        self.external_file.write_bytes(self.original_bytes)
        self.archive=self.root/"archive"
        archive_revision(self.project,self.baseline,self.external_file,self.archive)
        self.diff_profile=self.root/"diff.yaml"
        self.diff_profile.write_text(yaml.safe_dump(confirmed().model_dump(mode="json")),encoding="utf-8")
        self.diff=self.root/"diff"
        diff_revision(self.project,self.archive,self.baseline,self.diff_profile,self.diff)
        self.regen_profile=self.root/"regen.yaml"
        self.write_regen()

    def write_regen(self,start=6000,end=8000,locks=None):
        value={"preset_version":"test-local-v1","presets":{"replacement":{"low":35,"high":65,"period_ms":1000}},
            "windows":[{"window_id":"window1","start_ms":start,"end_ms":end,"preset_id":"replacement","end_transition_ms":250}],
            "locked_ranges":locks or []}
        self.regen_profile.write_text(yaml.safe_dump(value),encoding="utf-8")

    def prepare(self,name="proposal"):
        output=self.root/name
        result=prepare_regeneration(self.project,self.diff,self.regen_profile,output)
        return output,result

    def test_prepare_accept_undo_redo_and_reopen_keep_original_bytes(self):
        self.write_regen(locks=[{"start_ms":9000,"end_ms":10000}])
        output,result=self.prepare()
        self.assertTrue(result["eligible_for_acceptance"],result)
        self.assertEqual((output/"parent_external_original.funscript").read_bytes(),self.original_bytes)
        candidate=json.loads((output/"candidate_curve.json").read_text(encoding="utf-8"))
        self.assertNotEqual(candidate["actions"],self.external["actions"])
        self.assertTrue(equal_on_ranges(candidate["actions"],self.external["actions"],[(0,6000),(8000,10000),(3000,5000)]))
        proof=json.loads((output/"preservation_proof.json").read_text())
        self.assertTrue(all(proof.values()))
        accepted=self.root/"accepted"
        manifest=accept_candidate(self.project,output,result["candidate_id"],accepted)
        self.assertTrue(manifest["script_exported"])
        self.assertEqual((self.archive/"external_original.funscript").read_bytes(),self.original_bytes)
        undo,redo=self.root/"undo",self.root/"redo"
        restore_revision(self.project,accepted,"parent",undo)
        restore_revision(self.project,accepted,"result",redo)
        self.assertEqual((undo/"track.funscript").read_bytes(),self.original_bytes)
        self.assertEqual((redo/"track.funscript").read_bytes(),(accepted/"track.funscript").read_bytes())
        with self.assertRaisesRegex(ValueError,"ID mismatch"):
            accept_candidate(self.project,output,"wrong-id",self.root/"wrong")

    def test_manual_influence_and_locks_block_candidate_instead_of_skipping_silently(self):
        self.write_regen(start=4000,end=6000)
        output,result=self.prepare("protected")
        self.assertFalse(result["eligible_for_acceptance"])
        self.assertFalse((output/"candidate_curve.json").exists())
        self.write_regen(locks=[{"start_ms":6500,"end_ms":7500}])
        _,result=self.prepare("locked")
        self.assertFalse(result["eligible_for_acceptance"])
        with self.assertRaises(ValueError):
            accept_candidate(self.project,output,result["candidate_id"],self.root/"bad-accept")

    def test_off_grid_boundaries_are_not_silently_rounded(self):
        self.write_regen(start=6100,end=8000)
        output,result=self.prepare()
        self.assertFalse(result["eligible_for_acceptance"])
        conflicts=json.loads((output/"conflicts.json").read_text())
        self.assertIn("window_endpoints_must_be_existing_external_points",[row["reason"] for row in conflicts])

    def test_new_project_lock_after_proposal_prevents_acceptance(self):
        output,result=self.prepare()
        override=self.project/"manual_overrides.json"
        atomic_json(override,{"schema_version":"1.3","overrides":[],"locked_ranges":[[6500,7500]]})
        manifest=json.loads((self.project/"project.json").read_text())
        manifest["artifact_hashes"]["overrides"]=sha256(override)
        atomic_json(self.project/"project.json",manifest)
        with self.assertRaisesRegex(ValueError,"current project lock"):
            accept_candidate(self.project,output,result["candidate_id"],self.root/"locked-after")

    def test_inherited_protection_survives_new_baseline_and_identical_roundtrip(self):
        self.write_regen(locks=[{"start_ms":9000,"end_ms":10000}])
        output,result=self.prepare()
        accepted=self.root/"accepted"
        accept_candidate(self.project,output,result["candidate_id"],accepted)
        second_archive=self.root/"second-archive"
        archive_revision(self.project,accepted,accepted/"track.funscript",second_archive)
        second_diff=self.root/"second-diff"
        diff_revision(self.project,second_archive,accepted,self.diff_profile,second_diff)
        report=json.loads((second_diff/"diff_report.json").read_text())
        self.assertEqual(report["classification"],"byte_identical")
        self.assertGreater(report["inherited_protection_count"],0)
        self.assertTrue(any(row["start_ms"]<=3000 and row["end_ms"]>=5000 for row in report["protected_ranges"]))
        self.write_regen(start=3000,end=5000)
        result=prepare_regeneration(self.project,second_diff,self.regen_profile,self.root/"second-proposal")
        self.assertFalse(result["eligible_for_acceptance"])

    def test_tightened_device_limits_are_checked_again_at_prepare_and_accept(self):
        from funscripter.project import reopen_project
        project,timeline,media,report=reopen_project(self.project)
        project.config.device.max_speed_pos_per_sec=20.0
        with patch("funscripter.revision_workflow.reopen_project",return_value=(project,timeline,media,report)):
            output,result=self.prepare("speed-blocked")
        self.assertFalse(result["eligible_for_acceptance"])
        self.assertIn("device_speed_limit",json.loads((output/"validation.json").read_text())["constraint_errors"])
        output,result=self.prepare("unrestricted-proposal")
        with patch("funscripter.revision_workflow.reopen_project",return_value=(project,timeline,media,report)):
            with self.assertRaisesRegex(ValueError,"final constraints"):
                accept_candidate(self.project,output,result["candidate_id"],self.root/"speed-accept")

    def test_identical_curve_from_different_baseline_revision_is_not_assumed_same_source(self):
        other=self.root/"other-baseline"
        fuse_project(self.project,self.root/"planning",other,self.root/"fusion.yaml")
        output=self.root/"unknown-baseline-diff"
        result=diff_revision(self.project,self.archive,other,self.diff_profile,output)
        self.assertFalse(result["baseline_known"])
        report=json.loads((output/"diff_report.json").read_text())
        self.assertEqual(report["classification"],"unknown_baseline")
        self.assertEqual(report["protection_scope"],"entire_revision")

    def test_tampered_packet_rejected_and_originals_untouched(self):
        output,result=self.prepare()
        (output/"candidate_curve.json").write_text("{}",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"source artifact changed"):
            accept_candidate(self.project,output,result["candidate_id"],self.root/"tampered")
        self.assertEqual((self.external_file).read_bytes(),self.original_bytes)


if __name__=="__main__":
    unittest.main()
