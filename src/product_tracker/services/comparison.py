"""Turning tracked listings into the grid a person actually wants to read.

One product, its models down the side, the shops across the top. The work here is almost
entirely about *not lying* in a small space: a blank cell is the easiest thing to render and
the most dishonest, because "we are blocked", "it is sold out", "we could not read the
price" and "nobody tracks it here" all collapse into the same emptiness, and only one of
them is a fact about the product.

So every cell carries a :class:`CellStatus` saying which of those it is, and the renderers
are expected to show the difference.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from ..db.models import Product, ProductVariant
from ..domain.enums import Availability, CellStatus, CheckStatus, FetchOutcome
from ..domain.models import ComparisonCell, ComparisonMatrix, ComparisonRow
from ..repositories.groups import GridData, GroupRepository

#: Outcomes where the fetch itself failed, so the absence of a price says nothing about
#: the listing. Distinct from PRICE_NOT_FOUND, where we did read the page.
_FAILURE_OUTCOMES = frozenset(
    {
        FetchOutcome.TIMEOUT.value,
        FetchOutcome.HTTP_ERROR.value,
        FetchOutcome.ERROR.value,
        FetchOutcome.PAGE_STRUCTURE.value,
    }
)

DEFAULT_STALE_AFTER = timedelta(hours=6)


class GroupNotFoundError(LookupError):
    def __init__(self, slug: str) -> None:
        super().__init__(f"no product group with slug {slug!r}")
        self.slug = slug


def build_matrix(
    session: Session,
    slug: str,
    *,
    user_id: int,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    now: datetime | None = None,
) -> ComparisonMatrix:
    """Assemble the comparison grid for one of ``user_id``'s groups.

    Raises :class:`GroupNotFoundError` when the slug is unknown *to this user* -- a group
    someone else owns is not found rather than forbidden, so the response does not confirm
    that it exists.

    The listings and prices inside the grid are shared data: several users can compare the
    same tracked URLs, and each fetch serves all of them.
    """
    repo = GroupRepository(session)
    group = repo.get_by_slug(user_id, slug)
    if group is None:
        raise GroupNotFoundError(slug)

    grid = repo.load_grid(group)
    moment = now or datetime.now(UTC)

    store_names = _store_names(grid)
    # Columns ordered by display name so the grid is stable between renders.
    store_slugs = tuple(sorted(store_names, key=lambda s: store_names[s].casefold()))

    rows = tuple(
        _build_row(variant, grid, store_slugs, stale_after=stale_after, now=moment)
        for variant in grid.variants
    )

    currencies = sorted(
        {
            cell.currency
            for row in rows
            for cell in row.cells.values()
            if cell.has_price and cell.currency
        }
    )

    return ComparisonMatrix(
        group_slug=group.slug,
        group_name=group.name,
        brand=group.brand,
        store_slugs=store_slugs,
        store_names=store_names,
        rows=rows,
        generated_at=moment,
        currencies=tuple(currencies),
    )


def _store_names(grid: GridData) -> dict[str, str]:
    """Every store that appears anywhere in this group, slug -> display name."""
    names: dict[str, str] = {}
    for products in grid.products_by_variant.values():
        for product in products:
            names[product.store.slug] = product.store.name
    return names


def _one_per_store(products: list[Product]) -> dict[str, Product]:
    """Pick one listing per shop for this model.

    A shop can legitimately carry the same model twice -- two sellers, or two URLs for one
    product -- and the grid has one square per shop. Building the mapping by comprehension
    keeps whichever happened to come last, so a listing disappears from the comparison
    depending on row order, which is the kind of thing nobody notices until a price looks
    wrong.

    The cheapest buyable one wins, because that is the number a shopper acts on. Listings
    with no usable price never displace one that has a price.
    """
    chosen: dict[str, Product] = {}
    for product in products:
        slug = product.store.slug
        current = chosen.get(slug)
        if current is None or _prefer(product, current):
            chosen[slug] = product
    return chosen


def _prefer(candidate: Product, incumbent: Product) -> bool:
    """Whether ``candidate`` should replace ``incumbent`` in its shop's square."""
    if candidate.current_price is None:
        return False
    if incumbent.current_price is None:
        return True
    if candidate.current_price != incumbent.current_price:
        return candidate.current_price < incumbent.current_price
    # Equal prices: settle on the lower id so the grid does not change between renders.
    return candidate.id < incumbent.id


def _build_row(
    variant: ProductVariant,
    grid: GridData,
    store_slugs: tuple[str, ...],
    *,
    stale_after: timedelta,
    now: datetime,
) -> ComparisonRow:
    products = grid.products_by_variant.get(variant.id, [])
    by_store = _one_per_store(products)

    cells: dict[str, ComparisonCell] = {}
    for slug in store_slugs:
        product = by_store.get(slug)
        if product is None:
            # No listing at this shop for this model. Not a fact about stock.
            cells[slug] = ComparisonCell(status=CellStatus.NOT_TRACKED)
            continue
        cells[slug] = cell_for_product(
            product,
            previous_price=grid.previous_price.get(product.id),
            last_check=grid.last_check.get(product.id),
            stale_after=stale_after,
            now=now,
        )

    return ComparisonRow(
        variant_id=variant.id,
        label=variant.label,
        attributes=dict(variant.attributes or {}),
        cells=cells,
    )


def _classify(
    product: Product,
    status_value: str | None,
    error_type: str | None,
) -> CellStatus:
    """Which of the several meanings of "no price" applies to this listing.

    The ordering of these branches is the whole design. A block is reported *before* stock,
    because a shop that refused the request has told us nothing about whether the item is
    buyable -- reporting "out of stock" there would be the exact failure this project
    refuses to make.
    """
    if product.last_checked_at is None:
        return CellStatus.NEVER_CHECKED
    if error_type == FetchOutcome.BLOCKED.value:
        return CellStatus.BLOCKED
    if product.availability is Availability.OUT_OF_STOCK:
        return CellStatus.OUT_OF_STOCK
    if product.current_price is None:
        failed = error_type in _FAILURE_OUTCOMES or status_value == CheckStatus.FAILED.value
        return CellStatus.FAILED if failed else CellStatus.NO_PRICE
    return CellStatus.OK


def cell_for_product(
    product: Product,
    *,
    previous_price: Decimal | None = None,
    last_check: tuple[str, str | None] | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    now: datetime | None = None,
) -> ComparisonCell:
    """Classify one listing into a grid cell."""
    moment = now or datetime.now(UTC)
    status_value, error_type = last_check or (None, None)

    status = _classify(product, status_value, error_type)
    aged = (
        product.last_checked_at is not None and (moment - product.last_checked_at) > stale_after
    )
    # A price whose most recent check did not read one is the *last* price we saw, not a
    # price we just confirmed. Age alone would call it fresh: the check ran a minute ago,
    # it simply came back without a price. Showing that as a confident current figure is
    # the same class of overclaim as calling a failed extraction "out of stock".
    unconfirmed = status is CellStatus.OK and not (
        status_value == CheckStatus.SUCCESS.value and error_type is None
    )

    return ComparisonCell(
        status=status,
        price=product.current_price,
        currency=product.currency,
        availability=product.availability,
        product_id=product.id,
        url=product.url,
        last_checked_at=product.last_checked_at,
        is_stale=aged or unconfirmed,
        previous_price=previous_price,
    )
