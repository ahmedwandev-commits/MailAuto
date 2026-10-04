"""Mock of SEDCO CEM (cem-api_lb2 .../CVMServer) as seen in Logic.docx."""
from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session

from . import state as S

HERE = Path(__file__).parent
app = Flask("mock_cem", template_folder=str(HERE / "templates"), static_folder=str(HERE / "static"))
app.secret_key = "mock-cem-secret"
app.config["SESSION_COOKIE_NAME"] = "cem_session"  # both mocks run on 127.0.0.1

USERS_URL = "/CVMServer/List/EntityList?pSelectedTab=User&pEntitieName=User"
PERMS = [("supervising", "Supervising"), ("monitoring", "Monitoring"), ("serving", "Serving"),
         ("alerts_receiving", "Alerts Receiving"), ("serving_random", "Serving and Random Call")]


def read_url(pid):
    return f"/CVMServer/User/Read?pSelectedTab=User&pEntityName=User&pID={pid}"


def who():
    return session.get("who")


def guard():
    return None if who() else redirect("/CVMServer/Login?returnUrl=%2Fcvmserver")


def get_user(pid):
    try:
        return S.STATE["users"].get(int(pid))
    except (TypeError, ValueError):
        return None


@app.route("/")
def root():
    return redirect("/CVMServer/Login?returnUrl=%2Fcvmserver")


