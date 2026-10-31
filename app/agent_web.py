# -*- coding: utf-8 -*-
"""Web pages of the AI-assistant channel: the phone upload link, the how-to-connect page, the eval dashboard."""

import os
import json
import glob

from flask import Blueprint, request, jsonify, render_template

import app as portal
import agent_core as core

agent_web = Blueprint("agent_web", __name__)
EVAL_DIR = os.environ.get("EVAL_RESULTS_DIR", "/var/sewasetu/evals")


@agent_web.route("/u/<token>", methods=["GET", "POST"])
def upload_page(token):
    """One-time link from the assistant: the applicant or a relative photographs the age proof here.
    Browsers get a page; programs (curl -F document=@file) get JSON."""
    wants_json = "text/html" not in request.headers.get("Accept", "")
    mobile = core.redeem_upload_token(token)
    if not mobile:
        problem = "This link has expired or has already been used. Ask the assistant for a new link."
        if wants_json:
            return jsonify({"uploaded": False, "problem": problem}), 410
        return render_template("agent_upload.html", state="expired", problem=problem), 410
    first_name = core.first_name_on_draft(mobile)
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or not f.filename:
            result = {"uploaded": False, "problem": "choose the photo or file of the document first"}
        else:
            result = core.attach_document(mobile, f.read(portal.MAX_DOC_BYTES + 1), f.filename, "upload link",
                                          token=token)
        if wants_json:
            return jsonify(result), (200 if result["uploaded"] else 400)
        return render_template("agent_upload.html", state="done" if result["uploaded"] else "form",
                               problem=result.get("problem"), first_name=first_name)
    return render_template("agent_upload.html", state="form", first_name=first_name)


@agent_web.route("/assistant")
def assistant_page():
    return render_template("assistant.html", citizen_url=portal.MCP_URLS["citizen"],
                           officer_url=portal.MCP_URLS["officer"], pilot=core.PILOT_BLOCKS)


def _load_eval_runs():
    runs = []
    for path in sorted(glob.glob(os.path.join(EVAL_DIR, "run-*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                runs.append(json.load(fh))
        except (OSError, ValueError):
            continue
    meta = {}
    meta_path = os.path.join(EVAL_DIR, "dashboard.json")
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    return runs, meta


@agent_web.route("/eval-dashboard")
def eval_dashboard():
    runs, meta = _load_eval_runs()
    selected = request.args.get("run")
    run = next((r for r in runs if r.get("run_id") == selected), runs[-1] if runs else None)
    return render_template("eval_dashboard.html", runs=runs, run=run, meta=meta)


@agent_web.route("/eval-dashboard.json")
def eval_dashboard_json():
    runs, meta = _load_eval_runs()
    return jsonify({"meta": meta, "runs": runs})
