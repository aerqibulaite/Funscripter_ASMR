"""Read-only request inspection and separate human-feedback candidates."""
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

from . import llm
from .models import LLMConfig, Outline, SemanticBatchV2
from .planner import validate_outline
from .project import atomic_json
from .media import sha256
from .semantic_contracts import validate_batch_v2


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("review path escapes planning directory")
    return path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def entries(directory):
    root = Path(directory).resolve()
    records = {}
    for name in ("repair_source_log.json", "request_log.json"):
        path = inside(root, root / name)
        if path.is_file():
            for row in read(path):
                records[(row["key"], row.get("attempt", 1))] = row
    result = []
    for folder in sorted((root / "requests").glob("*")):
        if not re.fullmatch(r"[a-f0-9]{64}", folder.name):
            continue
        main = inside(root, folder / "request.json")
        if not main.is_file():
            continue
        attempts = {1}
        for path in folder.glob("*.json"):
            match = re.fullmatch(r"(?:response|request)-(\d+)\.json", path.name)
            if match:
                attempts.add(int(match[1]))
        for attempt in sorted(attempts):
            request = inside(root, folder / f"request-{attempt}.json")
            if not request.is_file():
                request = main
            response = inside(root, folder / f"response-{attempt}.json")
            payload = read(request)
            body = json.loads(payload["messages"][-1]["content"])
            record = records.get((folder.name, attempt), {})
            cue_ids = [row["cue_id"] for row in body.get("target_cues", body.get("cues", []))]
            result.append({"key": folder.name, "attempt": attempt, "task": body["task"],
                "cue_ids": cue_ids, "status": record.get("status", "待校验" if response.exists() else "等待回答"),
                "error": record.get("error"), "elapsed_seconds": record.get("elapsed_seconds"),
                "request": str(request), "response": str(response) if response.is_file() else None,
                "updated": max(request.stat().st_mtime, response.stat().st_mtime if response.exists() else 0)})
    return sorted(result, key=lambda row: row["updated"])


