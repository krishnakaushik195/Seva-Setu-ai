# -*- coding: utf-8 -*-
"""Chat in the terminal with GPT-4o connected to a Sewa Setu MCP server; you play the citizen or the officer.

    python evals/chat.py citizen [mobile]   signs in with the OTP read from the local SMS gateway
    python evals/chat.py officer            signs in as ADMIN_USERNAME / ADMIN_PASSWORD from .env

Every tool call the assistant makes is printed. Type 'exit' to stop.
"""

import asyncio
import json
import os
import random
import sys
from datetime import datetime

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sewasetu_client import sign_in, tool_text  # noqa: E402
from llm import chat, ENV  # noqa: E402

BASE = os.environ.get("SEWASETU_BASE", "http://localhost:8000").rstrip("/")
SYSTEM = ("You are a helpful general-purpose AI assistant chatting with a user on their phone. The user has "
          "connected the 'Sewa Setu' connector of the Government of Purvanchal; use its tools when they help. "
          "Today is %s.\n\nInstructions from the Sewa Setu connector:\n%s")


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    role = sys.argv[1] if len(sys.argv) > 1 else "citizen"
    url = "%s/mcp/%s" % (BASE, role)
    if role == "citizen":
        mobile = sys.argv[2] if len(sys.argv) > 2 else \
            random.choice("6789") + "".join(random.choice("0123456789") for _ in range(9))
        print("Signing in as a citizen with mobile %s (OTP read from the SMS gateway)..." % mobile)
        token = sign_in(BASE, url, "citizen", mobile=mobile, client_name="Terminal chat (GPT-4o)")
    else:
        user = ENV.get("ADMIN_USERNAME") or "admin"
        print("Signing in as officer %s..." % user)
        token = sign_in(BASE, url, "officer", username=user, password=ENV["ADMIN_PASSWORD"],
                        client_name="Terminal chat (GPT-4o)")
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = (await session.list_tools()).tools
            print("Connected to %s. Tools: %s" % (url, ", ".join(t.name for t in tools)))
            print("Type as the %s. 'exit' to stop." % role)
            oa_tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                          "parameters": t.inputSchema}} for t in tools]
            messages = [{"role": "system", "content": SYSTEM % (datetime.now().strftime("%d %B %Y"),
                                                                init.instructions or "")}]
            while True:
                try:
                    text = input("\nYou: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if text.lower() in ("exit", "quit"):
                    break
                if not text:
                    continue
                messages.append({"role": "user", "content": text})
                for _ in range(10):
                    msg = await asyncio.to_thread(chat, messages, oa_tools)
                    calls = msg.get("tool_calls") or []
                    if not calls:
                        messages.append({"role": "assistant", "content": msg.get("content") or ""})
                        print("\nAssistant: %s" % (msg.get("content") or ""))
                        break
                    messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                    images = []
                    for call in calls:
                        name = call["function"]["name"]
                        args = json.loads(call["function"].get("arguments") or "{}")
                        print("  [tool] %s %s" % (name, json.dumps(args, ensure_ascii=False)[:220]))
                        result = await session.call_tool(name, args)
                        images += [c for c in result.content if c.type == "image"]
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": tool_text(result)[:8000]})
                    if images:
                        messages.append({"role": "user", "content": [{"type": "text", "text": "(image returned by the tool)"}] +
                                         [{"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (i.mimeType, i.data)}}
                                          for i in images]})


if __name__ == "__main__":
    asyncio.run(main())
