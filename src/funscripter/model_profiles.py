"""Resolve optional knowledge paths relative to their owning YAML profile."""
import json
from pathlib import Path
import yaml
from .models import LLMConfig


def resolve_knowledge(config, base):
    updates = {field: str((Path(base) / getattr(config, field)).resolve())
               for field in ("semantic_knowledge_file", "outline_seed_dir") if getattr(config, field)}
    if updates:
        config = config.model_copy(update=updates)
    return config


def load_model_profile(path):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    return resolve_knowledge(LLMConfig.model_validate_json(json.dumps(raw, allow_nan=False)), path.parent)
