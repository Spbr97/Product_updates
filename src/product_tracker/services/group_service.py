"""Creating groups and attaching listings to the model they actually sell.

Grouping is an *overlay*. Nothing here touches a price, a history row, or a schedule:
attaching a listing to a variant only records "this URL sells that model", and detaching it
leaves the listing tracking exactly as before. That is deliberate -- a person reorganising
their comparison should never be able to destroy months of recorded prices by accident.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..db.models import Product, ProductGroup, ProductVariant
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..repositories.groups import GroupRepository, VariantRepository
from ..repositories.products import ProductRepository
from .variants import infer_variant, infer_variant_from_url, sort_position, variant_label

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SLUG = 64


def slugify(text: str) -> str:
    """A URL-safe slug: "iPhone 17 Pro" -> "iphone-17-pro"."""
    lowered = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return lowered[:_MAX_SLUG].strip("-")


def create_group(
    session: Session,
    *,
    slug: str | None,
    name: str,
    brand: str | None = None,
    notes: str | None = None,
) -> ProductGroup:
    """Create a group. ``slug`` defaults to a slugified ``name``."""
    if not name.strip():
        raise ValidationError("a group needs a name")

    resolved = (slug or slugify(name)).strip().casefold()
    if not _SLUG_PATTERN.match(resolved):
        raise ValidationError(
            f"{resolved!r} is not a valid slug: use lowercase letters, digits and hyphens"
        )

    repo = GroupRepository(session)
    if repo.get_by_slug(resolved) is not None:
        raise DuplicateError("product group", resolved)

    return repo.add(
        ProductGroup(slug=resolved, name=name.strip(), brand=brand, notes=notes)
    )


def get_group(session: Session, slug: str) -> ProductGroup:
    group = GroupRepository(session).get_by_slug(slug.strip().casefold())
    if group is None:
        raise NotFoundError("product group", slug)
    return group


def delete_group(session: Session, slug: str) -> None:
    """Remove a group and its variants. Listings survive, with ``variant_id`` set to NULL."""
    GroupRepository(session).delete(get_group(session, slug))


def get_or_create_variant(
    session: Session,
    group: ProductGroup,
    *,
    label: str | None = None,
    attributes: dict[str, str] | None = None,
) -> ProductVariant:
    """Find or create the variant identified by ``label``.

    When ``attributes`` are given without a label, the label is derived from them, so the
    same attributes always resolve to the same row rather than creating near-duplicates.
    """
    attrs = dict(attributes or {})
    resolved = (label or (variant_label(attrs) if attrs else "")).strip()
    if not resolved:
        raise ValidationError("a variant needs a label or attributes to derive one from")

    repo = VariantRepository(session)
    existing = repo.get_by_label(group.id, resolved)
    if existing is not None:
        # Fill in attributes learned later without overwriting what is already recorded.
        if attrs and not existing.attributes:
            existing.attributes = attrs
            existing.position = sort_position(attrs)
        return existing

    return repo.add(
        ProductVariant(
            group_id=group.id,
            label=resolved,
            attributes=attrs,
            position=sort_position(attrs),
        )
    )


def attach_product(
    session: Session,
    product_id: int,
    *,
    group_slug: str,
    label: str | None = None,
    attributes: dict[str, str] | None = None,
) -> tuple[Product, ProductVariant]:
    """Point a tracked listing at the model it sells.

    With no label and no attributes, the variant is inferred from the listing's title. If
    nothing can be inferred, this raises rather than inventing a label -- a wrong grouping
    silently merges two different phones, which is worse than being asked.
    """
    product = ProductRepository(session).get(product_id)
    if product is None:
        raise NotFoundError("product", product_id)

    group = get_group(session, group_slug)

    resolved_label, resolved_attrs = label, dict(attributes or {})
    if resolved_label is None and not resolved_attrs:
        # The title is the best evidence; the URL slug is the fallback for listings we
        # were blocked from reading, where the retailer still names the model in the path.
        resolved_label, resolved_attrs = infer_variant(product.name)
        if resolved_label is None:
            resolved_label, resolved_attrs = infer_variant_from_url(product.url)
        if resolved_label is None:
            raise ValidationError(
                f"could not infer a model or colour from {product.name or product.url!r}. "
                "Pass --variant to name it explicitly."
            )

    variant = get_or_create_variant(
        session, group, label=resolved_label, attributes=resolved_attrs
    )
    product.variant_id = variant.id
    session.flush()
    return product, variant


def detach_product(session: Session, product_id: int) -> Product:
    """Remove a listing from its group. The listing and its history are untouched."""
    product = ProductRepository(session).get(product_id)
    if product is None:
        raise NotFoundError("product", product_id)
    product.variant_id = None
    session.flush()
    return product
