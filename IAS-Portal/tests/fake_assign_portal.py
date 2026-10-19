"""A fake of the portal's "assign new requests" pages for the ticketing auto-assign engine's end-to-end tests (real
Chromium driven over CDP against a real HTTP server).

``FakeAssignPortal`` serves, on 127.0.0.1 and a free port, what the stand-alone NBE auto-authorizer bot walks through:

    /App_Security_Login/Login.aspx           login (a hidden decoy input, user + password, a «دخول» button)
    /Portal/Groups.aspx                      the packages grid; the MAKER package comes FIRST (so a bot that clicks by
                                             position enters the wrong role), then the SLA package, then - unless
                                             ``supervisor=False`` - the SUPERVISOR package; its «دخــــــول» link opens
                                             the module in a new window (``open_mode="blank"``) or the same tab
    .../Security_Assign_Supervisor/Home.aspx the module menu (Panel_Menu) with the Request_Assign.aspx link
    .../Security_Assign_Supervisor/Request_Assign.aspx
                                             the assign page, a real ASP.NET-style post-back form:
        * «نوع النموذج» (ddlFormType) posts back on change; the grid only shows after «بحث» (btnSearch)
        * the grid shows ``page_size`` rows until its length <select> is set to All (-1) - and goes back to the first
          page after EVERY post-back, like a re-rendered DataTable
        * «القرار» (ddlExecuteStatus) posts back; value 6 makes «الموظفين» (ddlEmployee) appear, offering the employees
          ``offered[form_type]``; the employee select posts back too
        * ticked boxes are restored after a post-back (as ASP.NET does with posted check-boxes)
        * «تنفيذ» (btnExecute) asks confirm() and assigns exactly the POSTED (ticked) rows to the chosen employee

Everything is fake: names, numbers and the password are made up by the tests; the password is never stored.
"""
import html
import http.server
import threading
import urllib.parse

LOGIN_PATH = "/App_Security_Login/Login.aspx"
GROUPS_PATH = "/Portal/Groups.aspx"
MODULE = "/SecurityCodeRequest/Security_Assign_Supervisor"
HOME_PATH = MODULE + "/Home.aspx"
ASSIGN_PATH = MODULE + "/Request_Assign.aspx"
MAKER_PACKAGE = "مدخل البيانات بوحدة الأكواد السرية"
SLA_PACKAGE = "User Provisioning Portal - موظف اسناد - مستخدم السرية"
SUPERVISOR_PACKAGE = "مشرف البيانات بوحدة الأكواد السرية"
FORM_TYPES = {"1": "نموذج 5", "2": "نموذج 6"}
HEADERS = ["رقم الطلب", "اسم الموظف", "النظام", "الفتره من / الى"]


def sample_requests():
    """{form_type: [request]} - a past, an open-ended, a FUTURE-dated and an Arabic-digit request, over two forms."""
    return {
        "1": [
            {"id": "R101", "employee": "موظف اختبار 1", "system": "Flexcube", "period": "-"},
            {"id": "R102", "employee": "موظف اختبار 2", "system": "Flexcube", "period": "01/10/2026 - 31/12/2026"},
            {"id": "R103", "employee": "موظف اختبار 3", "system": "DWP", "period": "01/01/2099 - 31/12/2099"},
            {"id": "R104", "employee": "موظف اختبار 4", "system": "DWP", "period": "-"},
            {"id": "R105", "employee": "موظف اختبار 5", "system": "Flexcube", "period": "٠١/١٠/٢٠٢٦ - ٣١/١٢/٢٠٢٦"},
        ],
        "2": [
            {"id": "R201", "employee": "موظف اختبار 6", "system": "GL", "period": "-"},
        ],
    }


