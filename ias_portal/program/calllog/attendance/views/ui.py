"""View helpers: navigation structure and server-side SVG charts (no inline styles, no chart library)."""
from html import escape

# ---------------------------------------------------------------- navigation
PREFIX = "/attendance"


def build_nav(user, path, args):
    """Primary sections (top bar) and the tabs of the active section (second bar). Which entries exist depends on the role."""
    role = user["role"]
    if path.startswith(PREFIX):
        path = path[len(PREFIX):] or "/"
    kind = args.get("kind", "summary") if path == "/reports" else ""

    def tab(label, icon, href, on):
        return {"label": label, "icon": icon, "href": PREFIX + href, "active": bool(on)}

    # Dashboard: the Day view and Team availability (every role sees them, scoped to its teams)
    dash = [tab("Day view", "layers", "/planning/day", path == "/planning/day"), tab("Team availability", "users", "/team", path == "/team")]
    plan = [tab("Weekly plan", "calendar-check", "/planning/week", path == "/planning/week"),
            tab("Monthly calendar", "calendar", "/schedule", path == "/schedule" or path == "/schedule/edit"),
            tab("Leave", "plane", "/leave", path.startswith("/leave")),
            tab("Compensatory days", "coffee", "/comp", path.startswith("/comp"))]
    imports = []
    if role == "admin":                        # uploading is administrator-only (managers have no import screens)
        imports.append(tab("Monthly schedule", "upload", "/schedule/import", path.startswith("/schedule/import")))
        imports.append(tab("HR attendance", "file", "/hr/import", path.startswith("/hr/import")))
        imports.append(tab("Name mapping", "link", "/schedule/names", path == "/schedule/names"))
        imports.append(tab("HR numbers", "users", "/hr/links", path == "/hr/links"))
    if role in ("admin", "gm"):
        imports.append(tab("Import history", "history", "/imports/history", path in ("/imports/history", "/schedule/history", "/hr/history")))
    # the analysis pages: the overview, the employees, the reports (/employee/<id> is the Person dashboard)
    analytics = [tab("Overview", "bars", "/analytics", path == "/analytics"), tab("Employees", "users", "/employees", path == "/employees"),
                 tab("Reports", "file", "/reports", path == "/reports")]
    admin = [tab("Rules & allowance", "sliders", "/admin/settings", path == "/admin/settings"),
             tab("Scoring rules", "award", "/admin/scoring", path == "/admin/scoring"),
             tab("Staff & teams", "users", "/admin/employees", path == "/admin/employees"),
             tab("Roles & permissions", "shield", "/admin/roles", path == "/admin/roles"),
             tab("Audit log", "history", "/admin/audit", path == "/admin/audit")]
    if role in ("allowance", "employee"):      # an employee: their own schedule (+ the shift-allowance export when assigned)
        return {"sections": [], "tabs": [], "group": ""}
    # the sections themselves are in the portal's side menu (Today, Planning, Analysis, Settings, Logs); here only the tabs of
    # the group the page belongs to
    if role in ("admin", "manager"):
        plan.append(tab("Changes", "pencil", "/modify", path == "/modify"))
    plan.append(tab("Monthly shifts", "calendar-check", "/monthly-shifts", path.startswith("/monthly-shifts")))   # managers approve the month
    settings = admin[:4] + imports if role == "admin" else []
    groups = (("Planning", plan), ("Attendance analysis", analytics), ("Attendance settings", settings))
    for name, tabs in groups:
        if any(t["active"] for t in tabs):
            return {"sections": [], "tabs": tabs, "group": name}
    return {"sections": [], "tabs": [], "group": ""}


