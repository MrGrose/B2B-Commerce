import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

from b2b_commerce.invoices.service import InvoiceView

INVOICE_FONT_PATH = (
    Path(__file__).resolve().parent.parent / "static" / "fonts" / "DejaVuSans.ttf"
)

# Возвращает путь к шрифту для PDF.
def _invoice_font_path() -> Path:
    if INVOICE_FONT_PATH.is_file():
        return INVOICE_FONT_PATH
    for candidate in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Шрифт DejaVuSans.ttf не найден")


# Делает безопасное имя файла из номера счёта.
def invoice_download_filename(invoice_number: str, extension: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", invoice_number).strip("._") or "invoice"
    return f"{safe}.{extension}"


# Форматирует дату счёта для печатной формы.
def _format_invoice_date(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d.%m.%Y")


# Форматирует сумму для печатной формы.
def _money(value: Decimal) -> str:
    formatted = f"{value.quantize(Decimal('0.01')):,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", " ")


_ONES = (
    "",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_TENS = (
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
)
_HUNDREDS = (
    "",
    "сто",
    "двести",
    "триста",
    "четыреста",
    "пятьсот",
    "шестьсот",
    "семьсот",
    "восемьсот",
    "девятьсот",
)


# Выбирает форму слова по числу.
def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


# Переводит число 0..999 в русские слова.
def _triplet_ru(n: int, feminine: bool) -> str:
    hundreds, rem = divmod(n, 100)
    parts: list[str] = []
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    if rem >= 20:
        tens, ones = divmod(rem, 10)
        parts.append(_TENS[tens])
        rem = ones
    if rem:
        if feminine and rem == 1:
            parts.append("одна")
        elif feminine and rem == 2:
            parts.append("две")
        else:
            parts.append(_ONES[rem])
    return " ".join(parts)


# Переводит целое число в русские слова.
def _int_to_ru(n: int) -> str:
    if n == 0:
        return "ноль"
    groups = (
        (10**9, False, "миллиард", "миллиарда", "миллиардов"),
        (10**6, False, "миллион", "миллиона", "миллионов"),
        (10**3, True, "тысяча", "тысячи", "тысяч"),
        (1, False, "", "", ""),
    )
    parts: list[str] = []
    rest = n
    for scale, feminine, one, few, many in groups:
        qty, rest = divmod(rest, scale)
        if qty == 0:
            continue
        words = _triplet_ru(qty, feminine)
        if one:
            parts.append(f"{words} {_plural_ru(qty, one, few, many)}")
        else:
            parts.append(words)
    return " ".join(parts)


# Сумма прописью: рубли и копейки.
def amount_in_words(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    rubles = int(quantized)
    kopecks = int(quantized * 100) % 100
    return (
        f"{_int_to_ru(rubles).capitalize()} "
        f"{_plural_ru(rubles, 'рубль', 'рубля', 'рублей')} "
        f"{kopecks:02d} {_plural_ru(kopecks, 'копейка', 'копейки', 'копеек')}"
    )


# Общие поля печатной формы PDF и XLSX из снимка счёта.
def invoice_export_values(invoice: InvoiceView) -> dict[str, str]:
    seller = invoice.seller
    buyer = invoice.buyer
    return {
        "number": invoice.number,
        "date": _format_invoice_date(invoice.created_at),
        "title": (
            f"Счет на оплату № {invoice.number} "
            f"от {_format_invoice_date(invoice.created_at)}"
        ),
        "seller_name": seller.legal_name or seller.name or "—",
        "seller_inn": seller.inn or "—",
        "seller_kpp": seller.kpp or "—",
        "seller_address": seller.legal_address or "—",
        "seller_bank": seller.bank_name or "—",
        "seller_bik": seller.bik or "—",
        "seller_corr_account": seller.corr_account or "—",
        "seller_bank_account": seller.bank_account or "—",
        "buyer_name": buyer.legal_name or buyer.name or "—",
        "buyer_inn": buyer.inn or "—",
        "buyer_kpp": buyer.kpp or "—",
        "buyer_address": buyer.legal_address or "—",
        "delivery_address": buyer.recipient_address or buyer.legal_address or "—",
        "total": _money(invoice.total),
        "amount_words": amount_in_words(invoice.total),
        "notes": invoice.notes or "",
        "items_count": str(len(invoice.items)),
    }


# Итоговые строки печатной формы счёта.
def invoice_document_totals(invoice: InvoiceView) -> list[tuple[str, str]]:
    values = invoice_export_values(invoice)
    return [
        ("Итого", values["total"]),
        ("Без НДС", "—"),
        ("Всего к оплате", values["total"]),
    ]


# Рисует верхнюю таблицу банковских реквизитов в стиле 1С.
def _draw_bank_header(pdf: FPDF, values: dict[str, str]) -> None:
    x = pdf.l_margin
    y = pdf.get_y()
    w = pdf.epw
    left_w = w * 0.58
    mid_w = w * 0.12
    right_w = w - left_w - mid_w
    top_h = 16
    pdf.set_line_width(0.3)
    pdf.rect(x, y, left_w, top_h)
    pdf.rect(x + left_w, y, mid_w, top_h / 2)
    pdf.rect(x + left_w + mid_w, y, right_w, top_h / 2)
    pdf.rect(x + left_w, y + top_h / 2, mid_w, top_h / 2)
    pdf.rect(x + left_w + mid_w, y + top_h / 2, right_w, top_h / 2)

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + 1.5, y + 0.4)
    pdf.cell(left_w - 3, 4, "Банк получателя")
    pdf.set_font("DejaVu", size=9)
    pdf.set_xy(x + 1.5, y + 4.5)
    pdf.multi_cell(left_w - 3, 5, values["seller_bank"])

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + left_w + 0.5, y + 0.5)
    pdf.cell(mid_w - 1, 7, "БИК")
    pdf.set_font("DejaVu", size=9)
    pdf.set_xy(x + left_w + mid_w + 0.5, y + 0.5)
    pdf.cell(right_w - 1, 7, values["seller_bik"])

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + left_w + 0.5, y + top_h / 2 + 0.5)
    pdf.cell(mid_w - 1, 7, "№ к/с")
    pdf.set_font("DejaVu", size=8)
    pdf.set_xy(x + left_w + mid_w + 0.5, y + top_h / 2 + 0.5)
    pdf.cell(right_w - 1, 7, values["seller_corr_account"])

    y2 = y + top_h
    inn_w = w * 0.22
    kpp_w = w * 0.22
    name_w = w * 0.28
    acc_w = w - inn_w - kpp_w - name_w
    bot_h = 16
    pdf.rect(x, y2, inn_w, bot_h)
    pdf.rect(x + inn_w, y2, kpp_w, bot_h)
    pdf.rect(x + inn_w + kpp_w, y2, name_w, bot_h)
    pdf.rect(x + inn_w + kpp_w + name_w, y2, acc_w, bot_h)

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + 1.5, y2 + 0.4)
    pdf.cell(inn_w - 3, 4, "ИНН")
    pdf.set_font("DejaVu", size=9)
    pdf.set_xy(x + 1.5, y2 + 5)
    pdf.cell(inn_w - 3, 8, values["seller_inn"])

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + inn_w + 1.5, y2 + 0.4)
    pdf.cell(kpp_w - 3, 4, "КПП")
    pdf.set_font("DejaVu", size=9)
    pdf.set_xy(x + inn_w + 1.5, y2 + 5)
    pdf.cell(kpp_w - 3, 8, values["seller_kpp"])

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + inn_w + kpp_w + 1.5, y2 + 0.4)
    pdf.cell(name_w - 3, 4, "Получатель")
    pdf.set_font("DejaVu", size=8)
    pdf.set_xy(x + inn_w + kpp_w + 1.5, y2 + 4.5)
    pdf.multi_cell(name_w - 3, 4.5, values["seller_name"])

    pdf.set_font("DejaVu", size=7)
    pdf.set_xy(x + inn_w + kpp_w + name_w + 1.5, y2 + 0.4)
    pdf.cell(acc_w - 3, 4, "№ р/с")
    pdf.set_font("DejaVu", size=8)
    pdf.set_xy(x + inn_w + kpp_w + name_w + 1.5, y2 + 5)
    pdf.multi_cell(acc_w - 3, 5, values["seller_bank_account"])
    pdf.set_y(y2 + bot_h + 4)


