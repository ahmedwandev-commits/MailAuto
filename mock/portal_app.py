"""Mock of the Security Code Request portal (nbesrv .../SecurityCodeRequest).

Behaves like the screenshots in Logic.docx: SSO login, user groups page,
re-assign page, "الطلبات الجديدة-مستخدم سرية" page with the decision panel.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from . import state as S

HERE = Path(__file__).parent
app = Flask("mock_portal", template_folder=str(HERE / "templates"), static_folder=str(HERE / "static"))
app.secret_key = "mock-portal-secret"
app.config["SESSION_COOKIE_NAME"] = "portal_session"  # both mocks run on 127.0.0.1

BASE = "/SecurityCodeRequest/Security_Assign_Maker"
SYSTEMS = [S.CEM_SYSTEM, "OBDX نظام آخر", "CRM نظام العملاء"]
DECISIONS = {"1": "تم التنفيذ", "2": "لم يتم التنفيذ"}
REASONS = {"11": "ليس لدية كود", "12": "الطلب غير مستوفى", "99": "Other"}


def _valid_user(u, p):
    exp_u = os.environ.get("PORTAL_USERNAME", "233786")
    exp_p = os.environ.get("PORTAL_PASSWORD", "Mock@12345")
    return u == exp_u and p == exp_p


def _need_login():
    return "user" not in session


@app.route("/")
def root():
    return redirect("/App_Security_Login/Login.aspx")


@app.route("/App_Security_Login/Login.aspx", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("ctl00$txtUserName", "").strip()
        p = request.form.get("ctl00$txtPassword", "")
        if _valid_user(u, p):
            session.clear()
            session["user"] = u
            S.log("portal", f"login {u}")
            return redirect(f"/App_Security_Login/UserGroups.aspx?SID={uuid.uuid4()}")
        error = "اسم المستخدم او كلمة المرور غير صحيح"
    return render_template("portal/login.html", error=error)


@app.route("/App_Security_Login/UserGroups.aspx")
def groups():
    if _need_login():
        return redirect(url_for("login"))
    groups = [
        {"name": "السرية الموحد معلني السرية الموحد"},
        {"name": "Backend - HR i-Rec-Administrator (Security Group) - HR i-REC"},
        {"name": "Notification Hub-SMS - User"},
        {"name": "User Provisioning Portal-مدخل البيانات بوحدة الأكواد السرية"},
        {"name": "SSO_ThirdParty-Maker"},
        {"name": "User Provisioning Portal-موظف اسناد - مستخدم السرية", "target": f"{BASE}/Default.aspx"},
        {"name": "Shorting URL-Provisioning maker"},
    ]
    return render_template("portal/groups.html", groups=groups, user=session["user"])


@app.route(f"{BASE}/Default.aspx")
def home():
    if _need_login():
        return redirect(url_for("login"))
    return render_template("portal/home.html", user=session["user"], page="home")


def _visible_for_reassign(system):
    return [t for t in S.STATE["tickets"] if t["status"] == "open" and t["system"] == system]


def _visible_for_making(system):
    return [t for t in S.STATE["tickets"]
            if t["status"] == "open" and t["system"] == system and t["assigned_to"] == session["user"]]


@app.route(f"{BASE}/Request_ReAssign.aspx", methods=["GET", "POST"])
def reassign():
    if _need_login():
        return redirect(url_for("login"))
    ctx = dict(user=session["user"], page="reassign", systems=SYSTEMS, employees=S.EMPLOYEES,
               system=None, searched=False, tickets=[], message=None, error=None)
    if request.method == "POST":
        action = request.form.get("action")
        system = request.form.get("system") or ""
        ctx["system"] = system
        with S.LOCK:
            if action == "assign":
                ids = [int(x) for x in request.form.getlist("ticket_ids")]
                assignee = request.form.get("assignee", "")
                if not ids:
                    ctx["error"] = "يجب اختيار طلب واحد على الأقل"
                elif not assignee:
                    ctx["error"] = "يجب اختيار مستخدم السرية"
                else:
                    for t in S.STATE["tickets"]:
                        if t["id"] in ids:
                            t["assigned_to"] = assignee
                    S.log("portal", f"reassigned {ids} -> {assignee}")
                    ctx["message"] = f"تم إعادة الإسناد بنجاح ({len(ids)} طلب)"
            if not system:
                ctx["error"] = ctx["error"] or "يجب اختيار النظام"
            else:
                ctx["searched"] = True
                ctx["tickets"] = _visible_for_reassign(system)
    return render_template("portal/reassign.html", **ctx)


def _needs_matrix(ticket_type: str) -> bool:
    t = ticket_type.replace("إ", "ا").replace("أ", "ا")
    return "تعديل" in t or "نشاء" in t


@app.route(f"{BASE}/Request_Making.aspx", methods=["GET", "POST"])
def making():
    if _need_login():
        return redirect(url_for("login"))
    ctx = dict(user=session["user"], page="making", systems=SYSTEMS, system=session.get("making_system"),
               searched=False, tickets=[], message=None, error=None)
    if request.method == "POST":
        action = request.form.get("action")
        system = request.form.get("system") or session.get("making_system") or ""
        session["making_system"] = system
        ctx["system"] = system
        with S.LOCK:
            if action == "decide":
                ctx["message"], ctx["error"] = _decide(request.form)
            if system:
                ctx["searched"] = True
                ctx["tickets"] = _visible_for_making(system)
            else:
                ctx["error"] = ctx["error"] or "يجب اختيار النظام"
    return render_template("portal/making.html", **ctx)


def _decide(form):
    try:
        tid = int(form.get("ticket_id") or 0)
    except ValueError:
        tid = 0
    t = next((x for x in S.STATE["tickets"] if x["id"] == tid), None)
    if not t or t["status"] != "open" or t["assigned_to"] != session["user"]:
        return None, "يجب اختيار طلب صحيح"
    decision = DECISIONS.get(form.get("decision", ""))
    reason = REASONS.get(form.get("reason", ""))
    notes = (form.get("notes") or "").strip()
    matrix = form.get("matrix") == "1"
    if not decision:
        return None, "القرار مطلوب"
    if decision == "لم يتم التنفيذ" and not reason:
        return None, "يجب اختيار الأسباب"
    if reason == "Other" and not notes:
        return None, "يجب كتابة الملاحظات"
    if _needs_matrix(t["ticket_type"]) and not matrix:
        return None, "يجب الاطلاع على مصفوفة الصلاحيات"
    t.update(status="closed", decision=decision, reason=reason if decision != "تم التنفيذ" else None,
             notes=notes or None, matrix_checked=matrix, closed_by=session["user"])
    S.log("portal", f"ticket {tid} ({t['user_id']}, {t['ticket_type']}) -> {decision} / {reason} / {notes}")
    return f"تم حفظ القرار بنجاح للطلب رقم {tid}", None


# ---- test helpers -------------------------------------------------------
@app.route("/__state")
def get_state():
    return jsonify(S.snapshot())


@app.route("/__reset", methods=["POST"])
def do_reset():
    S.reset()
    return jsonify({"ok": True})
