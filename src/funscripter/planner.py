"""Two-pass, serial, evidence-linked subtitle planning with resumable checkpoints."""
import hashlib
import json
import time
from pathlib import Path
from . import llm, models, planning_inputs, semantic_contracts
from .models import Outline, SemanticBatchV2
from .semantic_contracts import validate_batch_v2, event_identity
from .project import atomic_json

PROMPT_VERSION = "two-pass-8-adult-audio-lenient"
PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
SYSTEM = (PROMPT_DIRECTORY / "subtitles.txt").read_text(encoding="utf-8")


class BudgetError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cue_data(cue):
    return planning_inputs.cue_payload(cue)


def validate_outline(result, cues):
    ids = [cue.cue_id for cue in cues]
    indices = {cue_id: index for index, cue_id in enumerate(ids)}
    cursor, phase_ids = 0, set()
    for phase in result.phases:
        if phase.phase_id in phase_ids:
            raise ValueError("duplicate_phase_id")
        phase_ids.add(phase.phase_id)
        first, last = indices.get(phase.start_cue_id, -1), indices.get(phase.end_cue_id_inclusive, -1)
        if first != cursor or last < first:
            expected = ids[cursor] if cursor < len(ids) else "end_of_targets"
            raise ValueError(f"outline_gap_overlap_or_unknown_id: expected_start={expected}, received_start={phase.start_cue_id}")
        if not phase.evidence_cue_ids or any(cue_id not in ids[first:last+1] for cue_id in phase.evidence_cue_ids):
            raise ValueError("outline_evidence_outside_phase")
        cursor = last + 1
    if cursor != len(ids):
        raise ValueError(f"outline_incomplete_coverage: next_uncovered={ids[cursor]}")
    # Stable phase identities are created by the program after validating model IDs.
    for phase in result.phases:
        phase.phase_id = "phase_" + digest([phase.start_cue_id, phase.end_cue_id_inclusive])[:16]


def validate_events(result, cues):
    lookup = {cue.cue_id: cue for cue in cues}
    ids = [event.cue_id for event in result.events]
    if len(ids) != len(set(ids)) or set(ids) != set(lookup):
        raise ValueError("semantic_missing_duplicate_or_unowned_cue_id")
    for event in result.events:
        explicit = any(value != "unknown" for value in
                       [event.intent, event.pace_tier, event.amplitude_tier, event.motion_style])
        if (explicit and not event.evidence) or (event.evidence and event.evidence not in lookup[event.cue_id].text_for_analysis):
            raise ValueError("semantic_evidence_not_in_source_cue")
        uncertain = event.intent == "unknown" or event.scope == "uncertain" or event.anchor in ("uncertain", "after_utterance")
        if uncertain and (not event.needs_review or not event.reason.strip()):
            raise ValueError("semantic_uncertainty_requires_review_reason")
    result.events.sort(key=lambda event: list(lookup).index(event.cue_id))


