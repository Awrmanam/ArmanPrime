from __future__ import annotations

from .admin_catalog_v2_ext import build_admin_catalog_v2_router
from .enhanced import create_app as create_base_app


def create_app(settings):
    app = create_base_app(settings)
    runtime = app.state.runtime
    repo = runtime.repo
    store = repo.variant_store

    router = build_admin_catalog_v2_router(repo, store)
    runtime.dispatcher.include_router(router)
    # The v2 catalog must see its own c2 callbacks and the catalog entry u1 callback
    # before the older admin/variant routers. The router is already parent-bound by
    # include_router; moving the registered object preserves that relationship.
    runtime.dispatcher.sub_routers.insert(0, runtime.dispatcher.sub_routers.pop())
    return app
