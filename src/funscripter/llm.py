"""Explicit local-service adapters. No provider fallback or SDK dependency."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_request(config, messages, schema):
    base = config.base_url.rstrip("/")
    body = {"model": config.model_id, "messages": messages, "stream": False}
    sampling = {key: value for key, value in {"temperature": config.temperature, "top_p": config.top_p}.items()
                if value is not None}
    if config.backend == "llama_cpp":
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        body.update(sampling)
        body["max_tokens"] = config.max_output_tokens
        body["response_format"] = {"type": "json_object", "schema": schema}
        if config.thinking != "backend_default":
            body["chat_template_kwargs"] = {"enable_thinking": config.thinking}
    else:
        url = base + ("/chat" if base.endswith("/api") else "/api/chat")
        body["format"] = schema
        body["options"] = sampling | {"num_predict": config.max_output_tokens,
                                       "num_ctx": config.context_budget_tokens}
        if config.thinking != "backend_default":
            body["think"] = config.thinking
    return url, body


def chat(config, messages, schema):
    url, body = make_request(config, messages, schema)
    headers = {"Content-Type": "application/json"}
    if config.api_key_env:
        secret = os.environ.get(config.api_key_env)
        if not secret:
            raise ValueError("configured API key environment variable is missing")
        headers["Authorization"] = "Bearer " + secret
    request = Request(url, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(), headers=headers)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=config.timeout_seconds) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("model_response_too_large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("model_response_not_object")
        if config.backend == "llama_cpp":
            choice = payload["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("model_response_incomplete_or_refused")
            message = choice["message"]
        else:
            if payload.get("done") is not True or payload.get("done_reason") not in (None, "stop"):
                raise ValueError("model_response_incomplete_or_refused")
            message = payload["message"]
        if message.get("refusal"):
            raise ValueError("model_refusal")
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model_content_empty")
        return content, {key: payload[key] for key in ("model", "usage", "prompt_eval_count", "eval_count") if key in payload}
    except HTTPError as exc:
        exc.close()
        raise ValueError(f"model_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("model_connection_or_timeout") from None
    except (KeyError, IndexError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("model_response_invalid_envelope") from None
