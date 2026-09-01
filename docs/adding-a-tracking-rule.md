# Adding a tracking condition

Rules are rows, not code branches. A new condition is an evaluator function and a registry
entry; the tracking engine, the repositories, and the notification layer are untouched.

## 1. Add the enum member and a migration

`src/product_tracker/domain/enums.py`:

```python
class RuleType(StrEnum):
    ...
    PRICE_BELOW_AVERAGE = "price_below_average"
```

The values are a native PostgreSQL enum, so this needs a migration:

```python
def upgrade() -> None:
    op.execute("ALTER TYPE rule_type ADD VALUE IF NOT EXISTS 'price_below_average'")

def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum. Recreating the type means rewriting
    # every column that uses it; leaving the value in place is the honest no-op.
    pass
```

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block in older PostgreSQL, so
add `op.execute("COMMIT")` first if you target < 12.

## 2. Write the evaluator

`src/product_tracker/services/rules_engine.py`:

```python
@register(RuleType.PRICE_BELOW_AVERAGE)
def _price_below_average(rule: TrackingRule, ctx: RuleContext) -> RuleMatch | None:
    if ctx.stats is None or ctx.stats.average is None or ctx.current_price is None:
        return None
    if ctx.current_price >= ctx.stats.average:
        return None
    return _price_match(
        rule,
        ctx,
        title=f"Below average: {_name(ctx)}",
        body=f"{format_money(ctx.current_price, ctx.product.currency)} is below the "
             f"{format_money(ctx.stats.average, ctx.product.currency)} average.",
    )
```

**Evaluators must be pure.** No database, no network, no clock. Everything you may look at
is on `RuleContext`, which is what makes the whole rule set testable with plain values. If
you need something that is not there, add it to the context — do not query for it.

## 3. Validate any new parameters

Rule settings live in the JSONB `params` column, so no migration is needed for them. But
validate at creation time, in `validate_params`:

```python
if rule_type is RuleType.PRICE_DROPS_BY_PERCENT:
    percent = params.get("percent")
    if percent is None:
        raise ValidationError("price_drops_by_percent requires a percent")
    ...
    return {**params, "percent": str(value)}
```

A rule that can be saved but can never fire is worse than one that is refused.

## 4. Decide how it deduplicates

`dedupe_signature` has to match how the rule *fires*, or deduplication silently fails:

- **Change rules** — fire on a transition, so key on the transition (`100->90`).
- **State rules** — fire on every check while a condition holds, so key on the *state*.

This is not hypothetical. `price_below_target` originally keyed on the transition and sent
a duplicate on its second check, because `69999->69999` looks like a new alert.

If your rule fires whenever a condition is true, add it alongside `PRICE_BELOW_TARGET`.

## 5. Test it

```python
def test_fires_below_the_average(self):
    ctx = context(current="90", stats=stats(average=Decimal("100")))
    assert evaluate(rule(RuleType.PRICE_BELOW_AVERAGE), ctx, now=NOW) is not None
```

`test_rules_engine.py` has a `test_every_rule_type_has_an_evaluator` guard, so a member
added without an evaluator fails the suite rather than silently never firing.

## Semantics worth copying

Two decisions in the existing rules that a new one should probably follow:

- **Do not fire from `UNKNOWN`.** `became_available` requires a *known*-unavailable
  previous state. Coming from "we don't know" is not "it's back" — announcing that would be
  an invention.
- **Prefer state over crossing.** `price_below_target` fires on being at or below the
  target, not on the moment of crossing it, so setting a target after a drop you missed
  still alerts. Repetition is handled by deduplication and `cooldown_seconds`, not by
  narrowing the condition.
