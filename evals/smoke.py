# -*- coding: utf-8 -*-
"""End-to-end smoke test: a citizen applies through the citizen MCP, an officer approves through the officer MCP.

    python evals/smoke.py [base_url]       (admin credentials are read from .env)
"""

import asyncio
import base64
import io
import os
import random
import sys

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
from sewasetu_client import sign_in, mcp_session, tool_json  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")


def env(name):
    with open(os.path.join(os.path.dirname(__file__), "..", ".env"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()


def voter_id_png(name, dob):
    img = Image.new("RGB", (900, 560), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 890, 550], outline="black", width=4)
    for y, text in ((40, "ELECTION COMMISSION OF INDIA"), (90, "ELECTOR PHOTO IDENTITY CARD"),
                    (200, "Name: " + name), (260, "Date of Birth: " + dob), (320, "Sex: Female"),
                    (380, "Address: Lakhipur, Sonari, Purvanchal")):
        d.text((40, y), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def check(label, ok, detail=""):
    ok = bool(ok)
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else "  -> " + str(detail)[:400]))
    return ok


async def main():
    mobile = "9" + "".join(random.choice("0123456789") for _ in range(9))
    # a new person each run: the portal refuses a second live application for the same person
    person = random.choice(["Golapi", "Kamala", "Runu", "Minati"]) + " " + random.choice(["Bora", "Kalita", "Saikia", "Das"])
    dob = "%02d/%02d/%d" % (random.randint(1, 28), random.randint(1, 12), random.randint(1940, 1960))
    account = str(random.randint(10 ** 11, 10 ** 12 - 1))
    citizen_url, officer_url = BASE + "/mcp/citizen", BASE + "/mcp/officer"
    ok = True

    r = httpx.post(citizen_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=10)
    ok &= check("unauthenticated request gets 401 with resource metadata",
                r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""), r.headers)
    prm = httpx.get(BASE + "/.well-known/oauth-protected-resource/mcp/citizen", timeout=10).json()
    ok &= check("protected resource metadata points at the portal", prm["authorization_servers"] == [BASE], prm)

    token = sign_in(BASE, citizen_url, "citizen", mobile=mobile)
    async with mcp_session(citizen_url, token) as s:
        names = sorted(t.name for t in (await s.list_tools()).tools)
        ok &= check("citizen sees only citizen tools", "decide_application" not in names and "submit_application" in names, names)
        res = tool_json(await s.call_tool("save_application_details", {
            "applicant_name": person, "date_of_birth": dob, "gender": "Female",
            "marital_status": "widow", "village": "Lakhipur", "block": "Sonari",
            "bank_account_number": account, "ifsc": "sbin0003077", "account_in_applicant_name": True}))
        ok &= check("draft saved, only the document missing",
                    res.get("problems") == [] and any("age-proof" in m for m in res.get("still_needed", [])), res)
        link = res.get("age_proof_upload_link", "")
        up = httpx.post(link, files={"document": ("voter-id.png", voter_id_png(person, dob), "image/png")},
                        timeout=20)
        ok &= check("upload link accepts the document", up.status_code == 200 and up.json().get("uploaded"), up.text)
        again = httpx.post(link, files={"document": ("x.png", voter_id_png("x", "x"), "image/png")}, timeout=20)
        ok &= check("upload link is single-use", again.status_code == 410, again.status_code)
        rev = tool_json(await s.call_tool("review_application", {}))
        ok &= check("review is ready with a confirmation code", rev.get("ready_to_submit") and rev.get("confirmation_code"), rev)
        bad = tool_json(await s.call_tool("submit_application", {"confirmation_code": "ZZZZZZ",
                                                                  "applicant_confirmed_details": True,
                                                                  "declaration_accepted": True}))
        ok &= check("wrong confirmation code is refused", bad.get("submitted") is False, bad)
        sub = tool_json(await s.call_tool("submit_application", {"confirmation_code": rev.get("confirmation_code", ""),
                                                                  "applicant_confirmed_details": True,
                                                                  "declaration_accepted": True}))
        ok &= check("application submitted", sub.get("submitted") and sub.get("application_number"), sub)
        app_no = sub.get("application_number")
        dup = tool_json(await s.call_tool("save_application_details", {"applicant_name": "Haren Bora"}))
        ok &= check("second application on the same mobile is flagged",
                    any("already has a live application" in p for p in dup.get("problems", [])), dup)
        mine = tool_json(await s.call_tool("get_my_applications", {}))
        ok &= check("status visible to the citizen", (mine.get("applications") or [{}])[0].get("status") == "PENDING", mine)

    young = sign_in(BASE, citizen_url, "citizen", mobile="8" + mobile[1:])
    async with mcp_session(citizen_url, young) as s:
        res = tool_json(await s.call_tool("save_application_details", {
            "applicant_name": "Nagen Hazarika", "date_of_birth": "15/03/1967", "village": "Bordoloni",
            "block": "Dhemaji Pathar", "bank_account_number": "123456789012", "ifsc": "UTBI0PUR2819",
            "account_in_applicant_name": True}))
        ok &= check("under-60 and 12-character IFSC are reported",
                    any("60 years" in p for p in res.get("problems", [])) and any("IFSC" in p for p in res.get("problems", [])), res)

    officer_token = sign_in(BASE, officer_url, "officer", username=env("ADMIN_USERNAME") or "admin", password=env("ADMIN_PASSWORD"))
    async with mcp_session(officer_url, officer_token) as s:
        names = sorted(t.name for t in (await s.list_tools()).tools)
        ok &= check("officer sees officer tools", "decide_application" in names and "submit_application" not in names, names)
        q = tool_json(await s.call_tool("get_my_queue", {}))
        ok &= check("new application is in the queue", any(i["application_number"] == app_no for i in q.get("pending", [])), q)
        early = tool_json(await s.call_tool("decide_application", {"application_number": app_no, "decision": "approve",
                                                                    "reason": "looks fine", "officer_confirmed": True}))
        ok &= check("deciding without opening the case is refused", early.get("recorded") is False, early)
        case = tool_json(await s.call_tool("get_case", {"application_number": app_no}))
        ok &= check("case opens with checks", case.get("found") and case["checks"]["can_be_approved"], case)
        img = await s.call_tool("view_age_proof", {"application_number": app_no})
        ok &= check("age proof returned as an image", any(c.type == "image" for c in img.content), img)
        wrong = tool_json(await s.call_tool("decide_application", {"application_number": app_no, "decision": "approve",
                                                                    "reason": "Voter ID checked", "officer_confirmed": True,
                                                                    "dob_on_document": "01/01/1990"}))
        ok &= check("approval refused when the document date differs", wrong.get("recorded") is False, wrong)
        dec = tool_json(await s.call_tool("decide_application", {"application_number": app_no, "decision": "approve",
                                                                  "reason": "Voter ID checked; name and DOB match",
                                                                  "dob_on_document": dob,
                                                                  "officer_confirmed": True}))
        ok &= check("officer's approval recorded", dec.get("recorded") and dec.get("status") == "APPROVED", dec)

    try:
        async with mcp_session(officer_url, token) as s:
            await s.list_tools()
        ok &= check("citizen token rejected by officer server", False, "listed tools")
    except Exception:
        ok &= check("citizen token rejected by officer server", True)
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    print("application:", app_no, "mobile:", mobile)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