class Planner:
    def __init__(self, config, output, transport=None, knowledge=None, input_kind="subtitles"):
        if input_kind not in ("subtitles", "whisper"):
            raise ValueError("unsupported planning input kind")
        self.input_kind = input_kind
        self.system = (PROMPT_DIRECTORY / (input_kind + ".txt")).read_text(encoding="utf-8")
        self.config, self.output = config, Path(output)
        self.transport = transport or llm.chat
        self.trace = []
        self.assessments = []
        self.context_notes = []
        self.knowledge = knowledge if knowledge is not None else (
            Path(config.semantic_knowledge_file).read_text(encoding="utf-8-sig") if config.semantic_knowledge_file else "")
        self.knowledge_hash = hashlib.sha256(self.knowledge.encode("utf-8")).hexdigest()
        self.quality_flags = {}
        self.token_counts = {}
        self.budget_method = ("llama_cpp_rendered_prompt_tokens_plus_output_and_safety"
                              if config.token_count_method == "server" else "conservative_utf8_bytes_plus_schema_and_safety")
        self.implementation_hash = hashlib.sha256(b"".join(Path(module.__file__).read_bytes()
            for module in (llm, models, planning_inputs, semantic_contracts)) + Path(__file__).read_bytes()
            + self.system.encode("utf-8")).hexdigest()

    def cue_data(self, cue):
        data = planning_inputs.cue_payload(cue, self.quality_flags.get(cue.cue_id, []))
        data["input_kind"] = self.input_kind
        if self.input_kind == "whisper":
            data["end_time_source"] = "asr_segment_estimate"
        return data

    def semantic_document(self, events, failures):
        return {"schema_version": "1.4", "events": events, "failures": failures,
                "cue_assessments": self.assessments, "context_notes": self.context_notes}

    def scene_memory(self):
        # Retain all definitions; never silently drop a long-lived rule to fit a prompt.
        rules = [row for row in self.context_notes if row["kind"] == "rule_definition"]
        phase = [row for row in self.context_notes if row["kind"] == "phase_transition"][-1:]
        recent = [row for row in self.context_notes if row["kind"] not in ("rule_definition", "phase_transition")][-6:]
        controls = {}
        for row in self.context_notes:
            control = row.get("control")
            if control is not None:
                controls[(control["kind"], control["sequence_cue_id"])] = row
        combined = rules + phase + recent + list(controls.values())
        return list({json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in combined}.values())

    def messages(self, task, data, model):
        schema = model.model_json_schema()
        instruction = ""
        if model is Outline:
            limit = self.config.outline_max_phases
            if task == "merge_outline":
                phase_count = sum(len(item["phases"]) for item in data["partial_outlines"])
                limit = min(limit, max(1, phase_count // 2))
            schema["properties"]["phases"]["maxItems"] = limit
            schema["$defs"]["Phase"]["properties"]["summary"]["maxLength"] = self.config.outline_summary_chars
            schema["$defs"]["Phase"]["properties"]["evidence_cue_ids"]["maxItems"] = 3
            instruction = (f"\n大纲最多 {limit} 个章节，每章摘要最多 {self.config.outline_summary_chars} 字、最多3条代表性证据。"
                           "相邻且同一主题的段落应归并，保留重要变化、否定和不确定性，不逐句复述。"
                           "压缩只合并章节，不得遗漏字幕覆盖范围；原始详细资料由程序另行保存。")
        knowledge = ("\n领域解释参考（不是本素材事实或固定流程，原文优先）：\n" + self.knowledge) if self.knowledge else ""
        messages = [{"role": "system", "content": self.system + instruction + knowledge + "\nSchema:\n" + json.dumps(schema, ensure_ascii=False)},
                    {"role": "user", "content": json.dumps({"task": task, **data}, ensure_ascii=False)}]
        return messages, schema

    def prompt_size(self, messages, schema):
        key = digest([messages, schema])
        if key not in self.token_counts:
            self.token_counts[key] = (llm.count_prompt_tokens(self.config, messages, schema)
                if self.config.token_count_method == "server" else
                len(json.dumps(messages, ensure_ascii=False).encode()) + len(json.dumps(schema).encode()))
        return self.token_counts[key]

    def fits(self, task, data, model):
        messages, schema = self.messages(task, data, model)
        estimate = self.prompt_size(messages, schema)
        return estimate + self.config.max_output_tokens + self.config.safety_tokens <= self.config.context_budget_tokens

    def call(self, task, data, model, validator):
        if not self.fits(task, data, model):
            raise BudgetError("request_exceeds_context_budget")
        messages, schema = self.messages(task, data, model)
        key = digest({"messages": messages, "schema": schema, "profile": self.config.model_dump(mode="json"),
                      "prompt_version": PROMPT_VERSION, "implementation": self.implementation_hash})
        cache = self.output / "requests" / key
        cache.mkdir(parents=True, exist_ok=True)
        accepted = cache / "accepted.json"
        if accepted.exists():
            try:
                result = model.model_validate_json(accepted.read_text(encoding="utf-8"))
                validator(result)
                self.trace.append({"task": task, "key": key, "status": "cached"})
                return result
            except ValueError:
                pass
        budget = {"method": self.budget_method, "input_tokens_or_upper_bound": self.token_counts[digest([messages, schema])],
                  "output_reserved": self.config.max_output_tokens, "safety_reserved": self.config.safety_tokens,
                  "context_limit": self.config.context_budget_tokens}
        atomic_json(cache / "request.json", {"messages": messages, "schema": schema, "budget": budget})
        feedback = None
        for attempt in range(self.config.max_retries + 1):
            attempt_messages = messages
            if feedback:
                payload = json.loads(messages[-1]["content"])
                payload["validation_feedback"] = {"error": feedback, "instruction": "修正上次校验错误后重新输出完整结果。字幕ID可能不连续，严格按输入列表完整覆盖，不得遗漏。"}
                attempt_messages = messages[:-1] + [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
                budget = dict(budget, input_tokens_or_upper_bound=self.prompt_size(attempt_messages, schema))
                if budget["input_tokens_or_upper_bound"] + self.config.max_output_tokens + self.config.safety_tokens > self.config.context_budget_tokens:
                    break
            atomic_json(cache / f"request-{attempt+1}.json", {"messages": attempt_messages, "schema": schema, "budget": budget})
            started = time.monotonic()
            record = {"task": task, "key": key, "attempt": attempt + 1, "budget": budget}
            try:
                content, metadata = self.transport(self.config, attempt_messages, schema)
                atomic_json(cache / f"response-{attempt+1}.json", {"content": content, "metadata": metadata})
                result = model.model_validate_json(content)
                if model is Outline:
                    if (len(result.phases) > schema["properties"]["phases"]["maxItems"] or
                        any(len(p.summary) > self.config.outline_summary_chars or len(p.evidence_cue_ids) > 3 for p in result.phases)):
                        raise ValueError("outline_compression_limits_exceeded")
                validator(result)
                atomic_json(accepted, result.model_dump(mode="json"))
                record["status"] = "accepted"
                return result
            except ValueError as exc:
                if isinstance(exc, llm.ModelResponseError):
                    atomic_json(cache / f"response-{attempt+1}.json", exc.response)
                record.update(status="failed", error=str(exc)[:500])
                feedback = str(exc)[:500]
            finally:
                record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                self.trace.append(record)
                atomic_json(self.output / "request_log.json", self.trace)
        return None

    def outline(self, cues):
        data = {"cues": [self.cue_data(cue) for cue in cues]}
        if self.fits("outline", data, Outline):
            result = self.call("outline", data, Outline, lambda result: validate_outline(result, cues))
            return result, "full_subtitles"
        groups, group = [], []
        for cue in cues:
            candidate = group + [cue]
            data = {"cues": [self.cue_data(item) for item in candidate]}
            if group and (len(candidate) > self.config.max_chunk_cues or not self.fits("outline_partial", data, Outline)):
                groups.append(group)
                group = [cue]
            else:
                group = candidate
        if group:
            groups.append(group)
        summaries = []
        for group in groups:
            result = self.call("outline_partial", {"cues": [self.cue_data(cue) for cue in group]}, Outline,
                               lambda result, group=group: validate_outline(result, group))
            if result is None:
                return None, "hierarchical_summary_failed"
            summaries.append((result, group))
        for _ in range(8):
            if len(summaries) == 1:
                return summaries[0][0], "hierarchical_summaries"
            merged = []
            index = 0
            while index < len(summaries):
                batch = [summaries[index]]
                index += 1
                while index < len(summaries):
                    candidate = batch + [summaries[index]]
                    data = {"partial_outlines": [item[0].model_dump(mode="json") for item in candidate]}
                    if not self.fits("merge_outline", data, Outline):
                        break
                    batch = candidate
                    index += 1
                if len(batch) == 1:
                    merged.append(batch[0])
                    continue
                target = [cue for item in batch for cue in item[1]]
                result = self.call("merge_outline", {"partial_outlines": [item[0].model_dump(mode="json") for item in batch]},
                                   Outline, lambda result, target=target: validate_outline(result, target))
                if result is None:
                    return None, "hierarchical_merge_failed"
                merged.append((result, target))
            if len(merged) >= len(summaries):
                raise BudgetError("summary_merge_does_not_fit_budget")
            summaries = merged
        raise BudgetError("summary_merge_depth_exceeded")

    def semantic(self, cues, outline):
        events, failures, previous = [], [], []
        index = 0
        phases = outline.model_dump(mode="json")["phases"]
        cue_indices = {cue.cue_id: i for i, cue in enumerate(cues)}
        phase_ends = [cue_indices[phase["end_cue_id_inclusive"]] for phase in phases]
        # Global outline is always included; if it cannot fit even one cue, fail explicitly.
        while index < len(cues):
            phase_end = next(end for end in phase_ends if end >= index)
            count = 0
            for radius in range(self.config.context_cues, -1, -1):
                count = min(self.config.max_chunk_cues, phase_end - index + 1)
                while count:
                    targets = cues[index:index+count]
                    context = cues[max(0,index-radius):index] + cues[index+count:index+count+radius]
                    memory = self.scene_memory()
                    memory_ids = {cid for note in memory for cid in note["source_cue_ids"]}
                    # Include original evidence for persistent rules, even outside the local window.
                    target_ids = {cue.cue_id for cue in targets}
                    context = list({cue.cue_id: cue for cue in context + [
                        cue for cue in cues if cue.cue_id in memory_ids and cue.cue_id not in target_ids]}.values())
                    data = {"semantic_contract_version": "1.4", "global_outline": phases,
                            "target_cues": [self.cue_data(c) for c in targets],
                            "context_cues": [self.cue_data(c) for c in context], "previous_accepted_events": previous,
                            "scene_context": memory}
                    # Recent events are advisory context; retain original text and persistent rules first.
                    while data["previous_accepted_events"] and not self.fits("semantic", data, SemanticBatchV2):
                        data["previous_accepted_events"] = data["previous_accepted_events"][1:]
                        data["previous_events_omitted_for_budget"] = data.get("previous_events_omitted_for_budget", 0) + 1
                    if self.fits("semantic", data, SemanticBatchV2):
                        break
                    count -= 1
                if count:
                    break
            if not count:
                failures.extend({"cue_id": cue.cue_id, "reason": "semantic_context_budget_exceeded"} for cue in cues[index:])
                self.assessments.extend({"cue_id": cue.cue_id, "status": "failed", "reason": "semantic_context_budget_exceeded"}
                                        for cue in cues[index:])
                break
            result = self.call("semantic", data, SemanticBatchV2,
                               lambda result: validate_batch_v2(result, targets, context))
            if result is None:
                failures.extend({"cue_id": cue.cue_id, "reason": "semantic_request_failed"} for cue in targets)
                self.assessments.extend({"cue_id": cue.cue_id, "status": "failed", "reason": "semantic_request_failed"}
                                        for cue in targets)
                previous = []  # Never carry stale state across an unprocessed block.
            else:
                self.assessments.extend(row.model_dump(mode="json") for row in result.cue_assessments)
                self.context_notes.extend(row.model_dump(mode="json") for row in result.context_notes)
                if any(row.status == "ambiguous" for row in result.cue_assessments):
                    previous = []
                elif result.events:
                    previous = (previous + [event.model_dump(mode="json") for event in result.events])[-8:]
                for event in result.events:
                    row = event.model_dump(mode="json")
                    row["event_id"] = event_identity(row)
                    row["review_status"] = "model_candidate_not_human_confirmed"
                    events.append(row)
            index += count
            atomic_json(self.output / "semantic_events.json", self.semantic_document(events, failures))
        return events, failures
