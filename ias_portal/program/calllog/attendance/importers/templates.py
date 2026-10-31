"""Downloadable blank templates, generated from the layouts of the two real HR files."""
import io
from calendar import monthrange
from datetime import date

import openpyxl
from openpyxl.styles import Alignment, Font


_AR_DAYS = {0: "الإثنين", 1: "الثلاثاء", 2: "الأربعاء", 3: "الخميس", 4: "الجمعة", 5: "السبت", 6: "الأحد"}
_AR_MONTHS = {1: "يناير", 2: "فبراير", 3: "مارس", 4: "ابريل", 5: "مايو", 6: "يونيو", 7: "يوليو", 8: "اغسطس",
              9: "سبتمبر", 10: "اكتوبر", 11: "نوفمبر", 12: "ديسمبر"}


def attendance_template():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["تقرير احصائي ببيانات موقف الحضور والانصراف للموظف خلال الفترة من : DD-MM-YYYY الى : DD-MM-YYYY"])
    ws.append(["رقم الموظف", "أسم الموظف", "الإدارة / الفرع", "المنطقة / القطاع", "التاريخ", "اليوم", "نوع اليوم", "توقيت الحضور",
               "مصدر أثبات الحضور", "توقيت الأنصراف", "مصدر أثبات الأنصراف", "فرق الساعات", "موقف الاجازات",
               "موقف ( الأذون )", "موقف ( المهمات )"])
    ws.append([112100, "اسم الموظف الكامل", "الإدارة", "القطاع", date(2026, 10, 4), "الأحد", "يوم عمل",
               "08:13:12", "النظام", "16:32:08", "النظام", "08:18", None, None, None])
    for c in ws[2]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A3"
    return _bytes(wb)


def schedule_template(year, month):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "الورديات"
    ws["A1"] = f"شهر {_AR_MONTHS[month]} {year}"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A1:K1")
    ws["A2"], ws["B2"] = "اليوم", "التاريخ"
    ws["C2"] = "الوردية الصباحية\nFrom 7 AM To 3 PM"
    ws["G2"] = "الوردية المسائية\nFrom 2 PM To 10 PM"
    ws["K2"] = " الوردية الليلية\nFrom 10 PM To 7 AM"
    for col, lab in zip("CDEFGHIJK", ["FLEX", "CARD", "OPEN + MFA", "3rd Party"] * 2 + ["All"]):
        ws[f"{col}3"] = lab
    r = 4
    for d in range(1, monthrange(year, month)[1] + 1):
        dd = date(year, month, d)
        ws.cell(r, 1, _AR_DAYS[dd.weekday()])
        ws.cell(r, 2, dd)
        r += 1
    ws.append([])
    ws.append(["Type the employee names under each shift (several names in one cell: separate with a new line or ' - '). "
               "Write ( MFA ) after a name for the MFA role. For on-call staff add a row below the date: "
               "'name (VPN/ Stand By Site - CM) - name (...)'."])
    for row in ws.iter_rows(min_row=2, max_row=3):
        for c in row:
            c.alignment = Alignment(wrap_text=True, horizontal="center")
    return _bytes(wb)


def _bytes(wb):
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