@app.route("/CVMServer/Login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u, p = request.form.get("UserName", "").strip(), request.form.get("Password", "")
        if u == os.environ.get("CEM_USERNAME", os.environ.get("PORTAL_USERNAME", "233786")) and \
                p == os.environ.get("CEM_PASSWORD", os.environ.get("PORTAL_PASSWORD", "Mock@12345")):
            session["who"] = f"{u} - Mock Operator"
            S.log("cem", f"login {u}")
            return redirect("/cvmserver")
        error = "Invalid user name or password"
    return render_template("cem/login.html", error=error, who=None, title="Login")


@app.route("/cvmserver")
@app.route("/CVMServer")
def home():
    return guard() or render_template("cem/users.html", who=who(), users=[], total=len(S.STATE["users"]), flt="",
                                      title="CEM")


@app.route("/CVMServer/List/EntityList")
def entity_list():
    if guard():
        return guard()
    flt = (request.args.get("pFilter") or "").strip().lower()
    users = sorted(S.STATE["users"].values(), key=lambda u: u["pid"])
    if flt:
        users = [u for u in users if flt in u["login"].lower() or flt in u["name_en"].lower()
                 or flt in u["department"].lower()]
    return render_template("cem/users.html", who=who(), users=users, total=len(S.STATE["users"]), flt=flt,
                           title="Users")


@app.route("/CVMServer/User/Read")
def user_read():
    if guard():
        return guard()
    u = get_user(request.args.get("pID"))
    if not u:
        return "User not found", 404
    return render_template("cem/user_read.html", who=who(), u=u, title=u["name_en"])


@app.route("/CVMServer/User/Edit", methods=["GET", "POST"])
def user_edit():
    if guard():
        return guard()
    u = get_user(request.args.get("pID"))
    if not u:
        return "User not found", 404
    if request.method == "POST":
        f = request.form
        with S.LOCK:
            u.update(name_en=f.get("name_en", u["name_en"]), name_ar=f.get("name_ar", u["name_ar"]),
                     auth_type=f.get("auth_type", u["auth_type"]),
                     active=f.get("active") == "1", locked=f.get("locked") == "1", modified_by=who())
        S.log("cem", f"edit user {u['login']}: active={u['active']} locked={u['locked']}")
        return redirect(read_url(u["pid"]))
    return render_template("cem/user_form.html", who=who(), u=u, create=False, title="Edit")


@app.route("/CVMServer/api/verify")
def verify():
    login = (request.args.get("login") or "").strip()
    return jsonify({"ok": bool(re.fullmatch(r"[1-8]\d{5}", login))})


@app.route("/CVMServer/User/Create", methods=["GET", "POST"])
def user_create():
    if guard():
        return guard()
    blank = {"pid": None, "name_en": "", "name_ar": "", "login": "", "auth_type": "Windows",
             "department": "", "active": True, "locked": False}
    if request.method == "POST":
        f = request.form
        login = f.get("login", "").strip()
        data = dict(blank, name_en=f.get("name_en", "").strip(), name_ar=f.get("name_ar", "").strip(), login=login,
                    auth_type=f.get("auth_type", ""), department=f.get("department", ""),
                    active=f.get("active") == "1", locked=f.get("locked") == "1")
        error = None
        if not data["name_en"] or not data["name_ar"]:
            error = "Name is required"
        elif not login:
            error = "Login Name is required"
        elif f.get("verified_login") != login:
            error = "Login Name must be verified"
        elif not data["department"]:
            error = "Department is required"
        elif any(x["login"] == login for x in S.STATE["users"].values()):
            error = "Login Name already exists"
        if error:
            return render_template("cem/user_form.html", who=who(), u=data, create=True, error=error, title="Create User")
        with S.LOCK:
            pid = S.next_id()
            data.update(pid=pid, roles=[], branches=[], created_by=who(), modified_by="")
            S.STATE["users"][pid] = data
        S.log("cem", f"created user {login} ({data['name_en']}) dept={data['department']} auth={data['auth_type']}")
        return redirect(read_url(pid))
    return render_template("cem/user_form.html", who=who(), u=blank, create=True, title="Create User")


@app.route("/CVMServer/List/ManyToManyRelationList", methods=["GET", "POST"])
def relation_list():
    if guard():
        return guard()
    kind = "role" if request.args.get("pEntitieName") == "Role" else "branch"
    u = get_user(request.args.get("pRelatedMTMID"))
    if not u:
        return "User not found", 404
    if request.method == "POST":
        action, ids = request.form.get("action"), request.form.getlist("ids")
        with S.LOCK:
            if kind == "role":
                if action == "add":
                    u["roles"] += [r for r in ids if r not in u["roles"]]
                elif action == "remove":
                    u["roles"] = [r for r in u["roles"] if r not in ids]
            else:
                if action == "add":
                    for ident in ids:
                        if not any(b["identity"] == ident for b in u["branches"]):
                            u["branches"].append({"link": S.next_id(), "identity": ident, "grants": []})
                elif action == "remove":
                    u["branches"] = [b for b in u["branches"] if str(b["link"]) not in ids]
        S.log("cem", f"{kind} {action} {ids} for user {u['login']}")
        return redirect(request.full_path)

    if kind == "role":
        all_roles = {r["name"]: r for r in S.ROLES}
        rows = [{"id": r, "cells": [r, "•", all_roles.get(r, {}).get("modified", "")]} for r in u["roles"]]
        avail = [{"id": r["name"], "cells": [r["name"], "•", r["modified"]]} for r in S.ROLES if r["name"] not in u["roles"]]
        ctx = dict(heading="Roles", cols=["Name", "Active", "Last Modification Time"], modal_title="Available Roles",
                   avail_cols=["Name", "Active", "Last Modification Time"])
    else:
        rows = []
        for b in u["branches"]:
            br = S.branch(b["identity"]) or {"name": b["identity"]}
            g = b["grants"]
            rows.append({"id": b["link"], "href": f"/CVMServer/QueueBranchUser/Read?pSelectedTab=User&pEntityName=QueueBranchUser"
                                                  f"&pOwnerEntityName=User&pRelatedMTMID={u['pid']}&pID={b['link']}",
                         "cells": [br["name"]] + ["•" if k in g else "" for k, _ in PERMS] + ["•", "Centralized"]})
        linked = {b["identity"] for b in u["branches"]}
        avail = [{"id": b["identity"], "cells": [b["name"], "•", "Main", "", "Disconnected", b["identity"]]}
                 for b in S.BRANCHES if b["identity"] not in linked]
        ctx = dict(heading="Branches", cols=["Name", "Supervising", "Monitoring", "Serving", "Alerts Receiving",
                                             "Serving and Random ...", "Active", "Mode"],
                   modal_title="Available Branches",
                   avail_cols=["Name", "Active", "Department Name", "IP Address", "State", "Identity"])
    return render_template("cem/relation.html", who=who(), u=u, kind=kind, rows=rows, avail=avail,
                           title=ctx["heading"], **ctx)


@app.route("/CVMServer/QueueBranchUser/Read", methods=["GET", "POST"])
def branch_perm():
    if guard():
        return guard()
    u = get_user(request.args.get("pRelatedMTMID"))
    link = next((b for b in (u or {}).get("branches", []) if str(b["link"]) == request.args.get("pID")), None)
    if not u or not link:
        return "Not found", 404
    if request.method == "POST":
        perm, op = request.form.get("perm"), request.form.get("op")
        with S.LOCK:
            if op == "grant" and perm not in link["grants"]:
                link["grants"].append(perm)
            elif op == "deny" and perm in link["grants"]:
                link["grants"].remove(perm)
        S.log("cem", f"{op} {perm} on branch {link['identity']} for {u['login']}")
        return redirect(request.full_path)
    b = S.branch(link["identity"]) or {"name": link["identity"]}
    return render_template("cem/branch_perm.html", who=who(), u=u, b=b, link=link, perms=PERMS,
                           title="Branch Users Permissions")


@app.route("/__state")
def get_state():
    return jsonify(S.snapshot())


@app.route("/__reset", methods=["POST"])
def do_reset():
    S.reset()
    return jsonify({"ok": True})
