import json
import sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from funscripter.models import Config, Project, Timeline, AudioAnalysisConfig, LLMConfig, Outline, SemanticBatch, FusionProfile, SegmentPlan, DiagnosticProfile, DiffProfile, RegenerationProfile
from funscripter.multiaxis import MultiAxisProfile
from funscripter.boundaries import BoundaryProfile, CandidateDocument, VadEvidence
(root / "schemas").mkdir(exist_ok=True)
for name, model in [("config", Config), ("project", Project), ("subtitles", Timeline), ("audio-analysis", AudioAnalysisConfig), ("llm", LLMConfig), ("outline", Outline), ("semantic-batch", SemanticBatch), ("fusion-profile", FusionProfile), ("segment-plan", SegmentPlan), ("diagnostic-profile", DiagnosticProfile), ("diff-profile", DiffProfile), ("regeneration-profile", RegenerationProfile)]:
    (root / "schemas" / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(root / "schemas/multiaxis-profile.schema.json").write_text(json.dumps(MultiAxisProfile.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for name, model in [("boundary-profile", BoundaryProfile), ("utterance-candidates", CandidateDocument), ("vad-evidence", VadEvidence)]:
    (root / "schemas" / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
