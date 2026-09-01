"""Creating groups and attaching listings to the model they actually sell.

Grouping is an *overlay*. Nothing here touches a price, a history row, or a schedule:
attaching a listing to a variant only records "this URL sells that model", and detaching it
leaves the listing tracking exactly as before. That is deliberate -- a person reorganising
their comparison should never be able to destroy months of recorded prices by accident.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..db.models import Product, ProductGroup, ProductVariant, VariantListing
from ..domain.errors import (
    DuplicateError,
    NotFoundError,
    QuotaExceededError,
    ValidationError,
)
from ..repositories.groups import GroupRepository, VariantRepository
from ..repositories.products import ProductRepository
from . import specs
from .specs import detect_category
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
    user_id: int,
    slug: str | None,
    name: str,
    brand: str | None = None,
    notes: str | None = None,
    category: str | None = None,
    settings: Settings | None = None,
) -> ProductGroup:
    """Create a group for one user. ``slug`` defaults to a slugified ``name``."""
    if not name.strip():
        raise ValidationError("a group needs a name")

    resolved = (slug or slugify(name)).strip().casefold()
    if not _SLUG_PATTERN.match(resolved):
        raise ValidationError(
            f"{resolved!r} is not a valid slug: use lowercase letters, digits and hyphens"
        )

    repo = GroupRepository(session)
    limit = (settings or get_settings()).max_groups_per_user
    if len(repo.list_all(user_id)) >= limit:
        raise QuotaExceededError("product groups", limit)

    # Uniqueness is per user: two people may each keep an "iphone-17".
    if repo.get_by_slug(user_id, resolved) is not None:
        raise DuplicateError("product group", resolved)

    return repo.add(
        ProductGroup(
            user_id=user_id,
            slug=resolved,
            name=name.strip(),
            brand=brand,
            category=category,
            notes=notes,
        )
    )


def get_group(session: Session, user_id: int, slug: str) -> ProductGroup:
    """One user's group.

    A slug belonging to somebody else raises NotFoundError rather than a permission error.
    That is the intended answer: telling an unauthorised caller "that exists but is not
    yours" leaks the fact that it exists.
    """
    group = GroupRepository(session).get_by_slug(user_id, slug.strip().casefold())
    if group is None:
        raise NotFoundError("product group", slug)
    return group


def delete_group(session: Session, user_id: int, slug: str) -> None:
    """Remove a group and its variants. Listings survive, with ``variant_id`` set to NULL."""
    GroupRepository(session).delete(get_group(session, user_id, slug))


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
    resolved = (label or (variant_label(attrs, group.category) if attrs else "")).strip()
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
    user_id: int,
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

    group = get_group(session, user_id, group_slug)

    # A group learns its category from the first listing that can tell us one, unless
    # somebody set it explicitly. Outside the inference branch on purpose: `track` supplies
    # a label from the search result, and doing this only when inferring meant every group
    # created that way stayed category-less and showed no specifications at all.
    if group.category is None:
        detected = detect_category(product.name)
        if detected == specs.GENERIC:
            detected = detect_category(product.url)
        if detected and detected != specs.GENERIC:
            group.category = detected
            session.flush()

    resolved_label, resolved_attrs = label, dict(attributes or {})
    if resolved_label is not None and not resolved_attrs:
        # A caller-supplied label still deserves specifications: they are what the grid
        # shows beside the price, and reading them costs nothing.
        _detected, resolved_attrs = infer_variant(product.name, group.category)

    if resolved_label is None and not resolved_attrs:
        # The title is the best evidence; the URL slug is the fallback for listings we
        # were blocked from reading, where the retailer still names the model in the path.
        resolved_label, resolved_attrs = infer_variant(product.name, group.category)
        if resolved_label is None:
            resolved_label, resolved_attrs = infer_variant_from_url(
                product.url, group.category
            )
        if resolved_label is None:
            raise ValidationError(
                f"could not infer a model or colour from {product.name or product.url!r}. "
                "Pass --variant to name it explicitly."
            )

    variant = get_or_create_variant(
        session, group, label=resolved_label, attributes=resolved_attrs
    )

    # Move it within *this* user's grouping only. Another user's link to the same listing
    # is untouched, which is the whole reason the link lives in its own table.
    _clear_links(session, product_id, group.id)
    session.add(VariantListing(variant_id=variant.id, product_id=product.id))
    session.flush()
    return product, variant


def detach_product(session: Session, product_id: int, user_id: int) -> Product:
    """Remove a listing from this user's groups. The listing and its history are untouched."""
    product = ProductRepository(session).get(product_id)
    if product is None:
        raise NotFoundError("product", product_id)

    links = session.execute(
        select(VariantListing)
        .join(ProductVariant, ProductVariant.id == VariantListing.variant_id)
        .join(ProductGroup, ProductGroup.id == ProductVariant.group_id)
        .where(VariantListing.product_id == product_id, ProductGroup.user_id == user_id)
    ).scalars()
    for link in links:
        session.delete(link)
    session.flush()
    return product


def _clear_links(session: Session, product_id: int, group_id: int) -> None:
    """Drop any existing link between this listing and this group.

    A listing sells one model, so re-attaching it to a different variant of the same group
    should move it rather than leave it in both places.
    """
    existing = session.execute(
        select(VariantListing)
        .join(ProductVariant, ProductVariant.id == VariantListing.variant_id)
        .where(VariantListing.product_id == product_id, ProductVariant.group_id == group_id)
    ).scalars()
    for link in existing:
        session.delete(link)
    session.flush()
