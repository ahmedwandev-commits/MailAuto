"""HTML pages: login, logout, password change, the two dashboards."""
from flask import Blueprint, Response, abort, g, jsonify, redirect, render_template, request, session, url_for

from . import auth, avatars, i18n, welcome as welcome_messages
from .security import password_problem, verify_password, hash_password
from .storage import iso_now, norm_uid

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    if not g.user:
        return redirect(url_for("pages.login"))
    # Hub pages are written in calllog/ops/views.py; plain paths so this works whichever blueprint serves them
    return redirect("/hub" if g.user["role"] in auth.OPS_ROLES else "/my")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if g.user:
            return redirect(url_for("pages.index"))
        return render_template("login.html", error=None, user_id="")

    uid = norm_uid(request.form.get("user_id"))[:64]       # a User ID is at most 32 characters; keeps logs / counters small
    password = request.form.get("password", "")
    ip = request.remote_addr
    limits = auth.throttles()
    wait = max(limits["user"].retry_after((uid, ip)), limits["ip"].retry_after(ip))
    if wait:
        auth.audit("login_blocked", target=uid, actor={"user_id": uid})
        minutes = -(-wait // 60)
        return render_template("login.html", user_id=uid, error=(
            f"Too many failed attempts. Try again in {minutes} minute(s).")), 429

    user = auth.store().get_user(uid)
    # Always hash-check (even for unknown IDs) so timing does not reveal which IDs exist.
    password_ok = verify_password(user["password_hash"] if user else None, password)
    if not (user and password_ok):
        limits["user"].fail((uid, ip))
        limits["ip"].fail(ip)
        auth.audit("login_failed", target=uid, actor={"user_id": uid})
        return render_template("login.html", user_id=uid,
                               error="Wrong User ID or password."), 401
    if not user.get("active", True):
        auth.audit("login_failed", target=uid, details={"reason": "disabled"}, actor=user)
        return render_template("login.html", user_id=uid, error=(
            "This account is disabled. Please contact your administrator.")), 403

    limits["user"].clear((uid, ip))
    session.clear()  # new session id on every login
    session.permanent = True
    session["uid"] = uid
    session["pwv"] = auth.password_mark(user)
    auth.csrf_token()
    auth.store().update_user(uid, {"last_login": iso_now(auth.now())})
    auth.audit("login", actor=user)
    session["welcome"] = True       # the welcome message is shown once, before the first page
    return redirect(url_for("pages.welcome"))


@bp.get("/welcome")
@auth.login_required
def welcome():
    """A positive message for the person's role, once after each sign-in (after the password change a new account
    must make first), then the portal opens by itself. Any other visit goes straight to the person's start page."""
    if not session.pop("welcome", False):
        return redirect(url_for("pages.index"))
    return render_template("welcome.html", message=welcome_messages.message_for(g.user, auth.now()),
                           next_url=url_for("pages.index"), seconds=welcome_messages.SECONDS)


@bp.post("/logout")
def logout():
    if g.user:
        auth.audit("logout")
    session.clear()
    return redirect(url_for("pages.login"))


@bp.post("/account/lang")
def set_lang():
    """The عربي / English switch (top bar and sign-in page): kept on the account, and in a cookie for the sign-in page."""
    lang = request.form.get("lang", "")
    nxt = request.form.get("next", "")
    if (not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in nxt)):     # "/\t/evil" is "//evil" to a browser
        nxt = url_for("pages.index")
    resp = redirect(nxt)
    if lang in i18n.LANGS:
        resp.set_cookie(i18n.COOKIE, lang, max_age=i18n.COOKIE_AGE, samesite="Lax", httponly=True)
        if g.user and g.user.get("lang") != lang:
            auth.store().update_user(g.user["user_id"], {"lang": lang})
    return resp


