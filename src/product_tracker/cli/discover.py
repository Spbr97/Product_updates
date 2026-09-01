"""``search`` and ``track``: finding a product by name instead of pasting URLs.

``track`` deliberately shows what it found and asks before adding anything. The one-word
difference between a Galaxy S25 and a Galaxy S25 FE is four hundred pounds of price, and
both match a search for "Galaxy S25" -- so the confirmation is the feature, not friction.
``--auto`` skips it for scripts, and it only ever accepts exact matches.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ..core.config import get_settings
from ..db.session import session_scope
from ..domain.enums import SearchOutcome
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..domain.models import SearchHit, SearchResult
from ..services import discovery, group_service
from ..services.product_service import ProductService
from ..stores.catalogue import STORES_BY_SLUG
from ..stores.registry import default_registry
from ..utils.money import format_money_short
from .formatting import ExitCode, error, info, stdout, success, table, warn
from .users import UserOption, acting_user

#: How each unsuccessful search outcome reads. A store that refused us has said nothing
#: about whether it stocks the product, and must not look like a store that had none.
_OUTCOME_TEXT: dict[SearchOutcome, str] = {
    SearchOutcome.NO_RESULTS: "no matching products",
    SearchOutcome.BLOCKED: "refused the search (says nothing about whether they stock it)",
    SearchOutcome.PAGE_STRUCTURE: "no readable results (JavaScript-rendered, or rate limited)",
    SearchOutcome.TIMEOUT: "timed out",
    SearchOutcome.ERROR: "search failed",
    SearchOutcome.UNSUPPORTED: "no search configured for this store yet",
    SearchOutcome.NEEDS_BROWSER: "needs a browser to read; install the browser extra",
    SearchOutcome.DISALLOWED: "their robots.txt asks us not to search; products still tracked",
}


def _store_name(slug: str) -> str:
    info_ = STORES_BY_SLUG.get(slug)
    return info_.display_name if info_ else slug


def _hits_table(hits: tuple[SearchHit, ...], title: str) -> None:
    listing = table(title, ["#", "Shop", "Price", "Match", "Title"])
    for index, hit in enumerate(hits, start=1):
        match = f"{hit.score:.0%}"
        if hit.qualifiers:
            # The word that makes this a different phone -- or a case for it.
            match += f" [yellow]+{'/'.join(hit.qualifiers)}[/yellow]"
        name = hit.title[:58]
        if hit.from_sitemap:
            # Say where it came from. This name was read off the URL, not published by the
            # shop, and presenting a derived name as the retailer's own would be a small
            # lie that the rest of this tool works hard not to tell.
            name = f"{name} [dim](from catalogue)[/dim]"
        listing.add_row(
            str(index),
            _store_name(hit.store_slug),
            format_money_short(hit.price, hit.currency),
            match,
            name,
        )
    stdout.print(listing)
    if any(hit.from_sitemap for hit in hits):
        stdout.print(
            "  [dim]Catalogue rows have no price yet: a shop's sitemap lists URLs, not "
            "prices. Their real title and price arrive with the first check.[/dim]"
        )


def _report_gaps(results: tuple[SearchResult, ...]) -> None:
    for result in results:
        text = _OUTCOME_TEXT.get(result.outcome, result.outcome.value)
        stdout.print(f"  [dim]{_store_name(result.store_slug):<20} {text}[/dim]")


def search(
    query: Annotated[str, typer.Argument(help='Product to look for, e.g. "iPhone 17".')],
    limit: Annotated[int, typer.Option("--limit", min=1, max=25)] = 5,
    store: Annotated[
        str | None, typer.Option("--store", help="Search one store only, by slug.")
    ] = None,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser/--no-browser",
            help="Render JavaScript shops when the quick pass finds no exact match.",
        ),
    ] = True,
) -> None:
    """Search the supported shops for a product, without tracking anything.

    The quick pass takes a couple of seconds. Shops that publish their results only with
    JavaScript are rendered afterwards, and only if that quick pass came up short, so the
    common case stays fast. ``--no-browser`` skips rendering entirely.
    """
    settings = get_settings()
    stores = (store,) if store else None
    if browser:
        info("searching... (JavaScript shops are rendered only if this finds nothing)")
    found = discovery.discover(
        query, settings, store_slugs=stores, limit_per_store=limit, allow_browser=browser
    )

    if found.hits:
        stdout.print()
        _hits_table(found.hits[: limit * 2], f'Results for "{query}"')
        exact = len(found.exact)
        stdout.print()
        if exact:
            info(f"{exact} exact match(es). Track them with: product-tracker track {query!r}")
        else:
            warn(
                "No exact match: every result differs from what you asked for. "
                "A '+word' above names a different model, not a different colour."
            )
    else:
        warn(f'Nothing found for "{query}".')

    if found.unsearchable:
        stdout.print()
        stdout.print("[dim]Shops that could not answer:[/dim]")
        _report_gaps(found.unsearchable)
    stdout.print()


def track(
    query: Annotated[str, typer.Argument(help='Product to find and track, e.g. "iPhone 17".')],
    group: Annotated[
        str | None,
        typer.Option("--group", help="Group slug to file the listings under. Derived if unset."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=25)] = 5,
    auto: Annotated[
        bool,
        typer.Option("--auto", help="Track exact matches without asking. Never near-matches."),
    ] = False,
    include_near: Annotated[
        bool,
        typer.Option("--include-near", help="Offer near-matches too, for manual confirmation."),
    ] = False,
    browser: Annotated[
        bool,
        typer.Option("--browser/--no-browser", help="Render JavaScript shops if needed."),
    ] = True,
    user: UserOption = None,
) -> None:
    """Find a product across shops and track the listings you confirm."""
    settings = get_settings()
    found = discovery.discover(
        query, settings, limit_per_store=limit, allow_browser=browser
    )

    candidates = found.best_per_store(exact_only=not include_near)
    if not candidates:
        warn(f'No {"exact " if not include_near else ""}match for "{query}".')
        if found.hits and not include_near:
            info("Re-run with --include-near to see near matches, or with: search")
        if found.unsearchable:
            stdout.print()
            _report_gaps(found.unsearchable)
        raise typer.Exit(ExitCode.NOT_FOUND)

    chosen = tuple(candidates.values())
    stdout.print()
    _hits_table(chosen, f'Best match per shop for "{query}"')
    stdout.print()

    if not auto:
        near = [hit for hit in chosen if not hit.is_exact]
        if near:
            warn(
                f"{len(near)} of these are not exact matches. A '+word' names a "
                "different model -- check before confirming."
            )
        if not typer.confirm(f"Track these {len(chosen)} listing(s)?", default=not near):
            info("Nothing tracked.")
            raise typer.Exit(ExitCode.OK)
    elif any(not hit.is_exact for hit in chosen):
        # --auto exists for scripts, and a script cannot eyeball a title.
        error("--auto only tracks exact matches; re-run without it to confirm these.")
        raise typer.Exit(ExitCode.ERROR)

    slug = group or group_service.slugify(query)
    added, skipped = _track_all(chosen, group_slug=slug, name=query, user=user)

    stdout.print()
    success(f"tracking {added} listing(s) under group {slug}")
    for line in skipped:
        stdout.print(f"  [dim]{line}[/dim]")
    if added:
        info(f"Compare them with: product-tracker compare {slug}")


def _track_all(
    hits: tuple[SearchHit, ...], *, group_slug: str, name: str, user: str | None
) -> tuple[int, list[str]]:
    """Add each listing and file it under the group. Returns (added, notes)."""
    added = 0
    notes: list[str] = []
    with session_scope() as session:
        owner = acting_user(session, user)
        service = ProductService(session, default_registry(), get_settings(), owner.id)

        try:
            group_service.get_group(session, owner.id, group_slug)
        except NotFoundError:
            group_service.create_group(session, user_id=owner.id, slug=group_slug, name=name)

        for hit in hits:
            try:
                product = service.add(hit.url)
            except DuplicateError:
                notes.append(f"{_store_name(hit.store_slug)}: already tracked")
                continue
            except ValidationError as exc:
                notes.append(f"{_store_name(hit.store_slug)}: {exc}")
                continue

            added += 1
            try:
                # The search title is better evidence than the not-yet-fetched listing.
                group_service.attach_product(
                    session,
                    product.id,
                    user_id=owner.id,
                    group_slug=group_slug,
                    label=_variant_label(hit),
                )
            except ValidationError:
                notes.append(
                    f"{_store_name(hit.store_slug)}: tracked, but no model could be read "
                    f"from {hit.title[:40]!r} -- attach it by hand"
                )
    return added, notes


def _variant_label(hit: SearchHit) -> str | None:
    """Infer the model from the search result's title, or leave it to be asked for."""
    from ..services.variants import infer_variant

    label, _attributes = infer_variant(hit.title)
    return label
