# Adding a store adapter

Five steps, none of which touch the tracking engine.

## 1. Add a catalogue entry

`src/product_tracker/stores/catalogue.py`:

```python
StoreInfo(
    slug="amazon-in",
    display_name="Amazon India",
    domains=("amazon.in", "www.amazon.in"),
    adapter_key="amazon_in",
),
```

The slug becomes a row in `stores` and the value of `products.store_id`. Domains are
matched exactly or as a subdomain, so `amazon.in` also claims `www.amazon.in` but never
`notamazon.in`.

## 2. Put the selectors in YAML

`src/product_tracker/stores/selectors/amazon_in.yaml`:

```yaml
# Most specific first; the first match wins. Keep old generations around —
# a stale selector costs nothing, a missing one breaks every check.
name:
  - "#productTitle"
price:
  - "span.a-price span.a-offscreen"
out_of_stock:
  - "#outOfStock"
image:
  - "#landingImage"
identifier_param: null
```

Selectors never belong in Python. When a retailer changes its markup — and they all do —
the fix should be a data edit that anyone can make.

## 3. Write the adapter

`src/product_tracker/stores/amazon_in.py`:

```python
class AmazonIndiaAdapter(DomainMatchAdapter):
    slug: ClassVar[str] = "amazon-in"
    display_name: ClassVar[str] = "Amazon India"
    domains: ClassVar[tuple[str, ...]] = ("amazon.in",)

    def fetch_product(self, url: str, ctx: FetchContext) -> FetchResult:
        response = http_fetch(url, ctx)
        if isinstance(response, FetchFailure):
            return failure_to_result(response, FetchMethod.HTTP)
        return self._interpret(response, url, FetchMethod.HTTP)
```

`DomainMatchAdapter` gives you `can_handle_url` from the `domains` tuple.

Copy the browser-fallback structure from `generic.py` if the site is client-rendered.

## 4. Register it

`src/product_tracker/stores/registry.py`:

```python
def _build_default_adapters() -> list[StoreAdapter]:
    return [FlipkartAdapter(), AmazonIndiaAdapter(), GenericStoreAdapter()]
```

Order does not matter — the registry sorts the fallback last regardless, so a generic
adapter can never shadow a named one.

## 5. Test against a saved page, never the live site

```bash
curl -sL "https://www.amazon.in/dp/B0XXXX" -o tests/fixtures/amazon_product.html
```

```python
def test_reads_the_price(self, adapter, ctx):
    stub("https://www.amazon.in/dp/B0XXXX", html=load("amazon_product.html"))
    result = adapter.fetch_product("https://www.amazon.in/dp/B0XXXX", ctx)
    assert result.price == Decimal("69999")
```

The suite must never depend on a retailer being online. Save the page, commit it, assert
against it.

Then: `product-tracker stores sync`.

## The contract

**`fetch_product` must not raise for a site problem.** Return a `FetchResult` whose
`outcome` says what went wrong. Exceptions are for bugs.

**Availability is a separate finding from outcome.** This is the rule that matters most:

| Situation | `outcome` | `availability` |
|---|---|---|
| Price read | `OK` | whatever the page said |
| Page says sold out | `OUT_OF_STOCK` | `OUT_OF_STOCK` |
| Listing 404s | `UNAVAILABLE` | `UNAVAILABLE` |
| Product found, no price | `PRICE_NOT_FOUND` | **`UNKNOWN`** |
| Blocked, timed out, unparseable | `BLOCKED` / `TIMEOUT` / `PAGE_STRUCTURE` | **`UNKNOWN`** |

Never report `OUT_OF_STOCK` because extraction failed. A tracker that cries "out of stock"
whenever its parser breaks teaches the user to ignore every alert it sends.

Equally: do not report `IN_STOCK` without evidence. A page with a price but no stock
statement is `UNKNOWN`, not in stock.

**Never work around an access control.** A CAPTCHA, a login wall, or a bot challenge gets
`FetchOutcome.BLOCKED` and stops there. No solving, no spoofing, no credentials.

**Keep secrets out of error messages.** They are stored in `check_executions.error_detail`
and written to logs. Use `redact_urls()` on anything derived from an exception — a product
URL can carry a session token in its query string.

## When you do not need an adapter

If the site publishes schema.org JSON-LD or OpenGraph product tags, `GenericStoreAdapter`
already handles it. Try adding the URL first and see what `product-tracker check` reports.
Write an adapter when the generic one returns `PRICE_NOT_FOUND` or `PAGE_STRUCTURE`.
