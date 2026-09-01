"""The tracking-rule engine.

Rules are data, not code branches. Each :class:`RuleType` maps to one evaluator -- a pure
function of ``(rule, RuleContext)`` returning a :class:`RuleMatch` or ``None``. Adding a
condition means writing an evaluator and registering it; the tracking engine, the
repositories, and the notification layer are untouched.

Evaluators must stay pure: no database, no network, no clock. Everything they may look at
is on the context, which is what makes the whole rule set testable with plain values.

Rule-specific settings live in the rule's ``params`` JSONB column, so a new condition with
new options needs no migration.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..core.logging import EVENT_RULE_MATCHED, get_logger
from ..db.models import TrackingRule
from ..domain.enums import Availability, RuleType
from ..domain.errors import ValidationError
from ..domain.models import RuleContext, RuleMatch
from ..utils.money import format_money

log = get_logger(__name__)

Evaluator = Callable[[TrackingRule, RuleContext], RuleMatch | None]

#: RuleType -> evaluator. The registry the engine walks.
RULE_EVALUATORS: dict[RuleType, Evaluator] = {}

#: Availability states that mean "you cannot buy this right now", as opposed to UNKNOWN,
#: which means we do not know. The distinction drives the availability rules.
_NOT_PURCHASABLE = frozenset({Availability.OUT_OF_STOCK, Availability.UNAVAILABLE})


def register(rule_type: RuleType) -> Callable[[Evaluator], Evaluator]:
    """Bind an evaluator to a rule type."""

    def decorator(func: Evaluator) -> Evaluator:
        RULE_EVALUATORS[rule_type] = func
        return func

    return decorator


# --- Parameter validation ---------------------------------------------------------


def target_price_of(rule: TrackingRule) -> Decimal | None:
    raw = rule.params.get("target_price")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def validate_params(rule_type: RuleType, params: dict[str, object]) -> dict[str, object]:
    """Check a rule's parameters at creation time, so a rule cannot be saved unusable.

    Returns the normalised params. Raises :class:`ValidationError` when they are wrong.
    """
    if rule_type is RuleType.PRICE_BELOW_TARGET:
        raw = params.get("target_price")
        if raw is None:
            raise ValidationError("price_below_target requires a target_price")
        try:
            target = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(f"target_price {raw!r} is not a number") from exc
        if target <= 0:
            raise ValidationError("target_price must be greater than zero")
        # Stored as a string: JSONB would otherwise round-trip it through a float.
        return {**params, "target_price": str(target)}
    return dict(params)


# --- Evaluators -------------------------------------------------------------------


@register(RuleType.PRICE_CHANGED)
def _price_changed(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    previous, current = ctx.previous_price, ctx.current_price
    if previous is None or current is None or current == previous:
        return None
    direction = "dropped" if current < previous else "rose"
    return _price_match(
        rule,
        ctx,
        title=f"Price {direction}: {_name(ctx)}",
        body=_price_body(ctx, previous, current),
    )


@register(RuleType.PRICE_DROPPED)
def _price_dropped(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    previous, current = ctx.previous_price, ctx.current_price
    if previous is None or current is None or current >= previous:
        return None
    return _price_match(
        rule,
        ctx,
        title=f"Price drop: {_name(ctx)}",
        body=_price_body(ctx, previous, current),
    )


@register(RuleType.PRICE_INCREASED)
def _price_increased(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    previous, current = ctx.previous_price, ctx.current_price
    if previous is None or current is None or current <= previous:
        return None
    return _price_match(
        rule,
        ctx,
        title=f"Price increase: {_name(ctx)}",
        body=_price_body(ctx, previous, current),
    )


@register(RuleType.PRICE_BELOW_TARGET)
def _price_below_target(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    """Fires on the *state* of being at or below the target, not on crossing it.

    Crossing would never fire for a product that was already below target when the rule
    was created -- the case where someone sets a target after a drop they missed.
    Repetition is handled by deduplication and the rule's cooldown, not by narrowing this
    condition.
    """
    target = target_price_of(rule)
    current = ctx.current_price
    if target is None or current is None or current > target:
        return None
    currency = ctx.product.currency
    return _price_match(
        rule,
        ctx,
        title=f"Target price reached: {_name(ctx)}",
        body=(
            f"{format_money(current, currency)} is at or below your target of "
            f"{format_money(target, currency)}."
        ),
        extra={"target_price": str(target)},
    )


@register(RuleType.BECAME_AVAILABLE)
def _became_available(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    """Requires a known-unavailable previous state.

    Coming from ``UNKNOWN`` is not "became available" -- we never established that it was
    unavailable, so announcing that it came back would be an invention.
    """
    if ctx.previous_availability not in _NOT_PURCHASABLE:
        return None
    if ctx.current_availability is not Availability.IN_STOCK:
        return None
    price = format_money(ctx.current_price, ctx.product.currency)
    return _availability_match(
        rule,
        ctx,
        title=f"Back in stock: {_name(ctx)}",
        body=f"Now available at {price}.",
    )


@register(RuleType.BECAME_UNAVAILABLE)
def _became_unavailable(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    if ctx.previous_availability is not Availability.IN_STOCK:
        return None
    if ctx.current_availability not in _NOT_PURCHASABLE:
        return None
    return _availability_match(
        rule,
        ctx,
        title=f"Out of stock: {_name(ctx)}",
        body=f"No longer available ({ctx.current_availability.value}).",
    )


# --- Engine -----------------------------------------------------------------------


def evaluate(rule: TrackingRule, ctx: RuleContext, *, now: datetime) -> RuleMatch | None:
    """Evaluate one rule, honouring ``enabled`` and its cooldown."""
    if not rule.enabled:
        return None
    if _in_cooldown(rule, now):
        return None

    evaluator = RULE_EVALUATORS.get(rule.rule_type)
    if evaluator is None:
        # A rule type in the database with no evaluator in this build: log it rather than
        # crash the check, so one stale row cannot stop a product being tracked.
        log.warning("rule.no_evaluator", rule_id=rule.id, rule_type=rule.rule_type.value)
        return None

    return evaluator(rule, ctx)


def evaluate_all(
    rules: list[TrackingRule], ctx: RuleContext, *, now: datetime
) -> list[RuleMatch]:
    """Evaluate every rule for a product, in id order for predictable output."""
    matches = []
    for rule in rules:
        match = evaluate(rule, ctx, now=now)
        if match is not None:
            log.info(
                EVENT_RULE_MATCHED,
                rule_id=rule.id,
                rule_type=rule.rule_type.value,
                product_id=ctx.product.id,
            )
            matches.append(match)
    return matches


def _in_cooldown(rule: TrackingRule, now: datetime) -> bool:
    if not rule.cooldown_seconds or rule.last_fired_at is None:
        return False
    return now < rule.last_fired_at + timedelta(seconds=rule.cooldown_seconds)


# --- Match construction -----------------------------------------------------------


def _name(ctx: RuleContext) -> str:
    return ctx.product.name or ctx.product.url


def _price_body(ctx: RuleContext, previous: Decimal, current: Decimal) -> str:
    currency = ctx.product.currency
    difference = current - previous
    pct = (
        f" ({difference / previous * 100:+.1f}%)"
        if previous
        else ""
    )
    return (
        f"{format_money(previous, currency)} -> {format_money(current, currency)}"
        f"{pct}"
    )


def _price_match(
    rule: TrackingRule,
    ctx: RuleContext,
    *,
    title: str,
    body: str,
    extra: dict[str, object] | None = None,
) -> RuleMatch:
    context: dict[str, object] = {
        "previous_price": str(ctx.previous_price) if ctx.previous_price is not None else None,
        "current_price": str(ctx.current_price) if ctx.current_price is not None else None,
        "currency": ctx.product.currency,
        "url": ctx.product.url,
        "notify_provider": rule.notify_provider,
        **(extra or {}),
    }
    return RuleMatch(
        rule_id=rule.id,
        rule_type=rule.rule_type,
        product_id=ctx.product.id,
        title=title,
        body=body,
        context=context,
    )


def _availability_match(
    rule: TrackingRule, ctx: RuleContext, *, title: str, body: str
) -> RuleMatch:
    return RuleMatch(
        rule_id=rule.id,
        rule_type=rule.rule_type,
        product_id=ctx.product.id,
        title=title,
        body=body,
        context={
            "previous_availability": ctx.previous_availability.value,
            "current_availability": ctx.current_availability.value,
            "current_price": str(ctx.current_price) if ctx.current_price is not None else None,
            "currency": ctx.product.currency,
            "url": ctx.product.url,
            "notify_provider": rule.notify_provider,
        },
    )


#: Rules that fire on a *transition*. They only match when something moved, so the
#: transition itself identifies the alert.
_CHANGE_RULES = frozenset(
    {RuleType.PRICE_CHANGED, RuleType.PRICE_DROPPED, RuleType.PRICE_INCREASED}
)


def dedupe_signature(match: RuleMatch) -> str:
    """The part of a notification's identity that distinguishes one alert from another.

    The signature has to match how the rule fires, or deduplication silently fails:

    * **Change rules** key on the transition (``100->90``). They only fire when there is
      one, so two different drops are two different alerts.
    * **State rules** -- ``price_below_target`` -- key on the *state*, because they fire on
      every check while the condition holds. Keying those on the transition would make the
      second check ("69999->69999") look like a new alert and send a duplicate.
    * **Availability rules** key on the resulting state, for the same reason.
    """
    context = match.context
    if match.rule_type in _CHANGE_RULES:
        return f"{context.get('previous_price')}->{context.get('current_price')}"
    if match.rule_type is RuleType.PRICE_BELOW_TARGET:
        # Target included so that lowering it re-alerts at the same price.
        return f"at:{context.get('current_price')}<={context.get('target_price')}"
    return str(context.get("current_availability"))
