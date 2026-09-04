from uuid import uuid4

import pytest

from shopbot.catalog_archive import install_catalog_archive


class FakeStore:
    def __init__(self, family_keep, family_archived, variant_keep, variant_archived):
        self.family_keep = family_keep
        self.family_archived = family_archived
        self.variant_keep = variant_keep
        self.variant_archived = variant_archived

    async def owner_families(self):
        return [
            {"id": self.family_keep},
            {"id": self.family_archived},
        ]

    async def storefront_families(self, _category_id):
        return [
            {"id": self.family_keep},
            {"id": self.family_archived},
        ]

    async def family_variants(self, _family_id, *, owner=False):
        assert isinstance(owner, bool)
        return [
            {"id": self.variant_keep},
            {"id": self.variant_archived},
        ]


@pytest.mark.asyncio
async def test_catalog_archive_filters_soft_deleted_catalog_rows(monkeypatch):
    family_keep = uuid4()
    family_archived = uuid4()
    variant_keep = uuid4()
    variant_archived = uuid4()
    store = FakeStore(
        family_keep,
        family_archived,
        variant_keep,
        variant_archived,
    )
    service = install_catalog_archive(object(), store)

    async def archived_ids(entity_type):
        if entity_type == "family":
            return {family_archived}
        return {variant_archived}

    monkeypatch.setattr(service, "archived_ids", archived_ids)

    assert await store.owner_families() == [{"id": family_keep}]
    assert await store.storefront_families(uuid4()) == [{"id": family_keep}]
    assert await store.family_variants(uuid4(), owner=True) == [{"id": variant_keep}]


def test_catalog_archive_install_is_idempotent():
    store = FakeStore(uuid4(), uuid4(), uuid4(), uuid4())
    first = install_catalog_archive(object(), store)
    second = install_catalog_archive(object(), store)
    assert first is second
