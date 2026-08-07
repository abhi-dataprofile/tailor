"""llm.py — backend multi-provider LLM layer (stdlib only).

Mirrors the browser's provider abstraction so backend pipelines can use ANY of
the current providers (not just Claude). Configure per operator via env; `auto`
tries hosted keys first, then local Ollama, then raises. Same fallback contract
as the in-app `gen()`.

Env:
  LLM_PROVIDER = auto | claude | gemini | openai | custom | ollama   (default auto)
  ANTHROPIC_API_KEY   CLAUDE_MODEL   (default claude-sonnet-4-6)
  GEMINI_API_KEY      GEMINI_MODEL   (default gemini-2.5-flash)
  OPENAI_API_KEY      OPENAI_MODEL   (default gpt-5-mini)
  CUSTOM_BASE_URL     CUSTOM_API_KEY   CUSTOM_MODEL   (OpenAI-compatible: OpenRouter, DeepSeek, GLM…)
  OLLAMA_URL (default http://localhost:11434)   OLLAMA_MODEL (default qwen2.5:3b)
"""
import os, json, urllib.request, urllib.error

def _env(k, d=""):
    return os.environ.get(k, d)

PROVIDER      = _env("LLM_PROVIDER", "auto")
CLAUDE_KEY    = _env("ANTHROPIC_API_KEY");  CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-4-6")
GEMINI_KEY    = _env("GEMINI_API_KEY");     GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")
OPENAI_KEY    = _env("OPENAI_API_KEY");     OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-5-mini")
CUSTOM_URL    = _env("CUSTOM_BASE_URL", "https://openrouter.ai/api/v1")
CUSTOM_KEY    = _env("CUSTOM_API_KEY");     CUSTOM_MODEL = _env("CUSTOM_MODEL", "deepseek/deepseek-v4-flash")
OLLAMA_URL    = _env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL  = _env("OLLAMA_MODEL", "qwen2.5:3b")

def _post(url, body, headers, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _json_hint(system, want_json):
    return system + (" Respond with valid JSON only — no markdown, no commentary." if want_json else "")

# ---- providers ----
def openai_chat(system, user, json_mode=False, temp=None, max_tokens=1200):
    body = {"model": OPENAI_MODEL, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": _json_hint(system, json_mode)}, {"role": "user", "content": user}]}
    if temp is not None: body["temperature"] = temp
    if json_mode: body["response_format"] = {"type": "json_object"}
    j = _post("https://api.openai.com/v1/chat/completions", body, {"Authorization": "Bearer " + OPENAI_KEY})
    return (((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()

def custom_chat(system, user, json_mode=False, temp=None, max_tokens=1200):
    body = {"model": CUSTOM_MODEL, "max_tokens": max_tokens, "temperature": 0.4 if temp is None else temp,
            "messages": [{"role": "system", "content": _json_hint(system, json_mode)}, {"role": "user", "content": user}]}
    if json_mode: body["response_format"] = {"type": "json_object"}
    j = _post(CUSTOM_URL.rstrip("/") + "/chat/completions", body, {"Authorization": "Bearer " + CUSTOM_KEY})
    return (((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()

def gemini_chat(system, user, json_mode=False, temp=None, max_tokens=1200):
    cfg = {"temperature": 0.4 if temp is None else temp, "maxOutputTokens": max_tokens}
    if json_mode: cfg["responseMimeType"] = "application/json"
    body = {"contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": _json_hint(system, json_mode)}]}, "generationConfig": cfg}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    j = _post(url, body, {})
    parts = (((j.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text", "") for p in parts).strip()

def claude_chat(system, user, json_mode=False, temp=None, max_tokens=1200):
    body = {"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": _json_hint(system, json_mode),
            "messages": [{"role": "user", "content": user}], "temperature": 0.4 if temp is None else temp}
    j = _post("https://api.anthropic.com/v1/messages", body,
              {"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in (j.get("content") or []) if b.get("type") == "text").strip()

def ollama_chat(system, user, json_mode=False, temp=None, max_tokens=1024):
    body = {"model": OLLAMA_MODEL, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.3 if temp is None else temp, "num_predict": max_tokens}}
    if json_mode: body["format"] = "json"
    j = _post(OLLAMA_URL + "/api/chat", body, {})
    return ((j.get("message") or {}).get("content") or "").strip()

def _ollama_up():
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=6); return True
    except Exception:
        return False

_HAS = {"claude": lambda: bool(CLAUDE_KEY), "gemini": lambda: bool(GEMINI_KEY),
        "openai": lambda: bool(OPENAI_KEY), "custom": lambda: bool(CUSTOM_KEY and CUSTOM_URL)}

def available():
    """Ordered list of providers ready to use, given current env/config."""
    out = [p for p in ("claude", "gemini", "openai", "custom") if _HAS[p]()]
    if _ollama_up():
        out.append("ollama")
    return out

_FN = {"claude": claude_chat, "gemini": gemini_chat, "openai": openai_chat, "custom": custom_chat, "ollama": ollama_chat}

def gen(system, user, json_mode=False, temp=None, max_tokens=1200):
    """Generate using the selected provider; `auto` = hosted keys first, then Ollama, with fallback."""
    if PROVIDER in _FN:
        order = [PROVIDER]
    else:  # auto — try any configured hosted provider, then always attempt Ollama last
        order = [p for p in ("claude", "gemini", "openai", "custom") if _HAS[p]()] + ["ollama"]
    last = None
    for p in order:
        try:
            return _FN[p](system, user, json_mode=json_mode, temp=temp, max_tokens=max_tokens)
        except Exception as e:
            last = e
            if PROVIDER in _FN:   # a pinned provider must not silently fall back
                raise
    raise RuntimeError(f"all providers failed: {last}")

if __name__ == "__main__":
    print("configured provider:", PROVIDER, "| available:", available())
    if available():
        print("test →", gen("You are terse.", "Reply with exactly: OK", max_tokens=8)[:60])