# ---------------------------------------------------------------- charts
def donut(segments, size=150, center="", label="", stroke=18):
    """segments: [(label, value, tone_class)] -> ring chart with a legend. Colours come from the tone classes."""
    total = sum(v for _, v, _ in segments) or 1
    r = 42
    circ = 2 * 3.14159265 * r
    parts, offset = [], 0.0
    for lab, v, tone in segments:
        if v <= 0:
            continue
        ln = circ * v / total
        parts.append(f'<circle class="seg {tone}" cx="50" cy="50" r="{r}" stroke-width="{stroke * 100 / size:.1f}" '
                     f'stroke-dasharray="{ln:.2f} {circ - ln:.2f}" stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 50 50)">'
                     f'<title>{escape(lab)}: {v}</title></circle>')
        offset += ln
    svg = (f'<svg class="donut" viewBox="0 0 100 100" width="{size}" height="{size}" role="img" aria-label="{escape(label or "chart")}">'
           f'<circle class="track" cx="50" cy="50" r="{r}" stroke-width="{stroke * 100 / size:.1f}"/>{"".join(parts)}'
           f'<text class="v" x="50" y="52" text-anchor="middle" font-size="17">{escape(str(center))}</text>'
           f'<text class="l" x="50" y="64" text-anchor="middle" font-size="6.5">{escape(label)}</text></svg>')
    legend = "".join(f'<span class="{tone}"><i class="sw"></i>{escape(lab)} <b>{v}</b></span>' for lab, v, tone in segments if v)
    return f'<div class="dviz">{svg}<div class="legend col">{legend}</div></div>'


def strip(segments, label="status of the day"):
    """segments: [(label, count, tone_class, hint)] -> one coloured bar (widths by count) with a legend of plain numbers: no percentage, no ring."""
    total = sum(c for _, c, _, _ in segments)
    if not total:
        return ""
    x, rects = 0.0, []
    for lab, c, tone, _ in segments:
        if c <= 0:
            continue
        w = 100.0 * c / total
        rects.append(f'<rect class="seg {tone}" x="{x:.2f}" width="{w:.2f}" height="12"><title>{escape(lab)}: {c}</title></rect>')
        x += w
    bar = f'<svg class="strip-bar" viewBox="0 0 100 12" preserveAspectRatio="none" role="img" aria-label="{escape(label)}">{"".join(rects)}</svg>'
    items = "".join(f'<li class="{tone}"><i class="sw"></i><b>{c}</b> {escape(lab)}<small>{escape(hint)}</small></li>' for lab, c, tone, hint in segments)
    return f'<div class="strip">{bar}<ul class="strip-key">{items}</ul></div>'


def hbars(items, scale=None):
    """items: [{'label', 'value', 'tone', 'href', 'value2', 'text'}]; value2 (e.g. planned vs available) draws a lighter bar behind."""
    if not items:
        return ""
    top = scale or max((max(i["value"], i.get("value2") or 0) for i in items), default=0) or 1
    rows = []
    for i in items:
        w = 100 * i["value"] / top
        w2 = 100 * (i.get("value2") or 0) / top
        tag = "a" if i.get("href") else "div"
        href = f' href="{escape(i["href"])}"' if i.get("href") else ""
        back = f'<rect class="fl2" width="{w2:.1f}" height="10" rx="3"/>' if i.get("value2") else ""
        rows.append(f'<{tag} class="hbar {i.get("tone", "s0")}"{href}><span class="hl">{i.get("icon", "")}{escape(str(i["label"]))}</span>'
                    f'<svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true"><rect class="tr" width="100" height="10" rx="3"/>{back}'
                    f'<rect class="fl" width="{max(w, 0.8 if i["value"] else 0):.1f}" height="10" rx="3"/></svg>'
                    f'<span class="hv">{escape(str(i.get("text", i["value"])))}</span></{tag}>')
    return f'<div class="hbars">{"".join(rows)}</div>'


def progress(done, total):
    pct = 100 if not total else round(100 * done / total)
    return (f'<div class="progress" role="progressbar" aria-valuenow="{pct}" aria-valuemin="0" aria-valuemax="100">'
            f'<svg viewBox="0 0 100 8" preserveAspectRatio="none"><rect class="tr" width="100" height="8"/>'
            f'<rect id="matchBar" class="fl" width="{pct}" height="8"/></svg></div>')