@bp.route("/account/picture", methods=["GET", "POST"])
@auth.login_required
def picture():
    """My picture: upload a photo, choose a built-in avatar, or go back to the initials."""
    if request.method == "GET":
        return render_template("picture.html", presets=avatars.PRESETS, error=None,
                               saved=request.args.get("saved") == "1")
    uid, action, store = g.user["user_id"], request.form.get("action", ""), auth.store()
    if action == "photo":
        upload = request.files.get("photo")
        data = avatars.from_data_url(request.form.get("photo_data")) or (upload.read() if upload else b"")
        error = avatars.problem(data)
        if error:
            return render_template("picture.html", presets=avatars.PRESETS, error=error, saved=False), 400
        store.update_user(uid, {"avatar": avatars.save_photo(store, uid, data)})
        auth.audit("picture_changed", details={"kind": "photo"})
    elif action == "preset" and request.form.get("preset") in avatars.PRESETS:
        avatars.remove_photo(store, uid)
        store.update_user(uid, {"avatar": {"kind": "preset", "id": request.form["preset"]}})
        auth.audit("picture_changed", details={"kind": "avatar", "id": request.form["preset"]})
    elif action == "remove":
        avatars.remove_photo(store, uid)
        store.update_user(uid, {"avatar": None})
        auth.audit("picture_changed", details={"kind": "initials"})
    else:
        return render_template("picture.html", presets=avatars.PRESETS, error="Choose a picture or an avatar first.",
                               saved=False), 400
    return redirect(url_for("pages.picture", saved=1))


@bp.get("/account/picture/<uid>")
@auth.login_required
def picture_file(uid):
    """A person's photo (any signed-in user sees the pictures of the others, as on any staff directory)."""
    store = auth.store()
    found = avatars.photo_file(store, store.get_user(uid))
    if not found:
        abort(404)
    try:                     # read at once (a photo is small): the file is never held open while a browser downloads it
        data = found[0].read_bytes()
    except OSError:
        abort(404)
    return Response(data, mimetype=found[1], headers={"Content-Disposition": "inline"})


@bp.post("/account/picture/<uid>/remove")
@auth.admin_required
def picture_remove(uid):
    """An administrator takes a person's photo down (Admin › Users); they are back to their initials."""
    store = auth.store()
    user = store.get_user(uid)
    if not user:
        return jsonify(error="No such user."), 404
    avatars.remove_photo(store, user["user_id"])
    store.update_user(user["user_id"], {"avatar": None})
    auth.audit("picture_removed", target=user["user_id"])
    return jsonify(ok=True)


@bp.route("/account/password", methods=["GET", "POST"])
@auth.login_required
def password():
    forced = bool(g.user.get("must_change_password"))
    if request.method == "GET":
        return render_template("password.html", error=None, forced=forced)

    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    error = None
    # The current password is guessed against the same counters as the sign-in (per account + address), so a session left
    # open on a shared PC cannot be used to try passwords at full speed.
    key, ip, limits = (g.user["user_id"], request.remote_addr), request.remote_addr, auth.throttles()
    wait = limits["user"].retry_after(key)
    if wait:
        auth.audit("password_change_blocked")
        return render_template("password.html", forced=forced, error=(
            f"Too many failed attempts. Try again in {-(-wait // 60)} minute(s).")), 429
    if not verify_password(g.user["password_hash"], current):
        limits["user"].fail(key)
        limits["ip"].fail(ip)
        error = "Your current password is not correct."
    elif new != request.form.get("confirm_password", ""):
        error = "The new passwords do not match."
    elif new == current:
        error = "Choose a password different from the current one."
    else:
        error = password_problem(new)
    if error:
        return render_template("password.html", error=error, forced=forced), 400

    limits["user"].clear(key)
    new_hash = hash_password(new)
    auth.store().update_user(g.user["user_id"], {
        "password_hash": new_hash, "must_change_password": False,
        "password_changed_at": iso_now(auth.now())})
    session["pwv"] = auth.password_mark({"password_hash": new_hash})      # this session stays, every other one ends
    auth.audit("password_changed")
    return redirect(url_for("pages.welcome" if session.get("welcome") else "pages.index"))


@bp.get("/dashboard")
@auth.login_required
def dashboard():
    if g.user["role"] == "admin":
        return redirect(url_for("pages.admin"))
    if g.user["role"] == "manager":      # managers do not log calls: their home is the dashboard
        return redirect("/hub")
    return render_template("user.html")


@bp.get("/admin")
@auth.admin_required
def admin():
    return render_template("admin.html")
