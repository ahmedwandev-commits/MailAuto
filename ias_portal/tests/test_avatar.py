"""My picture: a photo (checked by its first bytes), a built-in avatar, or the initials; an administrator can take a photo
down; every signed-in user sees the pictures."""
import base64
import io

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _form(b, **data):
    return b.client.post("/account/picture", data={"csrf_token": b.csrf, **data}, content_type="multipart/form-data")


def test_photo_avatar_and_initials(app, make_employee):
    emp = make_employee("101", name="Khaled Mohamed Fekry")
    page = emp.get("/account/picture").get_data(as_text=True)
    assert "My picture" in page and 'class="avatar avatar-xl"' in page and ">KM<" in page        # initials at first

    r = _form(emp, action="photo", photo=(io.BytesIO(PNG), "me.png"))
    assert r.status_code == 302 and r.headers["Location"].endswith("/account/picture?saved=1")
    user = app.extensions["store"].get_user("101")
    assert user["avatar"]["kind"] == "photo" and user["avatar"]["ext"] == "png"
    src = f"/account/picture/101?v={user['avatar']['v']}"
    assert src in emp.get("/dashboard").get_data(as_text=True)                                  # the top bar shows it
    pic = emp.get(src)
    assert pic.status_code == 200 and pic.data == PNG and pic.mimetype == "image/png"
    assert "max-age" in pic.headers["Cache-Control"] and pic.headers["X-Content-Type-Options"] == "nosniff"
    files = list((app.extensions["store"].root / "avatars").iterdir())
    assert len(files) == 1 and "101" not in files[0].name                                       # never named after the ID

    # the page's square JPEG (data: text) replaces the PNG
    r = _form(emp, action="photo", photo_data="data:image/jpeg;base64," + base64.b64encode(JPEG).decode())
    assert r.status_code == 302 and app.extensions["store"].get_user("101")["avatar"]["ext"] == "jpg"
    assert [f.suffix for f in (app.extensions["store"].root / "avatars").iterdir()] == [".jpg"]

    r = _form(emp, action="preset", preset="a05")
    assert r.status_code == 302 and app.extensions["store"].get_user("101")["avatar"] == {"kind": "preset", "id": "a05"}
    assert not list((app.extensions["store"].root / "avatars").iterdir())                       # the photo is gone
    assert "/static/avatars/a05.svg" in emp.get("/account/picture").get_data(as_text=True)

    r = _form(emp, action="remove")
    assert r.status_code == 302 and app.extensions["store"].get_user("101")["avatar"] is None


def test_only_pictures_are_kept(app, make_employee):
    emp = make_employee("102")
    for bad in (b"<svg onload=alert(1)>", b"GIF89a....", b"", b"MZ\x90\x00"):
        r = _form(emp, action="photo", photo=(io.BytesIO(bad), "x.png"))
        assert r.status_code == 400
    assert _form(emp, action="photo", photo_data="data:text/html;base64,PGI+").status_code == 400
    assert _form(emp, action="preset", preset="../../etc").status_code == 400
    assert _form(emp, action="photo", photo=(io.BytesIO(b"\xff\xd8\xff" + b"0" * (1600 * 1024)), "big.jpg")).status_code in (400, 413)
    assert "avatar" not in app.extensions["store"].get_user("102") or not app.extensions["store"].get_user("102")["avatar"]
    assert emp.client.post("/account/picture", data={"action": "remove"}).status_code == 400   # CSRF checked


def test_others_see_it_and_an_admin_takes_it_down(app, admin, make_employee):
    a = make_employee("201", name="Mona Ali")
    b = make_employee("202")
    _form(a, action="photo", photo=(io.BytesIO(JPEG), "me.jpg"))
    v = app.extensions["store"].get_user("201")["avatar"]["v"]
    assert b.get(f"/account/picture/201?v={v}").status_code == 200                               # a colleague sees it
    assert app.test_client().get("/account/picture/201").status_code in (302, 401)               # not without signing in
    assert b.get("/account/picture/202").status_code == 404                                      # no photo: nothing
    assert b.post("/account/picture/201/remove", {}).status_code in (302, 403)                   # only an administrator
    assert app.extensions["store"].get_user("201")["avatar"]["kind"] == "photo"

    row = next(u for u in admin.get("/api/admin/users").get_json()["users"] if u["user_id"] == "201")
    assert row["picture_kind"] == "photo" and row["picture"].startswith("/account/picture/201") and row["initials"] == "MA"
    assert admin.post("/account/picture/201/remove", {}).get_json() == {"ok": True}
    assert app.extensions["store"].get_user("201")["avatar"] is None
    assert b.get(f"/account/picture/201?v={v}").status_code == 404
