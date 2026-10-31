# -*- coding: utf-8 -*-
"""Azure OpenAI chat completions over plain HTTPS, configured from the project's .env (values never printed)."""

import os
import time

import httpx

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load_env(path=os.path.join(ROOT, ".env")):
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = load_env()
MODEL_NAME = ENV.get("AZURE_OPENAI_MODEL") or ENV.get("AZURE_OPENAI_DEPLOYMENT")


def chat(messages, tools=None, temperature=0.2, max_tokens=900):
    """One chat completion; returns the assistant message dict (content, tool_calls)."""
    url = "%s/openai/deployments/%s/chat/completions?api-version=%s" % (
        ENV["AZURE_OPENAI_ENDPOINT"].rstrip("/"), ENV["AZURE_OPENAI_DEPLOYMENT"], ENV["AZURE_OPENAI_API_VERSION"])
    body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    for attempt in range(6):
        r = httpx.post(url, json=body, headers={"api-key": ENV["AZURE_OPENAI_API_KEY"]}, timeout=120)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(30, 3 * (attempt + 1)))
            continue
        if r.status_code != 200:
            raise RuntimeError("Azure OpenAI error %s: %s" % (r.status_code, r.text[:300]))
        return r.json()["choices"][0]["message"]
    raise RuntimeError("Azure OpenAI kept failing (rate limit or server error)")


if __name__ == "__main__":
    reply = chat([{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=5)
    print("model:", MODEL_NAME, "| reply:", reply.get("content"))