# Рисует строку стороны с подписью слева.
def _draw_party_row(pdf: FPDF, title: str, body: str) -> None:
    label_w = 40
    x = pdf.l_margin
    y = pdf.get_y()
    pdf.set_font("DejaVu", size=8)
    pdf.set_xy(x, y)
    pdf.multi_cell(label_w, 5, title)
    after_label = pdf.get_y()
    pdf.set_xy(x + label_w, y)
    pdf.set_font("DejaVu", size=9)
    pdf.multi_cell(pdf.epw - label_w, 5, body)
    pdf.set_y(max(pdf.get_y(), after_label) + 0.5)
    line_y = pdf.get_y()
    pdf.line(x, line_y, x + pdf.epw, line_y)
    pdf.ln(2)


# Генерирует PDF счёта на оплату.
def render_invoice_pdf(invoice: InvoiceView) -> bytes:
    values = invoice_export_values(invoice)
    font_path = _invoice_font_path()
    pdf = FPDF()
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.add_font("DejaVu", fname=str(font_path))
    _draw_bank_header(pdf, values)

    pdf.set_font("DejaVu", size=13)
    pdf.cell(pdf.epw, 8, values["title"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_line_width(0.4)
    title_y = pdf.get_y()
    pdf.line(pdf.l_margin, title_y, pdf.l_margin + pdf.epw, title_y)
    pdf.ln(4)

    _draw_party_row(pdf, "Поставщик", f"{values['seller_name']}, {values['seller_address']}")
    _draw_party_row(pdf, "Грузоотправитель", f"{values['seller_name']}, {values['seller_address']}")
    _draw_party_row(
        pdf,
        "Покупатель",
        (
            f"{values['buyer_name']}, ИНН {values['buyer_inn']}, "
            f"КПП {values['buyer_kpp']}, {values['buyer_address']}"
        ),
    )
    _draw_party_row(pdf, "Грузополучатель", values["delivery_address"])

    pdf.set_font("DejaVu", size=8)
    col_widths = (10, 88, 18, 14, 26, 26)
    with pdf.table(
        width=pdf.epw,
        col_widths=col_widths,
        text_align=("CENTER", "LEFT", "CENTER", "CENTER", "RIGHT", "RIGHT"),
        line_height=5,
        first_row_as_headings=True,
        headings_style=FontFace(fill_color=(235, 235, 235), emphasis=""),
    ) as table:
        row = table.row()
        for header in ("№", "Товары", "Кол-во", "Ед.", "Цена", "Сумма"):
            row.cell(header)
        for index, item in enumerate(invoice.items, start=1):
            row = table.row()
            row.cell(str(index))
            row.cell(item.product_name)
            row.cell(str(item.quantity))
            row.cell("шт.")
            row.cell(_money(item.unit_price))
            row.cell(_money(item.line_total))

    pdf.ln(2)
    totals_w = 42
    label_w = 42
    label_x = pdf.l_margin + pdf.epw - totals_w - label_w
    pdf.set_font("DejaVu", size=9)
    for label, amount in invoice_document_totals(invoice):
        pdf.set_x(label_x)
        pdf.cell(label_w, 6, label)
        pdf.cell(totals_w, 6, amount, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("DejaVu", size=9)
    pdf.multi_cell(
        pdf.epw,
        5,
        (
            f"Всего наименований {values['items_count']}, "
            f"на сумму {values['total']}"
        ),
    )
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 5, values["amount_words"])
    if values["notes"]:
        pdf.ln(2)
        pdf.set_x(pdf.l_margin)
        pdf.set_font("DejaVu", size=9)
        pdf.multi_cell(pdf.epw, 5, f"Условия поставки: {values['notes']}")
    pdf.ln(8)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("DejaVu", size=9)
    sig_w = pdf.epw * 0.45
    pdf.cell(38, 6, "Подпись")
    pdf.cell(sig_w, 6, "________________")
    pdf.cell(pdf.epw - 38 - sig_w, 6, values["seller_name"], new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())



# Генерирует PDF счёта без блокировки event loop.
async def render_invoice_pdf_async(invoice: InvoiceView) -> bytes:
    return await asyncio.to_thread(render_invoice_pdf, invoice)
