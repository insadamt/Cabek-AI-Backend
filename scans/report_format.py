# ============================================================
# COPIED REPORT FORMATTERS
# ============================================================

# Keep the standalone report independent of the Streamlit page and its import side effects.

def _pdf_money(value, dash_if_zero=False):
    """Format monétaire CABEK : 1 500,00."""
    if value is None:
        return "INCOMPLET"
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    if dash_if_zero and abs(value) < 1e-9:
        return "----"
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", " ")

def _pdf_text(value, default="----"):
    if value is None or str(value).strip() == "":
        return default
    return str(value)

def _pdf_date(value):
    if not value:
        return "----"
    return str(value)[:10]

def _pdf_num_to_words_fr(n):
    """Conversion simple des montants en lettres, suffisante pour le rapport DH."""
    if n is None:
        return "indisponible"
    units = [
        "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept",
        "huit", "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
    ]

    def under_100(x):
        if x < 17:
            return units[x]
        if x < 20:
            return "dix-" + units[x - 10]
        tens = {20: "vingt", 30: "trente", 40: "quarante", 50: "cinquante", 60: "soixante"}
        if x < 70:
            t = (x // 10) * 10
            r = x % 10
            return tens[t] if r == 0 else tens[t] + ("-et-un" if r == 1 else "-" + units[r])
        if x < 80:
            r = x - 60
            return "soixante-dix" if r == 10 else "soixante-" + under_100(r)
        r = x - 80
        return "quatre-vingts" if r == 0 else "quatre-vingt-" + under_100(r)

    def under_1000(x):
        if x < 100:
            return under_100(x)
        h, r = divmod(x, 100)
        base = "cent" if h == 1 else units[h] + " cent"
        if r == 0 and h > 1:
            base += "s"
        return base if r == 0 else base + " " + under_100(r)

    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        return "moins " + _pdf_num_to_words_fr(-n)
    if n < 1000:
        return under_1000(n)
    if n < 1_000_000:
        q, r = divmod(n, 1000)
        base = "mille" if q == 1 else under_1000(q) + " mille"
        return base if r == 0 else base + " " + under_1000(r)
    return str(n)
