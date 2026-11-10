"""Resolve optional knowledge paths relative to their owning YAML profile."""
import json
from pathlib import Path
import yaml
from .models import LLMConfig


def resolve_knowledge(config, base):
    if config.semantic_knowledge_file:
        config = config.model_copy(update={"semantic_knowledge_file": str(
            (Path(base) / config.semantic_knowledge_file).resolve())})
    return config


def load_model_profile(path):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    return resolve_knowledge(LLMConfig.model_validate_json(json.dumps(raw, allow_nan=False)), path.parent)
