"""A fake of the portal's «الاستفسار عن الطلبات» page for the SLA history read (real Chromium against a real HTTP server).

``FakeInquiryPortal`` serves INQUIRY_PATH on 127.0.0.1 and a free port, built like the bank's page (structure only):

* the search: «تاريخ الطلب من / الى» take a DATE ONLY (``21-10-2026``; anything else - a time, another order - is
  refused with a message and no results), «مجموعة العمل» (All) and the «بـحـث» button;
* the results grid in the real column order, one row per application of a request, each with its own «التفاصيل»
  ASP.NET post-back (``Select$N``, N counted on the result page shown - like a GridView);
* the portal sends at most ``server_page`` rows a search page («All» = 1000 on the bank's portal); more are behind its
  pager under the grid. ``pager``: "link" = the «التالي» link centred under the grid (``Page$Next``, a post-back);
  "buttons" = like the bank's page: the buttons «عرض التالي» / «عرض السابق» (plain submits, always shown) and the line
  «اجمالي عدد الطلبات : - N» with the total of every result page; "total_only" = that line and no way to the next page;
* ``grid``: how the DataTable on top of a result page behaves -
    "client"     every row of the page through a small DataTables API (window.jQuery.fn.dataTable), ``dt_len`` shown;
    "paged_api"  the API holds only the rows of the DataTable's current page, its «of N records» counts the page; its
                 «Show» list (5 / 10 / 20 / All) and next button (class paginate_button next) change what it holds;
    "dom"        no API: the DOM shows ``dt_len`` rows, «Show» and next re-render them;
  ``grid_lengths`` = the «Show» choices (-1 = All; the bank's All stops at the 1000 rows of a result page);
* «التفاصيل» posts back and the answer shows the box «تفاصيل الطلب» (رقم الطلب, the employee and user numbers) with
  «الحالة التاريخية للطلب»: sorted by user NAME (not time), the date cell «21/10/2026<br>08:54:23»,
  «Showing 1 to 5 of N records» and a pager. ``history``:
    "api"    a DataTables API holds every line;
    "show"   no API; «Show» 5 / 10 / 25 re-renders the lines;
    "pages"  no API; «Show» 5 only: the pager's next (paginate_button next / disabled) is the only way;
    "stuck"  like "pages" but next never moves: the table cannot be read whole.

Everything is made up: names, numbers and request numbers come from the tests.
"""
import html
import http.server
import json
import re
import threading
import time
import urllib.parse
from datetime import datetime

INQUIRY_PATH = "/SecurityCodeRequest/Security_Assign_Maker/RequestDetails_Inquiry.aspx"
GRID_TARGET = "ctl00$ContentPlaceHolder1$GridView_Inquiry"
DETAIL_GRID_ID = "ContentPlaceHolder1_UC_RequestDeatils_Inquiry1_UC_RequestDetails1_GridView_Req"
GRID_HEADERS = ["تاريخ الطلب", "نوع النموذج", "الفرع / الادارة", "اسم الموظف", "رقم الموظف", "رقم الموظف TP",
                "رقم المستخدم", "المسمى الوظيفي", "النظام", "المجموعة", "نوع الطلب", "موظف الاسناد", "مستخدم السرية",
                "مراجع السرية", "ملاحظات", "الحالة"]
GRID_FIELDS = ["date", "form", "branch", "employee", "employee_no", "tp", "user_no", "title", "system", "group",
               "request_type", "assigner", "executor", "checker", "notes", "status"]
HISTORY_HEADERS = ["رقم المستخدم", "اسم المستخدم", "نوع المستخدم", "التاريخ", "الحالة", "ملاحظات"]
DATE_ONLY = re.compile(r"^\d{2}-\d{2}-\d{4}$")


