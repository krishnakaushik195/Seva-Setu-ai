# -*- coding: utf-8 -*-
"""Sign in to Sewa Setu the way a person does (OAuth 2.1 + PKCE, OTP read from the simulated SMS
gateway, consent) and talk to the MCP servers through the official MCP client over streamable HTTP."""

import base64
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REDIRECT = "http://localhost:9999/callback"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _form_action(html):
    return None


def latest_otp(base, mobile, after_id=0, wait=15):
    """The newest delivered OTP for mobile from the simulated SMS gateway."""
    deadline = time.time() + wait
    while time.time() < deadline:
        msgs = httpx.get(base + "/__gateway/api/messages", params={"to": mobile}, timeout=10).json()["messages"]
        for m in reversed(msgs):
            if m["id"] > after_id and m["delivered"] and m["text"]:
                found = re.search(r"OTP is (\d{6})", m["text"])
                if found:
                    return found.group(1)
        time.sleep(1)
    raise RuntimeError("no OTP delivered to %s" % mobile)


def last_message_id(base, mobile):
    msgs = httpx.get(base + "/__gateway/api/messages", params={"to": mobile}, timeout=10).json()["messages"]
    return max([m["id"] for m in msgs] or [0])


def sign_in(base, resource, role, mobile=None, username=None, password=None, client_name="Sewa Setu eval client"):
    """Return an access token for `resource`, signing in as a citizen (mobile + OTP) or an officer."""
    with httpx.Client(base_url=base, follow_redirects=False, timeout=20) as http:
        reg = http.post("/oauth/register", json={"client_name": client_name, "redirect_uris": [REDIRECT],
                                                 "token_endpoint_auth_method": "none"}).json()
        client_id = reg["client_id"]
        verifier, challenge = _pkce()
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "s1",
                  "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource}
        r = http.get("/oauth/authorize?" + urlencode(params))
        login_url = r.headers["location"]
        if role == "citizen":
            before = last_message_id(base, mobile)
            http.post(login_url, data={"mobile": mobile})
            r = http.post(login_url, data={"otp": latest_otp(base, mobile, before)})
        else:
            r = http.post(login_url, data={"username": username, "password": password})
        if r.status_code != 302:
            raise RuntimeError("sign-in failed: %s" % re.sub(r"\s+", " ", r.text)[:300])
        consent_url = r.headers["location"]
        r = http.post(consent_url, data={"decision": "allow"})
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = http.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                              "code_verifier": verifier, "client_id": client_id,
                                              "redirect_uri": REDIRECT, "resource": resource}).json()
        return tok["access_token"]


@asynccontextmanager
async def mcp_session(url, token):
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def tool_text(result):
    """The tool result as text (images summarised)."""
    parts = []
    for c in result.content:
        if c.type == "text":
            parts.append(c.text)
        elif c.type == "image":
            parts.append("[image %s, %d bytes base64]" % (c.mimeType, len(c.data)))
    return "\n".join(parts)


def tool_json(result):
    try:
        return json.loads(tool_text(result))
    except ValueError:
        return {"text": tool_text(result), "is_error": result.isError}