def inspect_entry(directory, entry):
    request = read(inside(directory, entry["request"]))
    response = read(inside(directory, entry["response"])) if entry["response"] else {}
    raw = response.get("content", "尚未收到回答；每次请求结束后更新。")
    try:
        parsed = json.loads(raw)
        answer = json.dumps(parsed, ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        parsed = {}
        answer = str(raw)
    metadata = response.get("metadata", {})
    reasoning = metadata.get("reasoning_content") or metadata.get("thinking")
    return {"request": json.dumps(request, ensure_ascii=False, indent=2), "answer": answer,
            "analysis": analysis_text(request, parsed),
            "reasoning": reasoning or "此回答未保存思考文本。可查看回答中的 reason、evidence 和 cue_assessments。",
            "metadata": json.dumps(metadata, ensure_ascii=False, indent=2)}


def analysis_text(request, response):
    """Present only recorded judgments/evidence, never invent a reasoning trace."""
    body = json.loads(request["messages"][-1]["content"])
    response = response if isinstance(response, dict) else {}
    lines = ["以下为模型实际返回的分类、理由和证据，不补写模型未输出的分析。"]
    phases = response.get("phases", body.get("global_outline", []))
    if phases:
        lines.append("\n【章节背景】" + ("（本次输出）" if "phases" in response else "（本次请求携带的全局大纲）"))
        for phase in phases:
            lines.append(f"{phase.get('start_cue_id')} ～ {phase.get('end_cue_id_inclusive')}：{phase.get('summary', '')}")
    context = body.get("context_cues", [])
    if context:
        lines.append("\n【提供给模型的邻近台词】")
        lines.extend(f"{row['cue_id']}：{row.get('text', '')}" for row in context)
    assessments = {row["cue_id"]: row for row in response.get("cue_assessments", [])}
    labels = {"no_new_information": "无新增事件", "processed": "提取了事件", "ambiguous": "含义不确定", "failed": "处理失败", "excluded": "排除"}
    for cue in body.get("target_cues", body.get("cues", [])):
        row = assessments.get(cue["cue_id"], {})
        lines.extend([f"\n【{cue['cue_id']}】", "台词：" + cue.get("text", ""),
                      "模型判断：" + labels.get(row.get("status"), row.get("status", "未返回逐句判断")),
                      "判断依据：" + row.get("reason", "未输出")])
        for event in response.get("events", []):
            if event.get("cue_id") != cue["cue_id"]:
                continue
            lines.extend(["事件：" + " / ".join(str(event.get(k, "")) for k in ("intent", "statement_kind", "scope", "anchor")),
                          "事件理由：" + event.get("reason", "未输出"),
                          "原文证据：" + json.dumps(event.get("source_evidence", event.get("evidence", "")), ensure_ascii=False),
                          "待复核：" + str(event.get("needs_review"))])
    notes = response.get("context_notes", [])
    if notes:
        lines.append("\n【情境与规则笔记：候选解释，不直接执行动作】")
        lines.extend(f"{row['cue_id']} / {row['kind']} / {row['basis']}：{row['summary']}" for row in notes)
        for row in notes:
            if row.get("control"):
                lines.append("控制语义：" + json.dumps(row["control"], ensure_ascii=False))
    lines.append("\n无新增动作事件不等于场景没有延续；情境笔记与可执行动作分别保存。")
    return "\n".join(lines)


def save_feedback(workspace, directory, entry, note):
    if not note.strip():
        raise ValueError("请先填写纠正意见")
    request = inside(directory, entry["request"])
    response = inside(directory, entry["response"]) if entry["response"] else None
    folder = Path(workspace).resolve() / ".model-reviews" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    atomic_json(folder / "feedback.json", {"source_directory": str(Path(directory).resolve()),
        "request_path": str(request), "request_sha256": sha256(request),
        "response_path": str(response) if response else None, "response_sha256": sha256(response) if response else None,
        "key": entry["key"], "attempt": entry["attempt"], "note": note.strip(),
        "status": "feedback_only_not_applied"})
    return folder


def reanalyze(workspace, directory, entry, note, transport=None, thinking=None):
    original = read(inside(directory, entry["request"]))
    data = json.loads(original["messages"][-1]["content"])
    task = data.pop("task")
    if task not in ("semantic", "outline", "outline_partial"):
        raise ValueError("目前可重分析字幕分块或全文请求；合并大纲请重新规划")
    config = LLMConfig.model_validate_json(json.dumps(read(Path(directory) / "planning_run.json")["config"]))
    if thinking is not None:
        if type(thinking) is not bool:
            raise ValueError("thinking override must be boolean")
        config = config.model_copy(update={"thinking": thinking})
    folder = save_feedback(workspace, directory, entry, note)
    atomic_json(folder / "config.json", config.model_dump(mode="json"))
    # Keep the exact original system prompt/context; no source artifact is edited.
    messages = original["messages"][:-1] + [{"role": "user", "content": json.dumps(
        {"task": task, **data, "human_feedback": note.strip()}, ensure_ascii=False)}]
    schema = original["schema"]
    model = SemanticBatchV2 if task == "semantic" else Outline
    atomic_json(folder / "request.json", {"messages": messages, "schema": schema})
    try:
        count = (llm.count_prompt_tokens(config, messages, schema) if config.token_count_method == "server" else
                 len(json.dumps(messages, ensure_ascii=False).encode()) + len(json.dumps(schema).encode()))
        if count + config.max_output_tokens + config.safety_tokens > config.context_budget_tokens:
            raise ValueError("纠正意见加入后超过上下文预算，请缩短意见")
        content, metadata = (transport or llm.chat)(config, messages, schema)
        atomic_json(folder / "response-1.json", {"content": content, "metadata": metadata})
        result = model.model_validate_json(content)
        cues = lambda rows: [SimpleNamespace(cue_id=row["cue_id"], text_for_analysis=row["text"]) for row in rows]
        if task == "semantic":
            validate_batch_v2(result, cues(data["target_cues"]), cues(data["context_cues"]))
        else:
            validate_outline(result, cues(data["cues"]))
            if len(result.phases) > schema["properties"]["phases"].get("maxItems", config.outline_max_phases):
                raise ValueError("outline_compression_limits_exceeded")
            if any(len(p.summary) > config.outline_summary_chars or len(p.evidence_cue_ids) > 3 for p in result.phases):
                raise ValueError("outline_compression_limits_exceeded")
        atomic_json(folder / "candidate.json", result.model_dump(mode="json"))
        atomic_json(folder / "result.json", {"status": "candidate_needs_review_not_applied"})
    except Exception as exc:
        if isinstance(exc, llm.ModelResponseError):
            atomic_json(folder / "response-1.json", exc.response)
        atomic_json(folder / "result.json", {"status": "failed", "error": str(exc)})
        raise ValueError(f"{exc}\n记录已保存：{folder}") from exc
    return folder