def request(number, day, status, executor="", checker="", history=(), period=None, **grid):
    """One made-up request row: ``day`` = date of the request, ``history`` = [(user_no, name, user_type, datetime,
    status, notes)] in any order (the page sorts them by name). ``period`` = a «نموذج (7)» request's period, shown in
    its details: (from, to) as the two fields «الفترة من» / «الفترة الى», or one text for one field «الفتره من / الى»."""
    row = {"date": day.strftime("%d/%m/%Y"), "form": "أخرى", "branch": "70001", "employee": "موظف اختبار",
           "employee_no": "700201", "tp": "", "user_no": "700201", "title": "وظيفة اختبار", "system": "Flexcube",
           "group": "", "request_type": "اضافة صلاحية", "assigner": executor, "executor": executor, "checker": checker,
           "notes": "", "status": status}
    row.update(grid)
    return {"number": str(number), "grid": row, "history": list(history), "period": period}


def _js(value):
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


# a DataTables look-alike for one table: «Show» list, «Showing a to b of n records», previous / next; with ``api`` it
# answers window.jQuery.fn.dataTable like the real library (``hold`` = "all": every row, "page": the current page only)
_MINI_DT = r"""
window.__miniDT = window.__miniDT || [];
function miniDT(id, rows, opt) {
  const table = document.getElementById(id), wrap = table.closest('.dataTables_wrapper');
  const body = table.tBodies[0], sel = wrap.querySelector('.dataTables_length select');
  const info = wrap.querySelector('.dataTables_info'), nx = wrap.querySelector('.paginate_button.next'),
        pv = wrap.querySelector('.paginate_button.previous');
  const nodes = rows.map(cells => { const tr = document.createElement('tr');
    tr.innerHTML = cells.map(c => '<td>' + c + '</td>').join(''); return tr; });
  let page = 0, len = +sel.value;
  const size = () => (len < 0 ? nodes.length : len);
  const last = () => Math.max(0, Math.ceil(nodes.length / size()) - 1);
  function render() {
    body.replaceChildren(...nodes.slice(page * size(), (page + 1) * size()));
    const a = nodes.length ? page * size() + 1 : 0, b = Math.min(nodes.length, (page + 1) * size());
    info.textContent = 'Showing ' + a + ' to ' + b + ' of ' + nodes.length + ' records';
    nx.className = 'paginate_button next' + (page >= last() ? ' disabled' : '');
    pv.className = 'paginate_button previous' + (page <= 0 ? ' disabled' : '');
  }
  sel.onchange = () => { len = +sel.value; page = 0; render(); };
  nx.onclick = e => { e.preventDefault(); if (opt.stuck || page >= last()) return; page++; render(); };
  pv.onclick = e => { e.preventDefault(); if (page <= 0) return; page--; render(); };
  render();
  if (!opt.api) return;
  const held = () => (opt.hold === 'page' ? nodes.slice(page * size(), (page + 1) * size()) : nodes);
  window.__miniDT.push({table, held, setLen: n => { len = n; page = 0; render(); }, getLen: () => len});
  const find = node => window.__miniDT.find(d => d.table === node);
  function Api(node) { this.d = find(node); }
  Api.prototype.columns = function () { const n = this.d.table.tHead.rows[0].cells.length;
    return {indexes: () => ({toArray: () => [...Array(n).keys()]})}; };
  Api.prototype.column = function (c) { const t = this.d.table; return {header: () => t.tHead.rows[0].cells[c]}; };
  Api.prototype.rows = function () { const h = this.d.held();
    return {indexes: () => ({toArray: () => h.map((_, i) => i)}), nodes: () => ({toArray: () => h.slice()}),
            data: () => ({toArray: () => h.map(tr => [...tr.cells].map(td => td.innerHTML))})}; };
  Api.prototype.cell = function (r, c) { const tr = this.d.held()[r];
    return {node: () => (tr ? tr.cells[c] : null), data: () => (tr ? tr.cells[c].innerHTML : '')}; };
  const dt = function () {};
  dt.tables = () => window.__miniDT.map(d => d.table);
  dt.isDataTable = node => !!find(node);
  dt.Api = function (node) { const a = new Api(node); const d = a.d;
    a.page = {len: n => (n === undefined ? d.getLen() : (d.setLen(n), {draw: () => a}))}; return a; };
  window.jQuery = {fn: {dataTable: dt}};
}
"""


