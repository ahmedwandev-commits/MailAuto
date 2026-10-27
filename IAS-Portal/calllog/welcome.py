"""The welcome shown once after every sign-in, before the first page: a positive, motivating message for the person's
role (employee, manager, administrator), in Arabic and English. Which message a person sees depends on who they are
and on the day, so colleagues signing in together see different ones and a person sees a new one each day."""
import hashlib

MESSAGES = {
    "user": [
        ("كل طلب تُنجزه اليوم يصنع فرقًا حقيقيًا لزميل في فرع ينتظرك.",
         "Every request you complete today makes a real difference to a colleague in a branch who is waiting for you."),
        ("دقّتك هي أمان البنك. شكرًا لأنك تعمل بإتقان كل يوم.",
         "Your accuracy keeps the bank safe. Thank you for working with care every day."),
        ("ابدأ يومك بابتسامة، فالخدمة الجيدة تبدأ من روح جيدة.",
         "Start your day with a smile: good service starts with a good spirit."),
        ("خطوة صغيرة كل ساعة تصنع يومًا عظيمًا. أنت قادر.",
         "A small step every hour makes a great day. You can do it."),
        ("سرعتك في الرد توفّر على العميل الكثير. استمر بنفس الروح.",
         "Your quick replies save our customers a lot. Keep that spirit."),
        ("أنت جزء مهم من فريق يحمي هويات البنك وحسابات عملائه.",
         "You are an important part of the team that protects the bank's identities and its customers' accounts."),
        ("الإتقان عادة، وأنت تبنيها كل يوم. يوم موفق!",
         "Excellence is a habit, and you build it every day. Have a great day!"),
        ("شكرًا لصبرك ومساعدتك لزملائك؛ هذا ما يجعل فريقنا مميزًا.",
         "Thank you for your patience and for helping your colleagues: that is what makes our team special."),
    ],
    "manager": [
        ("فريقك ينجح لأنك تؤمن به. يوم قيادي موفق!",
         "Your team succeeds because you believe in it. Have a great day leading it!"),
        ("كلمة تقدير منك اليوم قد تكون أجمل ما يسمعه أحد أعضاء فريقك.",
         "A word of appreciation from you today may be the best thing someone on your team hears."),
        ("القائد الجيد يرى الأرقام، والقائد العظيم يرى الناس خلفها.",
         "A good leader sees the numbers; a great leader sees the people behind them."),
        ("كل طلب يُغلق في وقته هو ثمرة توجيهك. استمر!",
         "Every request closed on time is the fruit of your guidance. Keep going!"),
        ("وضوح أولوياتك يمنح فريقك الثقة والسرعة.",
         "Your clear priorities give your team confidence and speed."),
        ("النجاح الحقيقي أن يكبر فريقك معك. يوم مثمر!",
         "Real success is when your team grows with you. Have a productive day!"),
        ("دعمك لفريقك في أوقات الزحام هو ما يصنع الفرق.",
         "Your support for the team in the busy hours is what makes the difference."),
        ("اليوم فرصة جديدة لتحويل التحديات إلى إنجازات مع فريقك.",
         "Today is a new chance to turn challenges into achievements with your team."),
    ],
    "admin": [
        ("خلف كل صفحة تعمل بسلاسة جهدك أنت. شكرًا لك.",
         "Behind every page that runs smoothly is your work. Thank you."),
        ("أنت من يُبقي الأدوات تعمل ليتفرغ الجميع لخدمة العملاء.",
         "You keep the tools running so everyone else can focus on the customers."),
        ("نظام مستقر يعني فريقًا مطمئنًا. يوم هادئ ومثمر!",
         "A stable system means a confident team. Have a calm and productive day!"),
        ("كل تحسين صغير تضيفه يوفّر ساعات على زملائك.",
         "Every small improvement you make saves your colleagues hours."),
        ("الأمان والدقة يبدآن من إعداداتك. شكرًا لحرصك.",
         "Security and accuracy start with your settings. Thank you for your care."),
        ("أنت تربط الفرق ببعضها وتجعل الأرقام تحكي القصة كاملة.",
         "You connect the teams and make the numbers tell the whole story."),
        ("التميّز في التفاصيل، وأنت تتقنها. يوم موفق!",
         "Excellence is in the details, and you master them. Have a great day!"),
        ("شكرًا لأنك دائمًا الحل عندما يحتاجك الجميع.",
         "Thank you for always being the answer when everyone needs you."),
    ],
}
ROLE_LABELS = {"user": ("موظف", "Employee"), "manager": ("مدير", "Manager"), "admin": ("مسؤول النظام", "Administrator")}
SECONDS = 7                     # how long the welcome stays before the portal opens by itself


def greeting(hour):
    """(Arabic, English) greeting for the hour of the day."""
    if hour < 12:
        return "صباح الخير", "Good morning"
    if hour < 17:
        return "نهارك سعيد", "Good afternoon"
    return "مساء الخير", "Good evening"


def first_name(user):
    name = " ".join(str((user or {}).get("name") or "").split())
    return name.split(" ")[0] if name else ""


def message_for(user, now):
    """The welcome of ``user`` at ``now`` (a datetime): {greeting_ar, greeting_en, name, ar, en, role_ar, role_en}.
    The message is picked from the person's role by their user ID and the day."""
    role = (user or {}).get("role") if (user or {}).get("role") in MESSAGES else "user"
    pool = MESSAGES[role]
    seed = f"{str((user or {}).get('user_id') or '').lower()}|{now.date().isoformat()}".encode("utf-8")
    ar, en = pool[int(hashlib.sha256(seed).hexdigest(), 16) % len(pool)]
    g_ar, g_en = greeting(now.hour)
    r_ar, r_en = ROLE_LABELS[role]
    return {"greeting_ar": g_ar, "greeting_en": g_en, "name": first_name(user), "ar": ar, "en": en,
            "role_ar": r_ar, "role_en": r_en}
