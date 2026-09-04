from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import (
    AuthContext,
    current_auth,
    require_admin,
    require_admin_api,
    require_approved_company,
)
from b2b_commerce.cart.redirects import query_message
from b2b_commerce.cart.service import get_cart_quantities
from b2b_commerce.catalog.import_service import ImportService
from b2b_commerce.catalog.service import (
    STOREFRONT_PAGE_SIZE,
    ProductInput,
    add_product_image,
    add_product_images,
    count_products_admin,
    count_products_in_brand,
    count_products_in_category,
    count_products_storefront,
    create_brand,
    create_category,
    create_product,
    delete_brand,
    delete_category,
    get_brand,
    get_category,
    get_product_detail,
    list_admin_model_year_stats,
    list_brands,
    list_categories,
    list_products_admin,
    list_products_storefront,
    list_storefront_brand_stats,
    list_storefront_category_stats,
    list_storefront_model_year_stats,
    normalize_admin_sort,
    soft_delete_product,
    update_brand,
    update_category,
    update_product,
)
from b2b_commerce.config import Settings, get_settings
from b2b_commerce.db import get_session
from b2b_commerce.enums import ProductStatus
from b2b_commerce.http import admin_settings_url, catalog_url, templates
from b2b_commerce.infra.security import is_allowed_media_key
from b2b_commerce.infra.storage import ObjectStorage
from b2b_commerce.inventory.service import (
    correct_inventory,
    get_availability,
    get_default_warehouse,
    get_inventory_row,
    list_active_reservations_for_product,
    list_admin_product_stock,
    list_availability,
    reserved_quantity,
)

html = APIRouter()
api = APIRouter()


class CreateProductBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brand_id: UUID | None = None
    category_id: UUID | None = None
    description: str | None = None
    cost_price: Decimal | None = Field(default=None, ge=0)
    sale_price: Decimal = Field(ge=0)
    model_year: int | None = None
    status: str = ProductStatus.INACTIVE


class UpdateProductBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brand_id: UUID | None = None
    category_id: UUID | None = None
    description: str | None = None
    cost_price: Decimal | None = Field(default=None, ge=0)
    sale_price: Decimal = Field(ge=0)
    model_year: int | None = None
    status: str


class InventoryCorrectBody(BaseModel):
    quantity: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


# Зависимость object storage.
def get_storage(settings: Settings = Depends(get_settings)) -> ObjectStorage:
    return ObjectStorage(settings)


# Сериализует товар для JSON.
def _product_json(product, availability: int | None = None, include_cost: bool = False) -> dict:
    images = [
        {
            "id": str(image.id),
            "storage_key": image.storage_key,
            "url": f"/media/{image.storage_key}",
            "sort_order": image.sort_order,
        }
        for image in product.images
    ]
    payload = {
        "id": str(product.id),
        "name": product.name,
        "brand_id": str(product.brand_id) if product.brand_id else None,
        "category_id": str(product.category_id) if product.category_id else None,
        "description": product.description,
        "sale_price": str(product.sale_price),
        "margin_percent": (
            str(product.margin_percent) if product.margin_percent is not None else None
        ),
        "status": product.status,
        "images": images,
        "availability": availability,
    }
    if include_cost:
        payload["cost_price"] = str(product.cost_price) if product.cost_price is not None else None
    return payload


# Парсит целое из формы.
def _optional_int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError("Некорректный год модели") from exc


