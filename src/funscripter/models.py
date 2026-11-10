from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SubtitleConfig(Model):
    path: str
    format: Literal["auto", "lrc", "srt"] = "auto"
    encoding: str = "utf-8-sig"
    offset_ms: int = 0
    apply_lrc_offset: bool = True
    empty_timestamp_policy: Literal["end_hint", "display_clear"] = "end_hint"


class Preset(Model):
    low: int = Field(ge=0, le=100)
    high: int = Field(ge=0, le=100)
    period_ms: int = Field(ge=2)
    up_time_ratio: float = Field(default=0.5, gt=0, lt=1)

    @model_validator(mode="after")
    def check_range(self):
        if self.low >= self.high:
            raise ValueError("preset low must be less than high")
        if not 1 <= round(self.period_ms * self.up_time_ratio) < self.period_ms:
            raise ValueError("both directions must last at least one millisecond")
        return self


class Segment(Model):
    segment_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    preset_id: str


class GenerationConfig(Model):
    # Gaps and pauses explicitly hold the last generated position.
    initial_position: int = Field(default=50, ge=0, le=100)
    gap_policy: Literal["hold_position"] = "hold_position"
    presets: dict[str, Preset] = Field(default_factory=dict)
    segments: list[Segment] = Field(default_factory=list)


class DeviceConfig(Model):
    allowed_position_range: tuple[int, int] | None = None
    max_speed_pos_per_sec: float | None = Field(default=None, gt=0)
    final_interpolation: Literal["linear"] = "linear"

    @model_validator(mode="after")
    def check_range(self):
        if self.allowed_position_range is not None:
            low, high = self.allowed_position_range
            if not 0 <= low < high <= 100:
                raise ValueError("invalid device position range")
        return self


class AudioAnalysisConfig(Model):
    frame_ms: int = Field(default=40, ge=10, le=200)
    hop_ms: int = Field(default=10, ge=1, le=100)
    block_samples: int = Field(default=65536, ge=1024, le=4194304)
    bands_hz: dict[str, tuple[float, float]] = Field(default_factory=lambda: {
        "low": (40.0, 350.0), "mid": (350.0, 3000.0), "high": (3000.0, 10000.0)})
    silence_dbfs: float = Field(default=-60.0, ge=-120, le=-10)
    onset_relative_threshold: float = Field(default=0.15, gt=0, le=1)
    periodicity_window_ms: int = Field(default=8000, ge=1000, le=60000)
    periodicity_step_ms: int = Field(default=2000, ge=100, le=60000)
    min_period_ms: int = Field(default=250, ge=50)
    max_period_ms: int = Field(default=2000, ge=100)
    min_periodicity_score: float = Field(default=0.6, gt=0, le=1)
    min_modulation_ratio: float = Field(default=0.1, gt=0, le=2)

    @model_validator(mode="after")
    def check_analysis(self):
        if self.hop_ms > self.frame_ms:
            raise ValueError("hop_ms must not exceed frame_ms")
        if not self.bands_hz or any(not 0 <= low < high for low, high in self.bands_hz.values()):
            raise ValueError("bands must have ordered, nonnegative frequency limits")
        if any(not name.isidentifier() for name in self.bands_hz):
            raise ValueError("band names must be identifiers")
        if self.min_period_ms >= self.max_period_ms:
            raise ValueError("period search interval must be ordered")
        if self.periodicity_window_ms < 3 * self.max_period_ms:
            raise ValueError("periodicity window must span at least three maximum periods")
        return self


