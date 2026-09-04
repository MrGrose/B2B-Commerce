from fastapi import APIRouter, HTTPException, Request

from b2b_commerce.http import templates

html = APIRouter()

LEGAL_PAGES: dict[str, dict[str, str]] = {
    "privacy": {
        "title": "Политика конфиденциальности",
        "summary": "Черновик.",
    },
    "terms": {
        "title": "Пользовательское соглашение",
        "summary": "Черновик.",
    },
    "offer": {
        "title": "Публичная оферта",
        "summary": "Черновик.",
    },
}


# Список юридических страниц.
@html.get("/legal")
async def legal_index(request: Request):
    return templates.TemplateResponse(
        request,
        "legal/index.html",
        {"pages": LEGAL_PAGES},
    )


# Юридическая страница по slug.
@html.get("/legal/{slug}")
async def legal_page(request: Request, slug: str):
    page = LEGAL_PAGES.get(slug)
    if page is None:
        raise HTTPException(status_code=404, detail="Страница не найдена")
    return templates.TemplateResponse(
        request,
        "legal/page.html",
        {"slug": slug, "page": page},
    )
