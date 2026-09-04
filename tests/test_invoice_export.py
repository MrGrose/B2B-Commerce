from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from b2b_commerce.catalog.service import normalize_image_content_type, prepare_image_upload
from b2b_commerce.config import Settings
from b2b_commerce.invoices.pdf import (
    amount_in_words,
    invoice_download_filename,
    invoice_export_values,
    render_invoice_pdf,
)
from b2b_commerce.invoices.service import (
    InvoiceBuyerSnapshot,
    InvoiceItemView,
    InvoiceSellerSnapshot,
    InvoiceView,
    build_payment_instructions,
)


# Собирает тестовый счёт со снимками сторон.
def _sample_invoice(product_name: str = "Тестовый товар") -> InvoiceView:
    return InvoiceView(
        id=uuid4(),
        company_id=uuid4(),
        number="42",
        status="awaiting_payment",
        subtotal=Decimal("200.00"),
        total=Decimal("200.00"),
        notes="Тестовый счёт",
        payment_instructions="Получатель: ИП Продавец\nИНН 7701234567 / КПП 770101001\n",
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
        expires_at=None,
        paid_at=None,
        shipped_at=None,
        canceled_at=None,
        company_name='Demo Sports Company',
        items=[
            InvoiceItemView(
                id=uuid4(),
                product_id=uuid4(),
                product_name=product_name,
                quantity=2,
                unit_price=Decimal("100.00"),
                line_total=Decimal("200.00"),
            )
        ],
        seller=InvoiceSellerSnapshot(
            name="Seller IE",
            legal_name="ИП Продавец",
            inn="7701234567",
            kpp="770101001",
            legal_address="Москва, ул. Продавца, 1",
            bank_name="Тест Банк",
            bik="044525225",
            bank_account="40702810100000000001",
            corr_account="30101810400000000225",
        ),
        buyer=InvoiceBuyerSnapshot(
            name="Demo Sports Company",
            legal_name='ООО "Demo Sports Company"',
            inn="7712345678",
            kpp="771201001",
            legal_address="Москва, арена",
            contact_phone="+79991112233",
            contact_email="arena@example.com",
            recipient_address="Доставка, 5",
        ),
    )


def test_build_payment_instructions_contains_supplier_fields():
    settings = Settings()
    text = build_payment_instructions(settings)
    assert settings.supplier_inn in text
    assert settings.supplier_bank_account in text


def test_invoice_download_filename_sanitizes_number():
    assert invoice_download_filename("INV/2026#1", "pdf") == "INV_2026_1.pdf"


def test_amount_in_words():
    assert amount_in_words(Decimal("200.00")) == "Двести рублей 00 копеек"
    assert amount_in_words(Decimal("1.01")) == "Один рубль 01 копейка"
    assert amount_in_words(Decimal("22.23")) == "Двадцать два рубля 23 копейки"
    assert amount_in_words(Decimal("1000.00")) == "Одна тысяча рублей 00 копеек"


def test_render_invoice_pdf():
    invoice = _sample_invoice()
    values = invoice_export_values(invoice)
    pdf = render_invoice_pdf(invoice)
    assert pdf.startswith(b"%PDF")
    assert b"/Count 1" in pdf
    assert values["number"] == "42"
    assert values["seller_inn"] == "7701234567"
    assert values["amount_words"] == "Двести рублей 00 копеек"


def test_pdf_wraps_long_product_name_on_one_page():
    invoice = _sample_invoice("Ракетка падел профессиональная " * 8)
    pdf = render_invoice_pdf(invoice)
    assert pdf.startswith(b"%PDF")
    assert b"/Count 1" in pdf


def test_normalize_image_content_type_accepts_jpg_alias():
    assert normalize_image_content_type("image/jpg") == "image/jpeg"


def test_prepare_image_upload_normalizes_jpeg():
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), color="red").save(buffer, format="JPEG")
    prepared, content_type, ext = prepare_image_upload(buffer.getvalue(), "image/jpg")
    assert content_type == "image/jpeg"
    assert ext == "jpg"
    assert prepared.startswith(b"\xff\xd8")
