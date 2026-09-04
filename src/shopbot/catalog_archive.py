from __future__ import annotations

from types import MethodType
from uuid import UUID

from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, select, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import ProductRow
from .repository import InvalidState, ShopRepository
from .variant_store import VariantStore, families, variants

metadata = MetaData()

catalog_archives = Table(
    "catalog_archives",
    metadata,
    Column("entity_type", String(16), primary_key=True),
    Column("entity_id", PGUUID(as_uuid=True), primary_key=True),
    Column("archived_at", DateTime(timezone=True), nullable=False),
    Column("archived_by", BigInteger, nullable=False),
)


class CatalogArchiveService:
    """Soft-delete catalog entities while preserving order and quote history."""

    def __init__(self, repo: ShopRepository, store: VariantStore):
        self.repo = repo
        self.store = store

    async def archived_ids(self, entity_type: str) -> set[UUID]:
        async with self.repo.sessions() as session:
            result = await session.scalars(
                select(catalog_archives.c.entity_id).where(
                    catalog_archives.c.entity_type == entity_type
                )
            )
            return set(result.all())

    async def _record(self, session, entity_type: str, entity_id: UUID, actor: int) -> None:
        statement = pg_insert(catalog_archives).values(
            entity_type=entity_type,
            entity_id=entity_id,
            archived_at=self.store.now(),
            archived_by=actor,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[catalog_archives.c.entity_type, catalog_archives.c.entity_id],
            set_={
                "archived_at": self.store.now(),
                "archived_by": actor,
            },
        )
        await session.execute(statement)

    async def archive_variant(self, actor: int, variant_id: UUID) -> UUID:
        self.repo.owner(actor)
        async with self.repo.sessions.begin() as session:
            row = (
                await session.execute(
                    select(
                        variants.c.family_id,
                        variants.c.legacy_product_id,
                    ).where(variants.c.id == variant_id)
                )
            ).mappings().first()
            if not row:
                raise InvalidState("VARIANT_NOT_FOUND")

            await self._record(session, "variant", variant_id, actor)
            await session.execute(
                update(variants)
                .where(variants.c.id == variant_id)
                .values(active=False)
            )
            product = await session.get(
                ProductRow,
                row["legacy_product_id"],
                with_for_update=True,
            )
            if product:
                product.active = False
            await self.repo.audit(
                session,
                actor,
                "product_variant.archive",
                str(variant_id),
                "soft_delete=true",
            )
            return row["family_id"]

    async def archive_family(self, actor: int, family_id: UUID) -> None:
        self.repo.owner(actor)
        async with self.repo.sessions.begin() as session:
            family = (
                await session.execute(
                    select(families.c.id).where(families.c.id == family_id)
                )
            ).first()
            if not family:
                raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")

            plan_rows = (
                await session.execute(
                    select(
                        variants.c.id,
                        variants.c.legacy_product_id,
                    ).where(variants.c.family_id == family_id)
                )
            ).mappings().all()

            await self._record(session, "family", family_id, actor)
            await session.execute(
                update(families)
                .where(families.c.id == family_id)
                .values(active=False)
            )

            if plan_rows:
                variant_ids = [row["id"] for row in plan_rows]
                product_ids = [row["legacy_product_id"] for row in plan_rows]
                for variant_id in variant_ids:
                    await self._record(session, "variant", variant_id, actor)
                await session.execute(
                    update(variants)
                    .where(variants.c.id.in_(variant_ids))
                    .values(active=False)
                )
                await session.execute(
                    update(ProductRow)
                    .where(ProductRow.id.in_(product_ids))
                    .values(active=False)
                )

            await self.repo.audit(
                session,
                actor,
                "product_family.archive",
                str(family_id),
                f"soft_delete=true plans={len(plan_rows)}",
            )

    async def visible_rows(self, entity_type: str, rows: list[dict]) -> list[dict]:
        if not rows:
            return rows
        archived = await self.archived_ids(entity_type)
        return [row for row in rows if row["id"] not in archived]


def install_catalog_archive(repo: ShopRepository, store: VariantStore) -> CatalogArchiveService:
    existing = getattr(store, "_catalog_archive_service", None)
    if isinstance(existing, CatalogArchiveService):
        return existing

    service = CatalogArchiveService(repo, store)
    original_owner_families = store.owner_families
    original_storefront_families = store.storefront_families
    original_family_variants = store.family_variants

    async def owner_families_filtered(_self) -> list[dict]:
        return await service.visible_rows("family", await original_owner_families())

    async def storefront_families_filtered(_self, category_id: UUID) -> list[dict]:
        return await service.visible_rows(
            "family",
            await original_storefront_families(category_id),
        )

    async def family_variants_filtered(
        _self,
        family_id: UUID,
        *,
        owner: bool = False,
    ) -> list[dict]:
        return await service.visible_rows(
            "variant",
            await original_family_variants(family_id, owner=owner),
        )

    store.owner_families = MethodType(owner_families_filtered, store)
    store.storefront_families = MethodType(storefront_families_filtered, store)
    store.family_variants = MethodType(family_variants_filtered, store)
    setattr(store, "_catalog_archive_service", service)
    return service


def get_catalog_archive(store: VariantStore) -> CatalogArchiveService:
    service = getattr(store, "_catalog_archive_service", None)
    if not isinstance(service, CatalogArchiveService):
        raise RuntimeError("catalog archive service is not installed")
    return service
