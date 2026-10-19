"""
ai_brain.py - one interface for several AI back-ends.

Providers (all optional, all use plain HTTPS/HTTP - no SDKs needed):
  gemini         Google Gemini API (free tier available for eligible accounts)
  openai_compat  any OpenAI-style /chat/completions endpoint: Groq, OpenRouter,
                 LM Studio, llama.cpp server, a paid OpenAI key, ...
  ollama         local models served by Ollama (http://localhost:11434)

Brain.complete() walks the provider order from Settings, skips providers that
are disabled / have no key / hit their daily limit, and falls back to the next
one on any error. Callers handle AIUnavailable with an offline fallback.

Note: ChatGPT / Claude *chat* subscriptions do not include API access - an API
key from the provider's developer console is a separate thing.
"""
import json
import re
import urllib.error
import urllib.request

import config

PROVIDER_LABELS = {"gemini": "Gemini", "openai_compat": "OpenAI-compatible", "ollama": "Ollama (local)"}


class AIUnavailable(Exception):
    """No provider could answer."""


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:160]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"connection failed: {e.reason}")


def parse_json(text):
    """Extract the first JSON object from a model reply (tolerates ``` fences / chatter)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.M)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("model did not return JSON")


# ---- one function per provider: (cfg, key, system, prompt, json_mode, temperature) -> text
def _gemini(cfg, key, system, prompt, json_mode, temperature):
    url = f"{cfg['base_url'].rstrip('/')}/models/{cfg['model']}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    data = _post(url, body, {"x-goog-api-key": key}, 45)
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _openai_compat(cfg, key, system, prompt, json_mode, temperature):
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": cfg["model"], "messages": msgs, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return _post(url, body, headers, 60)["choices"][0]["message"]["content"]


def _ollama(cfg, key, system, prompt, json_mode, temperature):
    url = f"{cfg['base_url'].rstrip('/')}/api/chat"
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": cfg["model"], "messages": msgs, "stream": False,
            "options": {"temperature": temperature}}
    if json_mode:
        body["format"] = "json"
    return _post(url, body, {}, 180)["message"]["content"]      # small local models can be slow


CALLERS = {"gemini": _gemini, "openai_compat": _openai_compat, "ollama": _ollama}
NEEDS_KEY = {"gemini": True, "openai_compat": False, "ollama": False}


class Brain:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else config.load_settings()

    def reload(self, settings):
        self.settings = settings

    def status(self):
        """[(provider, label, state_text)] for the Settings page."""
        out = []
        for name in self.settings["provider_order"]:
            cfg = self.settings["providers"][name]
            if not cfg.get("enabled"):
                state = "off"
            elif NEEDS_KEY[name] and not config.has_secret(name):
                state = "no key"
            else:
                used, lim = config.usage_count(name), cfg.get("daily_limit", 0)
                state = f"ready ({used}/{lim} today)" if lim else f"ready ({used} today)"
            out.append((name, PROVIDER_LABELS[name], state))
        return out

    def any_available(self):
        return any(s.startswith("ready") for _, _, s in self.status())

    def complete(self, prompt, system=None, json_mode=True, temperature=0.7, log=None):
        """Return (text, provider_name). Raises AIUnavailable if every provider fails."""
        log = log or (lambda m: None)
        errors = []
        for name in self.settings["provider_order"]:
            cfg = self.settings["providers"][name]
            if not cfg.get("enabled"):
                continue
            key = config.get_secret(name)
            if NEEDS_KEY[name] and not key:
                errors.append(f"{name}: no API key")
                continue
            limit = cfg.get("daily_limit", 0)
            if limit and config.usage_count(name) >= limit:
                errors.append(f"{name}: daily limit reached")
                continue
            try:
                text = CALLERS[name](cfg, key, system, prompt, json_mode, temperature)
                config.usage_add(name)
                return text, name
            except Exception as e:
                errors.append(f"{name}: {e}")
                log(f"AI provider {name} failed ({e}); trying next...")
        raise AIUnavailable("; ".join(errors) or "no AI provider is enabled")

    def complete_json(self, prompt, system=None, temperature=0.7, log=None):
        """Like complete() but parses JSON. Returns (dict, provider_name)."""
        text, name = self.complete(prompt, system, True, temperature, log)
        return parse_json(text), name

    def test(self, name):
        """Ping one provider; returns (ok, message)."""
        cfg = self.settings["providers"][name]
        try:
            key = config.get_secret(name)
            if NEEDS_KEY[name] and not key:
                return False, "No API key saved."
            text = CALLERS[name](cfg, key, None, 'Reply with the JSON {"ok": true}', True, 0)
            return True, "Connected: " + text.strip()[:60]
        except Exception as e:
            return False, str(e)[:160]
