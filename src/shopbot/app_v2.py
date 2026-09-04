from __future__ import annotations

from . import enhanced as enhanced_module
from .admin_catalog_v2_ext import build_admin_catalog_v2_router
from .appearance_studio import build_appearance_studio_router, install_appearance_layer


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

    # Appearance is an additive V2 layer. Keep it ahead of the old admin router so
    # stale Premium Emoji / appearance callbacks are redirected into the new studio.
    if all(hasattr(repo, name) for name in ("coordinator", "sessions", "owner_id")):
        install_appearance_layer(repo)
        appearance_router = build_appearance_studio_router(repo)
        runtime.dispatcher.include_router(appearance_router)
        runtime.dispatcher.sub_routers.insert(0, runtime.dispatcher.sub_routers.pop())

    router = build_admin_catalog_v2_router(repo, store)
    runtime.dispatcher.include_router(router)
    # The v2 catalog must see its own c2 callbacks and the catalog entry u1 callback
    # before the older admin/variant routers. The router is already parent-bound by
    # include_router; moving the registered object preserves that relationship.
    runtime.dispatcher.sub_routers.insert(0, runtime.dispatcher.sub_routers.pop())
    return app