class LLMConfig(Model):
    backend: Literal["llama_cpp", "ollama"]
    base_url: str
    model_id: str = Field(min_length=1)
    model_revision: str = "unspecified"
    quantization: str = "unspecified"
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_retries: int = Field(default=2, ge=0, le=5)
    context_budget_tokens: int = Field(default=32768, ge=2048)
    max_output_tokens: int = Field(default=4096, ge=128)
    safety_tokens: int = Field(default=1024, ge=128)
    max_chunk_cues: int = Field(default=32, ge=1, le=256)
    context_cues: int = Field(default=1, ge=0, le=8)
    thinking: bool | Literal["backend_default"] = "backend_default"
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def check_llm(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url must be an HTTP service URL without credentials, query or fragment")
        if self.max_output_tokens + self.safety_tokens >= self.context_budget_tokens:
            raise ValueError("context budget must leave room for input")
        return self


class Phase(Model):
    phase_id: str
    start_cue_id: str
    end_cue_id_inclusive: str
    summary: str = Field(min_length=1, max_length=1000)
    trend_hint: Literal["steady", "increase", "decrease", "unknown"]
    evidence_cue_ids: list[str] = Field(min_length=1)
    needs_review: bool


class Outline(Model):
    schema_version: Literal["1.3"]
    phases: list[Phase] = Field(min_length=1)


class SemanticEvent(Model):
    cue_id: str
    intent: Literal["maintain", "pause", "resume", "increase_rate", "decrease_rate", "unknown"]
    scope: Literal["current_section", "next_section", "uncertain"]
    anchor: Literal["cue_start", "after_utterance", "uncertain"]
    pace_tier: Literal["stop", "slow", "medium", "fast", "unknown"]
    amplitude_tier: Literal["small", "medium", "large", "unknown"]
    motion_style: Literal["rhythmic", "smooth", "irregular", "hold", "unknown"]
    evidence: str
    needs_review: bool
    reason: str


class SemanticBatch(Model):
    schema_version: Literal["1.3"]
    events: list[SemanticEvent]


class Config(Model):
    schema_version: Literal["1.3"] = "1.3"
    audio: str
    subtitles: SubtitleConfig
    ffmpeg: str = "ffmpeg"
    llm: LLMConfig | None = None
    audio_analysis: AudioAnalysisConfig = Field(default_factory=AudioAnalysisConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)


class Cue(Model):
    cue_id: str
    source_line: int
    source_number: int | None = None
    text_raw: str
    text_for_analysis: str
    speaker_label: str | None = None
    original_start_ms: int
    original_end_ms: int | None = None
    start_ms: int
    end_ms: int | None
    end_inferred: bool = True


class Marker(Model):
    marker_id: str
    source_line: int
    source_number: int | None = None
    original_start_ms: int
    original_end_ms: int | None = None
    start_ms: int
    end_ms: int | None = None
    policy: str
    previous_cue_ids: list[str]
    next_cue_ids: list[str]


class Timeline(Model):
    metadata: dict[str, str]
    lrc_offset_ms: int
    effective_offset_ms: int
    cues: list[Cue]
    markers: list[Marker]
    issues: list[dict]


class Project(Model):
    schema_version: Literal["1.3"] = "1.3"
    project_id: str
    revision_id: str
    status: Literal["needs_review"] = "needs_review"
    created_at: str
    config: Config
    input_hashes: dict[str, str]
    artifact_hashes: dict[str, str]
    artifacts: dict[str, str]
    external_tools: dict[str, str]


class QualitativeState(Model):
    pace_tier: Literal["slow", "medium", "fast", "unknown"] = "unknown"
    amplitude_tier: Literal["small", "medium", "large", "unknown"] = "unknown"
    motion_style: Literal["rhythmic", "smooth", "irregular", "hold", "unknown"] = "unknown"
    paused: bool = False


class FusionProfile(Model):
    schema_version: Literal["1.3"] = "1.3"
    preset_version: str = Field(min_length=1)
    presets: dict[str, Preset]
    tier_map: dict[str, str]
    initial_state: QualitativeState = Field(default_factory=QualitativeState)
    unknown_policy: Literal["hold_position", "maintain_state"]
    manual_boundaries_ms: dict[str, int] = Field(default_factory=dict)
    use_empty_timestamp_hints: bool = False
    empty_timestamp_convention_confirmed: bool = False
    audio_period_tolerance: float = Field(default=0.25, ge=0, le=1)
    preview_page_ms: int = Field(default=60000, ge=1000, le=600000)

    @model_validator(mode="after")
    def check_fusion(self):
        for key, preset_id in self.tier_map.items():
            fields = key.split("/")
            if len(fields) != 3 or fields[0] not in ("slow", "medium", "fast") or fields[1] not in ("small", "medium", "large") or fields[2] not in ("rhythmic", "smooth", "irregular"):
                raise ValueError("tier_map keys must be pace/amplitude/style")
            if preset_id not in self.presets:
                raise ValueError("tier_map references unknown preset")
            # The existing generator is piecewise linear, not a smooth or irregular generator.
            if fields[2] != "rhythmic":
                raise ValueError("only rhythmic presets have a generator in this version")
        if self.use_empty_timestamp_hints and not self.empty_timestamp_convention_confirmed:
            raise ValueError("confirm the subtitle empty timestamp convention before using hints")
        if any(value < 0 for value in self.manual_boundaries_ms.values()):
            raise ValueError("manual boundaries must be nonnegative")
        return self


class FusedSegment(Model):
    segment_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    mode: Literal["hold_position", "preset"]
    preset_id: str | None
    state: QualitativeState
    cue_ids: list[str]
    subtitle_regions: list[dict]
    event_ids: list[str]
    audio_evidence_ids: list[str]
    needs_review: Literal[True]
    review_reasons: list[str]
    manual_source_id: str | None
    boundary_status: Literal["manual_segment", "generated_draft"]

    @model_validator(mode="after")
    def check_span(self):
        if self.start_ms >= self.end_ms or (self.mode == "preset") != (self.preset_id is not None):
            raise ValueError("invalid fused segment span or mode")
        return self


class SegmentPlan(Model):
    schema_version: Literal["1.3"]
    profile: FusionProfile
    phases: list[dict]
    segments: list[FusedSegment]
    event_dispositions: list[dict]
    state_transitions: list[dict]
    status: Literal["needs_review"]


class DiagnosticProfile(Model):
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    font_path: str
    width: int = Field(default=1280, ge=640, le=3840)
    height: int = Field(default=720, ge=360, le=2160)
    fps: int = Field(default=10, ge=1, le=60)
    audio_sample_rate: int = Field(default=48000, ge=8000, le=192000)
    audio_bitrate: str = "192k"
    crf: int = Field(default=23, ge=0, le=51)
    render_timeout_seconds: float = Field(default=1800.0, gt=0)
    alignment_search_ms: int = Field(default=100, ge=5, le=1000)
    max_alignment_error_ms: float = Field(default=5.0, ge=0, le=100)
    min_alignment_correlation: float = Field(default=0.8, gt=0, le=1)
    max_tail_padding_ms: float = Field(default=100.0, ge=0, le=1000)

    @model_validator(mode="after")
    def check_video(self):
        import re
        if self.width % 2 or self.height % 2:
            raise ValueError("yuv420p video dimensions must be even")
        if not re.fullmatch(r"[1-9][0-9]*k", self.audio_bitrate):
            raise ValueError("audio_bitrate must be a positive integer followed by k")
        if self.max_alignment_error_ms > self.alignment_search_ms:
            raise ValueError("alignment tolerance must not exceed search range")
        return self


class TimeRange(Model):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.start_ms >= self.end_ms:
            raise ValueError("range start must precede end")
        return self


class DiffProfile(Model):
    interpolation: Literal["linear", "unknown"] = "unknown"
    external_interpolation_confirmed: bool = False
    comparison_reference: str | None = None
    time_tolerance_ms: int = Field(default=0, ge=0, le=100)
    timing_calibration_reference: str | None = None
    max_local_change_fraction: float = Field(default=0.5, gt=0, le=1)

    @model_validator(mode="after")
    def confirmed(self):
        if self.external_interpolation_confirmed and (self.interpolation != "linear" or not self.comparison_reference or not self.comparison_reference.strip()):
            raise ValueError("confirmed comparisons need linear interpolation and a reference")
        if self.time_tolerance_ms and not self.timing_calibration_reference:
            raise ValueError("nonzero tolerance needs an editor round-trip calibration reference")
        return self


class RegenerationWindow(TimeRange):
    window_id: str = Field(min_length=1)
    preset_id: str
    end_transition_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def transition_fits(self):
        if self.end_transition_ms >= self.end_ms-self.start_ms:
            raise ValueError("end transition must fit strictly inside the selected window")
        return self


class RegenerationProfile(Model):
    preset_version: str = Field(min_length=1)
    presets: dict[str, Preset]
    windows: list[RegenerationWindow] = Field(min_length=1)
    locked_ranges: list[TimeRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_windows(self):
        ordered = sorted(self.windows, key=lambda item:item.start_ms)
        if len({item.window_id for item in ordered}) != len(ordered):
            raise ValueError("window IDs must be unique")
        if any(item.preset_id not in self.presets for item in ordered):
            raise ValueError("window references unknown preset")
        if any(a.end_ms>b.start_ms for a,b in zip(ordered,ordered[1:])):
            raise ValueError("regeneration windows must not overlap")
        return self
