from __future__ import annotations

from . import enhanced as enhanced_module
from .admin_catalog_v2_ext import build_admin_catalog_v2_router


def create_app(settings):
    app = enhanced_module.create_app(settings)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        # Preserve the historical app-factory contract for tests and external
        # callers that replace enhanced.create_app with a lightweight object.
        return app

    repo = runtime.repo
    store = repo.variant_store

    router = build_admin_catalog_v2_router(repo, store)
    runtime.dispatcher.include_router(router)
    # The v2 catalog must see its own c2 callbacks and the catalog entry u1 callback
    # before the older admin/variant routers. The router is already parent-bound by
    # include_router; moving the registered object preserves that relationship.
    runtime.dispatcher.sub_routers.insert(0, runtime.dispatcher.sub_routers.pop())
    return app