def _dt_wrapper(table_id, head, lengths, selected, title=""):
    opts = "".join(f"<option value='{v}'{' selected' if v == selected else ''}>{'All' if v == -1 else v}</option>"
                   for v in lengths)
    heads = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    return (f"<div class='dataTables_wrapper'>{title}"
            f"<div class='dataTables_length'><label>Show <select name='{table_id}_length'>{opts}</select></label></div>"
            f"<div class='dataTables_filter'><label>Search:<input type='search'></label></div>"
            f"<table id='{table_id}'><thead><tr>{heads}</tr></thead><tbody></tbody></table>"
            f"<div class='dataTables_info'></div><div class='dataTables_paginate'>"
            f"<a class='paginate_button previous' href='#'>‹</a><a class='paginate_button next' href='#'>›</a></div></div>")


class FakeInquiryPortal:
    """``with FakeInquiryPortal(requests) as portal: portal.url + INQUIRY_PATH``. Public knobs can be changed."""

    def __init__(self, requests, grid="client", history="api", server_page=1000, dt_len=20,
                 grid_lengths=(5, 10, 20, -1), pager="link", arrive_on_next=(), ignore_from=False, details="modal",
                 next_after_details=True, details_delay=0.0):
        self.requests = requests
        self.arrive_on_next = list(arrive_on_next)  # requests registered the moment the next result page is asked for
        self.ignore_from = ignore_from          # a search that lists every request up to «الى» (its «من» not taken)
        # "modal": «تفاصيل الطلب» in a box of its own; "in_page": filled into the page under the grid (the bank's), with
        # the status history's own server pager «التالي» (Page$Next) inside it
        self.details = details
        self.next_after_details = next_after_details      # False: «عرض التالي» right after a request's details does nothing
        self._details_shown = False
        self.details_delay = details_delay      # seconds the portal takes to answer one «التفاصيل»
        self.pager = pager
        self.grid = grid
        self.grid_lengths = list(grid_lengths)
        self.history = history
        self.server_page = server_page
        self.dt_len = dt_len
        self.searches = []                      # (from, to) as typed, per search
        self.refused = []                       # dates the search refused (not a plain dd-mm-yyyy)
        self.details_opened = []                # request numbers whose «التفاصيل» was opened
        self.pages_shown = []                   # result page index of every answer with results
        self._server = None
        self.url = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        portal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                portal._handle(self, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                portal._handle(self, {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ the page
    def _handle(self, h, form):
        if urllib.parse.urlsplit(h.path).path != INQUIRY_PATH:
            return self._send(h, 404, "<p>غير موجود</p>")
        state = {"from": form.get("hfFrom", ""), "to": form.get("hfTo", ""), "page": int(form.get("hfPage") or 0)}
        message, selected = "", None
        if "btnSearch" in form:
            typed = (form.get("txtFrom", "").strip(), form.get("txtTo", "").strip())
            self.searches.append(typed)
            if all(DATE_ONLY.match(t) for t in typed):
                state = {"from": typed[0], "to": typed[1], "page": 0}
            else:
                self.refused.append(typed)
                state, message = {"from": "", "to": "", "page": 0}, "تاريخ غير صحيح"
        target, arg = form.get("__EVENTTARGET", ""), form.get("__EVENTARGUMENT", "")
        stuck = not self.next_after_details and self._details_shown and "btnNext" in form
        self._details_shown = False
        if stuck:                                       # the page comes back as it was, the details closed
            rows = self._results(state)
            return self._send(h, 200, self._page(state, rows, max(0, (len(rows) - 1) // self.server_page), None, ""))
        if self.arrive_on_next and ((target == GRID_TARGET and arg == "Page$Next") or "btnNext" in form):
            self.requests = self.arrive_on_next + list(self.requests)    # the newest first: the rows move down
            self.arrive_on_next = []
        rows = self._results(state)
        last = max(0, (len(rows) - 1) // self.server_page)
        if (target == GRID_TARGET and arg == "Page$Next") or "btnNext" in form:
            state["page"] = min(last, state["page"] + 1)
        elif (target == GRID_TARGET and arg == "Page$Prev") or "btnPrev" in form:
            state["page"] = max(0, state["page"] - 1)
        elif target == GRID_TARGET and arg.startswith("Select$"):
            n = state["page"] * self.server_page + int(arg.split("$", 1)[1])
            if 0 <= n < len(rows):
                selected = rows[n]
                self.details_opened.append(selected["number"])
                self._details_shown = True
                if self.details_delay:
                    time.sleep(self.details_delay)
        return self._send(h, 200, self._page(state, rows, last, selected, message))

    def _results(self, state):
        if not state["from"]:
            return []
        lo, hi = (datetime.strptime(state[k], "%d-%m-%Y").date() for k in ("from", "to"))
        if self.ignore_from:
            lo = datetime.min.date()
        return [r for r in self.requests if lo <= datetime.strptime(r["grid"]["date"], "%d/%m/%Y").date() <= hi]

    def _page(self, state, rows, last, selected, message):
        hidden = "".join(f"<input type='hidden' name='{k}' id='{k}' value='{html.escape(str(v))}'>"
                         for k, v in (("__EVENTTARGET", ""), ("__EVENTARGUMENT", ""), ("hfFrom", state["from"]),
                                      ("hfTo", state["to"]), ("hfPage", state["page"])))
        search = ("<fieldset><legend>البحث عن طلبات</legend>"
                  f"<div><label for='txtFrom'>تاريخ الطلب من</label><input id='txtFrom' name='txtFrom' value='{html.escape(state['from'])}'></div>"
                  f"<div><label for='txtTo'>تاريخ الطلب الى</label><input id='txtTo' name='txtTo' value='{html.escape(state['to'])}'></div>"
                  "<div><label for='ddlGroup'>مجموعة العمل</label><select id='ddlGroup' name='ddlGroup'>"
                  "<option>اختر</option><option selected>All</option></select></div>"
                  "<input type='submit' name='btnSearch' value='بـحـث'>"
                  f"<span class='error'>{html.escape(message)}</span></fieldset>")
        body, script = "", ""
        if state["from"]:
            self.pages_shown.append(state["page"])
            shown = rows[state["page"] * self.server_page:(state["page"] + 1) * self.server_page]
            cells = [[html.escape(r["grid"][f]) for f in GRID_FIELDS]
                     + [f"<a href=\"javascript:__doPostBack('{GRID_TARGET}','Select${i}')\">التفاصيل</a>"]
                     for i, r in enumerate(shown)]
            body = ("<h3>نتيجة البحث عن الطلبات</h3>"
                    + _dt_wrapper("GridView_Inquiry", GRID_HEADERS + ["التفاصيل"], self.grid_lengths, self.dt_len))
            if self.pager in ("buttons", "total_only"):   # the bank's page: total line + «عرض السابق» / «عرض التالي»
                body += f"<div class='total'>اجمالي عدد الطلبات : - {len(rows)}</div>"
                if self.pager == "buttons":
                    body += ("<div class='pager-buttons' style='text-align:left'>"
                             "<input type='submit' class='btn' name='btnPrev' value='عرض السابق'> "
                             "<input type='submit' class='btn' name='btnNext' value='عرض التالي'></div>")
            elif last:                                 # the portal's own pager, centred under the grid
                links = []
                if state["page"] > 0:
                    links.append(f"<a href=\"javascript:__doPostBack('{GRID_TARGET}','Page$Prev')\">السابق</a>")
                links.append(f"<span>{state['page'] + 1}</span>")
                if state["page"] < last:
                    links.append(f"<a href=\"javascript:__doPostBack('{GRID_TARGET}','Page$Next')\">التالي</a>")
                body += "<div class='gv-pager' style='text-align:center'>" + " ".join(links) + "</div>"
            api = self.grid in ("client", "paged_api")
            hold = "page" if self.grid == "paged_api" else "all"
            script = f"miniDT('GridView_Inquiry', {_js(cells)}, {{api: {_js(api)}, hold: {_js(hold)}}});"
        if selected is not None:
            body += self._details(selected)
            hist = sorted(selected["history"], key=lambda e: e[1])            # by user NAME, as the portal shows it
            lines = [[html.escape(no), html.escape(name), html.escape(kind), f"{at:%d/%m/%Y}<br>{at:%H:%M:%S}",
                      html.escape(status), html.escape(notes)] for no, name, kind, at, status, notes in hist]
            stuck = self.history == "stuck"
            script += (f"miniDT({_js(DETAIL_GRID_ID)}, {_js(lines)}, {{api: {_js(self.history == 'api')}, "
                       f"hold: 'all', stuck: {_js(stuck)}}});")
        post = ("<script>function __doPostBack(t, a) { const f = document.getElementById('form1');"
                " f.__EVENTTARGET.value = t; f.__EVENTARGUMENT.value = a; f.submit(); }</script>")
        return ("<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><title>الاستفسار عن الطلبات</title>"
                f"</head><body><form id='form1' method='post' action='{INQUIRY_PATH}'>{hidden}{post}"
                f"<h2>الاستفسار عن الطلبات</h2>{search}{body}</form>"
                f"<script>{_MINI_DT}{script}</script></body></html>")

    def _details(self, r):
        g = r["grid"]
        lengths = [5] if self.history in ("pages", "stuck") else [5, 10, 25]
        fields = "".join(f"<div><label>{html.escape(k)}</label><input readonly value='{html.escape(v)}'></div>"
                         for k, v in (("رقم الطلب", r["number"]), ("تاريخ الطلب", g["date"]), ("النظام", g["system"]),
                                      ("رقم الموظف", g["employee_no"]), ("رقم المستخدم", g["user_no"]),
                                      ("الحالة", g["status"])))
        period = r.get("period")
        if isinstance(period, (tuple, list)):
            fields += "".join(f"<div><span>{html.escape(k)} :</span> <input readonly value='{html.escape(v)}'></div>"
                              for k, v in (("الفترة من", period[0]), ("الفترة الى", period[1])))
        elif period:
            fields += f"<div><label>الفتره من / الى</label><span>{html.escape(period)}</span></div>"
        if self.details == "in_page":                  # in the page, with the history grid's own server pager
            return ("<div id='pnlRequest'><div><h4>تفاصيل الطلب</h4>" + fields
                    + _dt_wrapper(DETAIL_GRID_ID, HISTORY_HEADERS, lengths, 5, "<h5>الحالة التاريخية للطلب</h5>")
                    + "<div class='gv-pager'><a href=\"javascript:__doPostBack('ctl00$ContentPlaceHolder1$gvHistory',"
                    "'Page$Next')\">التالي</a></div></div></div>")
        return ("<div class='modal' role='dialog' style='display:block'><div class='modal-content'>"
                f"<h4>تفاصيل الطلب</h4>{fields}"
                + _dt_wrapper(DETAIL_GRID_ID, HISTORY_HEADERS, lengths, 5, "<h5>الحالة التاريخية للطلب</h5>")
                + "<button type='button' class='close-box'>اغلاق</button></div></div>")

    @staticmethod
    def _send(h, code, body):
        data = body.encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