# Парсит год модели из query string.
def _optional_model_year(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


# Парсит UUID из формы.
def _optional_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    return UUID(value)


# Парсит decimal из формы.
def _parse_optional_decimal(value: str, field_name: str) -> Decimal | None:
    if not value or not value.strip():
        return None
    return _parse_decimal(value, field_name)


# Парсит decimal из строки.
def _parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value.strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValueError(f"Некорректное значение: {field_name}") from None
    if parsed < 0:
        raise ValueError(f"{field_name} не может быть отрицательным")
    return parsed


# Парсит UUID категории из query string.
def _parse_catalog_category_id(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


# Витрина каталога.
@html.get("/catalog")
async def catalog_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    q: str = Query(default=""),
    category_id: str = Query(default=""),
    brand_id: str = Query(default=""),
    model_year: str = Query(default=""),
    sort: str = Query(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    search = q.strip() or None
    filter_category_id = _parse_catalog_category_id(category_id)
    filter_brand_id = _parse_catalog_category_id(brand_id)
    filter_model_year = _optional_model_year(model_year)
    total = await count_products_storefront(
        db,
        category_id=filter_category_id,
        brand_id=filter_brand_id,
        model_year=filter_model_year,
        search=search,
    )
    total_pages = max(1, (total + STOREFRONT_PAGE_SIZE - 1) // STOREFRONT_PAGE_SIZE)
    current_page = min(page, total_pages)
    offset = (current_page - 1) * STOREFRONT_PAGE_SIZE
    catalog_q = q.strip()
    catalog_category_id = str(filter_category_id) if filter_category_id else ""
    catalog_brand_id = brand_id.strip()
    catalog_model_year = model_year.strip()
    catalog_sort = sort.strip()
    products = await list_products_storefront(
        db,
        offset=offset,
        category_id=filter_category_id,
        brand_id=filter_brand_id,
        model_year=filter_model_year,
        search=search,
        sort=catalog_sort or None,
    )
    availability_by_product = await list_availability(
        db, [product.id for product in products]
    )
    rows = [
        {
            "product": product,
            "availability": availability_by_product.get(product.id, 0),
        }
        for product in products
    ]
    cart_quantities = await get_cart_quantities(db, auth.company_id)
    cart_items_count = sum(cart_quantities.values())
    catalog_total_all = await count_products_storefront(db)
    category_stats = await list_storefront_category_stats(db)
    brand_stats = await list_storefront_brand_stats(db)
    model_year_stats = await list_storefront_model_year_stats(db)
    return templates.TemplateResponse(
        request,
        "catalog/list.html",
        {
            "products": rows,
            "cart_quantities": cart_quantities,
            "cart_items_count": cart_items_count,
            "page": current_page,
            "total_pages": total_pages,
            "catalog_q": catalog_q,
            "catalog_category_id": catalog_category_id,
            "catalog_brand_id": catalog_brand_id,
            "catalog_model_year": catalog_model_year,
            "catalog_sort": catalog_sort,
            "catalog_total_all": catalog_total_all,
            "category_stats": category_stats,
            "brand_stats": brand_stats,
            "model_year_stats": model_year_stats,
            "catalog_path": catalog_url(
                catalog_q,
                catalog_category_id,
                brand_id=catalog_brand_id,
                model_year=catalog_model_year,
                sort=catalog_sort,
                page=current_page if current_page > 1 else None,
            ),
            "error": query_message(request.query_params.get("cart_error")),
        },
    )


# Карточка товара для клиента.
@html.get("/catalog/products/{product_id}")
async def product_card(
    request: Request,
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    product = await get_product_detail(db, product_id)
    if product is None or product.status != ProductStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="Товар не найден")
    cart_quantities = await get_cart_quantities(db, auth.company_id)
    return templates.TemplateResponse(
        request,
        "catalog/detail.html",
        {
            "product": product,
            "availability": await get_availability(db, product_id),
            "cart_quantity": cart_quantities.get(product_id, 0),
            "error": query_message(request.query_params.get("cart_error")),
        },
    )


# Нормализует фильтр статуса для списка товаров.
def _status_filter(value: str | None) -> str | None:
    if not value:
        return None
    allowed = {ProductStatus.ACTIVE, ProductStatus.INACTIVE}
    if value not in allowed:
        return None
    return value



# Форма импорта прайса из Excel.
@html.get("/admin/products/import")
async def admin_products_import_form(
    request: Request,
    _auth: AuthContext = Depends(require_admin),
):
    return templates.TemplateResponse(
        request,
        "products/import.html",
        {
            "preview": None,
            "applied": None,
            "confirm_token": None,
            "error": None,
            "success": None,
        },
    )


# Предпросмотр или применение импорта прайса.
@html.post("/admin/products/import")
async def admin_products_import_submit(
    request: Request,
    action: str = Form(default="preview"),
    file: UploadFile | None = File(default=None),
    confirm_token: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
    storage: ObjectStorage = Depends(get_storage),
):
    context = {
        "preview": None,
        "applied": None,
        "confirm_token": None,
        "error": None,
        "success": None,
    }
    service = ImportService()

    try:
        if action == "confirm":
            report = await service.apply_staged(
                auth.subject_id,
                confirm_token,
                db,
                storage=storage,
            )
            context["applied"] = report
            context["success"] = (
                f"Импорт завершён: создано {report.created}, обновлено {report.updated}, "
                f"без изменений {report.skipped}, ошибок {report.errors}"
            )
        else:
            if file is None or not file.filename:
                raise ValueError("Выберите файл .xlsx")
            if not file.filename.lower().endswith(".xlsx"):
                raise ValueError("Поддерживается только формат .xlsx")
            data = await file.read()
            preview = await service.preview_upload(
                data,
                db,
                auth.subject_id,
                storage=storage,
            )
            context["preview"] = preview.report
            context["confirm_token"] = preview.confirm_token
    except ValueError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request,
            "products/import.html",
            context,
            status_code=400,
        )

    return templates.TemplateResponse(request, "products/import.html", context)




# Справочник категорий каталога.
@html.get("/admin/categories")
async def admin_categories_page(
    _request: Request,
    _auth: AuthContext = Depends(require_admin),
):
    return RedirectResponse(admin_settings_url("catalog"), status_code=303)


# Создаёт категорию каталога.
@html.post("/admin/categories")
async def admin_categories_create(
    _request: Request,
    name: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        await create_category(db, name, auth.subject_id)
    except ValueError as exc:
        return RedirectResponse(
            admin_settings_url("catalog", open_section="categories", error=str(exc)),
            status_code=303,
        )
    return RedirectResponse(
        admin_settings_url("catalog", open_section="categories"),
        status_code=303,
    )


# Форма правки категории.
@html.get("/admin/categories/{category_id}/edit")
async def admin_category_edit_form(
    request: Request,
    category_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    category = await get_category(db, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    product_count = await count_products_in_category(db, category_id)
    return templates.TemplateResponse(
        request,
        "categories/edit.html",
        {"category": category, "product_count": product_count, "error": None},
    )


# Сохраняет правки категории.
@html.post("/admin/categories/{category_id}/edit")
async def admin_category_edit_submit(
    request: Request,
    category_id: UUID,
    name: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        category = await update_category(db, category_id, name, auth.subject_id)
    except ValueError as exc:
        category = await get_category(db, category_id)
        if category is None:
            raise HTTPException(status_code=404, detail="Категория не найдена") from exc
        product_count = await count_products_in_category(db, category_id)
        return templates.TemplateResponse(
            request,
            "categories/edit.html",
            {
                "category": category,
                "product_count": product_count,
                "error": str(exc),
            },
            status_code=400,
        )
    if category is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    return RedirectResponse(
        admin_settings_url("catalog", open_section="categories"),
        status_code=303,
    )


# Удаляет категорию без товаров.
@html.post("/admin/categories/{category_id}/delete")
async def admin_category_delete(
    request: Request,
    category_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        deleted = await delete_category(db, category_id, auth.subject_id)
    except ValueError as exc:
        return RedirectResponse(
            admin_settings_url("catalog", open_section="categories", error=str(exc)),
            status_code=303,
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    return RedirectResponse(
        admin_settings_url("catalog", open_section="categories"),
        status_code=303,
    )



# Справочник брендов каталога.
@html.get("/admin/brands")
async def admin_brands_page(
    _request: Request,
    _auth: AuthContext = Depends(require_admin),
):
    return RedirectResponse(admin_settings_url("catalog"), status_code=303)


# Создаёт бренд каталога.
@html.post("/admin/brands")
async def admin_brands_create(
    _request: Request,
    name: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        await create_brand(db, name, auth.subject_id)
    except ValueError as exc:
        return RedirectResponse(
            admin_settings_url("catalog", open_section="brands", error=str(exc)),
            status_code=303,
        )
    return RedirectResponse(
        admin_settings_url("catalog", open_section="brands"),
        status_code=303,
    )


# Форма правки бренда.
@html.get("/admin/brands/{brand_id}/edit")
async def admin_brand_edit_form(
    request: Request,
    brand_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    brand = await get_brand(db, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="Бренд не найден")
    product_count = await count_products_in_brand(db, brand_id)
    return templates.TemplateResponse(
        request,
        "brands/edit.html",
        {"brand": brand, "product_count": product_count, "error": None},
    )


# Сохраняет правки бренда.
@html.post("/admin/brands/{brand_id}/edit")
async def admin_brand_edit_submit(
    request: Request,
    brand_id: UUID,
    name: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        brand = await update_brand(db, brand_id, name, auth.subject_id)
    except ValueError as exc:
        brand = await get_brand(db, brand_id)
        if brand is None:
            raise HTTPException(status_code=404, detail="Бренд не найден") from exc
        product_count = await count_products_in_brand(db, brand_id)
        return templates.TemplateResponse(
            request,
            "brands/edit.html",
            {
                "brand": brand,
                "product_count": product_count,
                "error": str(exc),
            },
            status_code=400,
        )
    if brand is None:
        raise HTTPException(status_code=404, detail="Бренд не найден")
    return RedirectResponse(
        admin_settings_url("catalog", open_section="brands"),
        status_code=303,
    )


# Удаляет бренд без товаров.
@html.post("/admin/brands/{brand_id}/delete")
async def admin_brand_delete(
    _request: Request,
    brand_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        deleted = await delete_brand(db, brand_id, auth.subject_id)
    except ValueError as exc:
        return RedirectResponse(
            admin_settings_url("catalog", open_section="brands", error=str(exc)),
            status_code=303,
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Бренд не найден")
    return RedirectResponse(
        admin_settings_url("catalog", open_section="brands"),
        status_code=303,
    )

# Список товаров в админке.
@html.get("/admin/products")
async def admin_products(
    request: Request,
    q: str = Query(default=""),
    brand_id: str = Query(default=""),
    category_id: str = Query(default=""),
    model_year: str = Query(default=""),
    status: str = Query(default=""),
    sort: str = Query(default="date_desc"),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    status_filter = _status_filter(status)
    parsed_brand_id = _optional_uuid(brand_id)
    parsed_category_id = _optional_uuid(category_id)
    parsed_model_year = _optional_model_year(model_year)
    normalized_sort = normalize_admin_sort(sort)
    list_kwargs = {
        "q": q or None,
        "brand_id": parsed_brand_id,
        "category_id": parsed_category_id,
        "model_year": parsed_model_year,
        "status": status_filter,
    }
    total = await count_products_admin(db, **list_kwargs)
    total_pages = max(1, (total + STOREFRONT_PAGE_SIZE - 1) // STOREFRONT_PAGE_SIZE)
    current_page = min(page, total_pages)
    offset = (current_page - 1) * STOREFRONT_PAGE_SIZE
    products = await list_products_admin(
        db,
        **list_kwargs,
        sort=normalized_sort,
        offset=offset,
        limit=STOREFRONT_PAGE_SIZE,
    )
    stock_rows = await list_admin_product_stock(db, [product.id for product in products])
    product_entries = [
        {"product": product, "stock": stock_rows[product.id]}
        for product in products
    ]
    return templates.TemplateResponse(
        request,
        "products/list.html",
        {
            "products": products,
            "product_entries": product_entries,
            "brands": await list_brands(db),
            "categories": await list_categories(db),
            "model_year_stats": await list_admin_model_year_stats(db),
            "page": current_page,
            "total_pages": total_pages,
            "filters": {
                "q": q,
                "brand_id": brand_id,
                "category_id": category_id,
                "model_year": model_year,
                "status": status_filter or "",
                "sort": normalized_sort,
            },
        },
    )


# Форма создания товара.
@html.get("/admin/products/new")
async def admin_product_new_form(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    return templates.TemplateResponse(
        request,
        "products/new.html",
        {
            "error": None,
            "form": {},
            "categories": await list_categories(db),
            "brands": await list_brands(db),
        },
    )


# Создаёт товар из HTML-формы.
@html.post("/admin/products")
async def admin_product_create_submit(
    request: Request,
    name: str = Form(),
    brand_id: str = Form(default=""),
    category_id: str = Form(default=""),
    description: str = Form(default=""),
    cost_price: str = Form(),
    sale_price: str = Form(),
    model_year: str = Form(default=""),
    status: str = Form(default=ProductStatus.INACTIVE),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    form = {
        "name": name,
        "brand_id": brand_id,
        "category_id": category_id,
        "description": description,
        "cost_price": cost_price,
        "sale_price": sale_price,
        "model_year": model_year,
        "status": status,
    }
    try:
        data = ProductInput(
            name=name,
            brand_id=_optional_uuid(brand_id),
            category_id=_optional_uuid(category_id),
            description=description or None,
            model_year=_optional_int(model_year),
            cost_price=_parse_optional_decimal(cost_price, "Себестоимость"),
            sale_price=_parse_decimal(sale_price, "Цена продажи"),
            status=status,
        )
        product = await create_product(db, data, auth.subject_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "products/new.html",
            {
                "error": str(exc),
                "form": form,
                "categories": await list_categories(db),
                "brands": await list_brands(db),
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "products/detail.html",
        await _admin_product_context(db, product.id),
        status_code=201,
    )


# Карточка товара в админке.
@html.get("/admin/products/{product_id}")
async def admin_product_detail(
    request: Request,
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    context = await _admin_product_context(db, product_id)
    if context is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return templates.TemplateResponse(request, "products/detail.html", context)


# HTML-фрагмент: активные резервы товара по счетам.
@html.get("/admin/products/{product_id}/reservations")
async def admin_product_reservations(
    request: Request,
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    product = await get_product_detail(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    reservations = await list_active_reservations_for_product(db, product_id)
    return templates.TemplateResponse(
        request,
        "products/reservations_fragment.html",
        {
            "product": product,
            "reservations": reservations,
        },
    )


# Обновляет товар из HTML-формы.
@html.post("/admin/products/{product_id}")
async def admin_product_update_submit(
    request: Request,
    product_id: UUID,
    name: str = Form(),
    brand_id: str = Form(default=""),
    category_id: str = Form(default=""),
    description: str = Form(default=""),
    cost_price: str = Form(),
    sale_price: str = Form(),
    model_year: str = Form(default=""),
    status: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        data = ProductInput(
            name=name,
            brand_id=_optional_uuid(brand_id),
            category_id=_optional_uuid(category_id),
            description=description or None,
            model_year=_optional_int(model_year),
            cost_price=_parse_optional_decimal(cost_price, "Себестоимость"),
            sale_price=_parse_decimal(sale_price, "Цена продажи"),
            status=status,
        )
        product = await update_product(db, product_id, data, auth.subject_id)
    except ValueError as exc:
        context = await _admin_product_context(db, product_id)
        if context is None:
            raise HTTPException(status_code=404, detail="Товар не найден") from exc
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request,
            "products/detail.html",
            context,
            status_code=400,
        )
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    context = await _admin_product_context(db, product_id)
    context["success"] = "Товар обновлён"
    return templates.TemplateResponse(request, "products/detail.html", context)


# Загружает фото товара.
@html.post("/admin/products/{product_id}/images")
async def admin_product_image_submit(
    request: Request,
    product_id: UUID,
    images: list[UploadFile] = File(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
    storage: ObjectStorage = Depends(get_storage),
):
    context = await _admin_product_context(db, product_id)
    if context is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    uploads: list[tuple[bytes, str]] = []
    for image in images:
        uploads.append(
            (
                await image.read(),
                image.content_type or "application/octet-stream",
            )
        )
    try:
        saved = await add_product_images(
            db,
            product_id,
            uploads,
            storage,
            auth.subject_id,
        )
    except ValueError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request,
            "products/detail.html",
            context,
            status_code=400,
        )
    context = await _admin_product_context(db, product_id)
    count = len(saved)
    context["success"] = "Фото добавлено" if count == 1 else f"Добавлено фото: {count}"
    return templates.TemplateResponse(request, "products/detail.html", context)


# Мягкое удаление товара из HTML-формы.
@html.post("/admin/products/{product_id}/delete")
async def admin_product_delete_submit(
    request: Request,
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    context = await _admin_product_context(db, product_id)
    if context is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    try:
        product = await soft_delete_product(db, product_id, auth.subject_id)
    except ValueError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request,
            "products/detail.html",
            context,
            status_code=400,
        )
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return RedirectResponse("/admin/products", status_code=303)


# Корректирует остаток товара.
@html.post("/admin/products/{product_id}/inventory/correct")
async def admin_product_inventory_correct_submit(
    request: Request,
    product_id: UUID,
    quantity: int = Form(),
    reason: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    context = await _admin_product_context(db, product_id)
    if context is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    try:
        await correct_inventory(db, product_id, quantity, reason, auth.subject_id)
    except ValueError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request,
            "products/detail.html",
            context,
            status_code=400,
        )
    context = await _admin_product_context(db, product_id)
    context["success"] = "Остаток обновлён"
    return templates.TemplateResponse(request, "products/detail.html", context)


# Прокси для фото из object storage.
@html.get("/media/{storage_key:path}")
async def media_proxy(
    storage_key: str,
    _auth: AuthContext = Depends(current_auth),
    storage: ObjectStorage = Depends(get_storage),
):
    if not is_allowed_media_key(storage_key):
        raise HTTPException(status_code=404, detail="Файл не найден")
    try:
        data, content_type = await storage.get_object(storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Файл не найден") from None
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=604800"},
    )


# Контекст карточки товара в админке.
async def _admin_product_context(db: AsyncSession, product_id: UUID) -> dict | None:
    product = await get_product_detail(db, product_id)
    if product is None:
        return None
    warehouse = await get_default_warehouse(db)
    inventory = await get_inventory_row(db, product_id, warehouse.id)
    return {
        "product": product,
        "categories": await list_categories(db),
        "brands": await list_brands(db),
        "warehouse": warehouse,
        "on_hand": inventory.quantity_on_hand if inventory else 0,
        "reserved": await reserved_quantity(db, product_id),
        "availability": await get_availability(db, product_id),
        "error": None,
        "success": None,
    }


# JSON: витрина.
@api.get("/catalog/products")
async def api_catalog(
    q: str = Query(default=""),
    category_id: str = Query(default=""),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_approved_company),
):
    search = q.strip() or None
    filter_category_id = _parse_catalog_category_id(category_id)
    products = await list_products_storefront(
        db, category_id=filter_category_id, search=search
    )
    availability_by_product = await list_availability(
        db, [product.id for product in products]
    )
    return [
        _product_json(
            product,
            availability=availability_by_product.get(product.id, 0),
        )
        for product in products
    ]


# JSON: карточка на витрине.
@api.get("/catalog/products/{product_id}")
async def api_catalog_product(
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_approved_company),
):
    product = await get_product_detail(db, product_id)
    if product is None or product.status != ProductStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return _product_json(product, availability=await get_availability(db, product_id))


# JSON: список товаров админа.
@api.get("/admin/products")
async def api_admin_products(
    q: str = Query(default=""),
    brand_id: str = Query(default=""),
    category_id: str = Query(default=""),
    model_year: str = Query(default=""),
    status: str = Query(default=""),
    sort: str = Query(default="date_desc"),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    normalized_sort = normalize_admin_sort(sort)
    products = await list_products_admin(
        db,
        q=q or None,
        brand_id=_optional_uuid(brand_id),
        category_id=_optional_uuid(category_id),
        model_year=_optional_model_year(model_year),
        status=_status_filter(status),
        sort=normalized_sort,
    )
    availability_by_product = await list_availability(
        db, [product.id for product in products]
    )
    return [
        _product_json(
            product,
            availability=availability_by_product.get(product.id, 0),
            include_cost=True,
        )
        for product in products
    ]


# JSON: карточка товара админа.
@api.get("/admin/products/{product_id}")
async def api_admin_product(
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    product = await get_product_detail(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return _product_json(
        product,
        availability=await get_availability(db, product_id),
        include_cost=True,
    )


# JSON: создание товара.
@api.post("/admin/products")
async def api_create_product(
    body: CreateProductBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    data = ProductInput(
        name=body.name,
        brand_id=body.brand_id,
        category_id=body.category_id,
        description=body.description,
        cost_price=body.cost_price,
        sale_price=body.sale_price,
        status=body.status,
    )
    try:
        product = await create_product(db, data, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _product_json(product, include_cost=True)


# JSON: обновление товара.
@api.put("/admin/products/{product_id}")
async def api_update_product(
    product_id: UUID,
    body: UpdateProductBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    data = ProductInput(
        name=body.name,
        brand_id=body.brand_id,
        category_id=body.category_id,
        description=body.description,
        cost_price=body.cost_price,
        sale_price=body.sale_price,
        status=body.status,
    )
    try:
        product = await update_product(db, product_id, data, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return _product_json(product, include_cost=True)


# JSON: загрузка фото.
@api.post("/admin/products/{product_id}/images")
async def api_upload_image(
    product_id: UUID,
    image: UploadFile = File(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
    storage: ObjectStorage = Depends(get_storage),
):
    file_bytes = await image.read()
    content_type = image.content_type or "application/octet-stream"
    try:
        row = await add_product_image(
            db,
            product_id,
            file_bytes,
            content_type,
            storage,
            auth.subject_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return {
        "id": str(row.id),
        "storage_key": row.storage_key,
        "url": f"/media/{row.storage_key}",
        "sort_order": row.sort_order,
    }


# JSON: мягкое удаление товара.
@api.post("/admin/products/{product_id}/delete")
async def api_delete_product(
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        product = await soft_delete_product(db, product_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return _product_json(product, include_cost=True)


# JSON: корректировка остатка.
@api.post("/admin/products/{product_id}/inventory/correct")
async def api_inventory_correct(
    product_id: UUID,
    body: InventoryCorrectBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        inventory = await correct_inventory(
            db,
            product_id,
            body.quantity,
            body.reason,
            auth.subject_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "product_id": str(product_id),
        "quantity_on_hand": inventory.quantity_on_hand,
    }
