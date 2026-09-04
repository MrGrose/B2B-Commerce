# Подключает модели доменов к Base.metadata для Alembic.


def load_models() -> None:
    from b2b_commerce.audit import models as audit_models
    from b2b_commerce.auth import models as auth_models
    from b2b_commerce.cart import models as cart_models
    from b2b_commerce.catalog import models as catalog_models
    from b2b_commerce.companies import models as companies_models
    from b2b_commerce.inventory import models as inventory_models
    from b2b_commerce.invoices import models as invoices_models
    from b2b_commerce.payments import models as payments_models
    from b2b_commerce.rapira import models as rapira_models
    from b2b_commerce.support import models as support_models

    _ = (
        audit_models,
        auth_models,
        cart_models,
        catalog_models,
        companies_models,
        inventory_models,
        invoices_models,
        payments_models,
        rapira_models,
        support_models,
    )