class FakeAssignPortal:
    """``with FakeAssignPortal() as portal: portal.url``; afterwards ``portal.assigned`` = {request id: employee}."""

    def __init__(self, requests=None, username="2001", password="fake-pw-123", supervisor=True, open_mode="blank",
                 page_size=3, offered=None):
        self.requests = requests if requests is not None else sample_requests()
        self.username = username
        self.password = password
        self.supervisor = supervisor
        self.open_mode = open_mode
        self.page_size = page_size
        self.offered = offered if offered is not None else {"1": {"111": "Alice Maker", "222": "Bob Maker"},
                                                            "2": {"111": "Alice Maker"}}
        self.assigned = {}                      # request id -> employee value it was assigned to
        self.executes = []                      # (form type, employee, [request ids]) per Execute
        self.logins = []                        # True / False per login attempt, never the password
        self._server = None
        self._thread = None
        self.url = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        portal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                portal._handle(self, "GET")

            def do_POST(self):
                portal._handle(self, "POST")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ routing
    def _handle(self, h, method):
        path = urllib.parse.urlsplit(h.path).path
        form = {}
        if method == "POST":
            body = h.rfile.read(int(h.headers.get("Content-Length") or 0)).decode("utf-8")
            form = urllib.parse.parse_qs(body, keep_blank_values=True)
        one = lambda k: (form.get(k) or [""])[0]                      # noqa: E731
        if path.lower() == LOGIN_PATH.lower():
            return self._login(h, method, one)
        if "sid=ok" not in h.headers.get("Cookie", ""):
            return self._redirect(h, LOGIN_PATH)
        if path == GROUPS_PATH:
            return self._send(h, self._groups_page())
        if path == HOME_PATH:
            return self._send(h, self._page("القائمة", "<div id='Panel_Menu'><a href='Request_Approve.aspx'>اعتماد</a> "
                                                       "<a href='Request_Assign.aspx'>اسناد الطلبات الجديدة</a></div>"))
        if path == ASSIGN_PATH:
            return self._send(h, self._assign(method, form, one))
        return self._send(h, self._page("غير موجود", ""), 404)

    @staticmethod
    def _send(h, body, code=200, headers=()):
        data = body.encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    @staticmethod
    def _redirect(h, location, headers=()):
        h.send_response(302)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()

    @staticmethod
    def _page(title, body):
        return (f"<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><title>{html.escape(title)}"
                f"</title></head><body>{body}</body></html>")

    # ------------------------------------------------------------------ pages
    def _login(self, h, method, one):
        message = ""
        if method == "POST":
            ok = one("txtName") == self.username and one("txtPassword") == self.password
            self.logins.append(ok)
            if ok:
                return self._redirect(h, GROUPS_PATH, [("Set-Cookie", "sid=ok; Path=/; HttpOnly")])
            message = "<span id='ContentPlaceHolder1_lblMessage'>بيانات الدخول غير صحيحة</span>"
        body = (f"{message}<form method='post' action='{LOGIN_PATH}'>"
                "<input type='text' name='decoy' style='display:none'>"
                "<input type='text' id='ContentPlaceHolder1_txtName' name='txtName'>"
                "<input type='password' id='ContentPlaceHolder1_txtPassword' name='txtPassword'>"
                "<input type='submit' id='ContentPlaceHolder1_btnAdd' name='btnAdd' value='دخول'></form>")
        return self._send(h, self._page("تسجيل الدخول", body))

    def _groups_page(self):
        target = "target='_blank'" if self.open_mode == "blank" else ""

        def row(i, name, href):
            return (f"<tr><td>{html.escape(name)}</td><td><a id='ContentPlaceHolder1_GridView_Application_btnSubmit_{i}'"
                    f" href='{href}' {target}>دخــــــول</a></td></tr>")
        rows = row(0, MAKER_PACKAGE, "/SecurityCodeRequest/Security_Assign_Maker/Home.aspx")
        rows += row(1, SLA_PACKAGE, "/SecurityCodeRequest/Security_Assign_Maker/Request_ReAssign.aspx")
        if self.supervisor:
            rows += row(2, SUPERVISOR_PACKAGE, HOME_PATH)
        return self._page("التطبيقات", f"<table id='ContentPlaceHolder1_GridView_Application'>"
                                       f"<tr><th>التطبيق</th><th></th></tr>{rows}</table>")

    def _assign(self, method, form, one):
        form_type = one("ddlFormType") if method == "POST" else ""
        searched = method == "POST" and ("btnSearch" in form or (one("searched") == "1"
                                                                 and form_type == one("lastForm")))
        decision = one("ddlExecuteStatus") if searched else ""
        employee = one("ddlEmployee") if decision == "6" else ""
        posted = {v for k, vals in form.items() if k.startswith("ch_Select_") for v in vals}
        if "btnExecute" in form and decision == "6" and employee and posted:
            rows = self.requests.get(form_type, [])
            done = [r["id"] for r in rows if r["id"] in posted]
            self.requests[form_type] = [r for r in rows if r["id"] not in posted]
            for rid in done:
                self.assigned[rid] = employee
            self.executes.append((form_type, employee, done))
            decision, employee, posted = "", "", set()             # the page comes back clean after Execute
        return self._page("اسناد الطلبات الجديدة", self._assign_body(form_type, searched, decision, employee, posted))

    def _assign_body(self, form_type, searched, decision, employee, posted):
        opts = "<option value='0'>--اختر--</option>" + "".join(
            f"<option value='{v}'{' selected' if v == form_type else ''}>{html.escape(t)}</option>"
            for v, t in FORM_TYPES.items())
        out = [f"<form method='post' action='Request_Assign.aspx' id='form1'>",
               f"<input type='hidden' name='searched' value='{1 if searched else 0}'>",
               f"<input type='hidden' name='lastForm' value='{html.escape(form_type)}'>",
               f"<select id='ContentPlaceHolder1_ddlFormType' name='ddlFormType' onchange='this.form.submit()'>{opts}"
               "</select>",
               "<input type='submit' id='ContentPlaceHolder1_btnSearch' name='btnSearch' value='بحث'>"]
        rows = self.requests.get(form_type, []) if searched and form_type in FORM_TYPES else []
        if searched and form_type in FORM_TYPES:
            head = "".join(f"<th>{html.escape(h)}</th>" for h in HEADERS)
            body = "".join(
                "<tr><td><input type='checkbox' id='ContentPlaceHolder1_GridView_RequestDetails_ch_Select_"
                f"{i}' name='ch_Select_{i}' value='{r['id']}'{' checked' if r['id'] in posted else ''}></td>"
                + "".join(f"<td>{html.escape(r[k])}</td>" for k in ("id", "employee", "system", "period")) + "</tr>"
                for i, r in enumerate(rows))
            out.append("<table id='ContentPlaceHolder1_GridView_RequestDetails'><thead><tr><th><input type='checkbox' "
                       "id='ContentPlaceHolder1_GridView_RequestDetails_CheckBox_All'></th>" + head + "</tr></thead>"
                       f"<tbody>{body}</tbody></table>"
                       "<select name='ContentPlaceHolder1_GridView_RequestDetails_length'>"
                       f"<option value='{int(self.page_size)}'>{int(self.page_size)}</option>"
                       "<option value='-1'>الكل</option></select>"
                       "<script>(function(){ const tb = document.querySelector("
                       "'#ContentPlaceHolder1_GridView_RequestDetails tbody'), ALL = [...tb.rows];"
                       "const sel = document.querySelector(\"select[name='ContentPlaceHolder1_GridView_RequestDetails_length']\");"
                       "const draw = () => { const n = +sel.value; tb.replaceChildren(...(n < 0 ? ALL : ALL.slice(0, n))); };"
                       "sel.addEventListener('change', draw); draw(); })();</script>")
        if rows:
            dec = ("<option value=''>--</option>"
                   + "".join(f"<option value='{v}'{' selected' if v == decision else ''}>{t}</option>"
                             for v, t in (("6", "اسناد لمستخدم السرية"), ("9", "رفض"))))
            out.append(f"<select id='ContentPlaceHolder1_ddlExecuteStatus' name='ddlExecuteStatus' "
                       f"onchange='this.form.submit()'>{dec}</select>")
            if decision == "6":
                emps = "<option value=''>--</option>" + "".join(
                    f"<option value='{v}'{' selected' if v == employee else ''}>{v} - {html.escape(n)} - ( 1 )</option>"
                    for v, n in self.offered.get(form_type, {}).items())
                out.append(f"<select id='ContentPlaceHolder1_ddlEmployee' name='ddlEmployee' "
                           f"onchange='this.form.submit()'>{emps}</select>")
            out.append("<input type='submit' id='ContentPlaceHolder1_btnExecute' name='btnExecute' value='تنفيذ' "
                       "onclick=\"return confirm('هل انت متأكد؟')\">")
        out.append("</form>")
        return "".join(out)
