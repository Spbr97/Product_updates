# Adding a notification provider

Four steps. The tracking engine and the rule engine are untouched — a provider knows
nothing about products, prices, or why an alert exists.

## 1. Add settings

`src/product_tracker/core/config.py`:

```python
discord_webhook_url: str | None = None
discord_username: str = "Product Tracker"
```

Secrets are `SecretStr`, always. That keeps them out of `repr()`, tracebacks, and
`product-tracker config`. Add the variables to `.env.example` too.

## 2. Write the provider

`src/product_tracker/notifications/discord.py`:

```python
class DiscordProvider(NotificationProvider):
    slug: ClassVar[str] = "discord"
    display_name: ClassVar[str] = "Discord"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.discord_webhook_url)

    def send(self, message: NotificationMessage) -> None:
        if not self.settings.discord_webhook_url:
            raise NotificationDeliveryError(self.slug, "no webhook URL configured")
        try:
            response = httpx.post(
                self.settings.discord_webhook_url,
                json={"content": render_plain_text(message)},
                timeout=self.settings.notification_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Not str(exc): httpx puts the URL in its messages, and the webhook URL is
            # itself a secret — anyone holding it can post to the channel.
            raise NotificationDeliveryError(self.slug, type(exc).__name__) from exc

        if response.status_code >= 400:
            raise NotificationDeliveryError(
                self.slug, f"HTTP {response.status_code}: {response.text[:200]}"
            )
```

## 3. Register it

`src/product_tracker/notifications/registry.py`:

```python
ALL_PROVIDERS: dict[str, type[NotificationProvider]] = {
    ...
    DiscordProvider.slug: DiscordProvider,
}
```

## 4. Enable and test it

```bash
NOTIFY_DEFAULT_PROVIDERS=console,discord
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

`product-tracker status` shows every provider and, separately, whether it is *enabled* and
whether it is *configured* — the common failure is one without the other.

Test with `respx`; never post to a real webhook from the suite:

```python
def test_transport_error_does_not_leak_the_url(self):
    respx.post(HOOK).mock(side_effect=httpx.ConnectError(f"failed connecting to {HOOK}"))
    with pytest.raises(NotificationDeliveryError) as excinfo:
        DiscordProvider(settings(discord_webhook_url=HOOK)).send(MESSAGE)
    assert "SECRETPATH" not in str(excinfo.value)
```

## The contract

**`is_configured()` does no I/O and never raises.** It answers "do I have my settings?" so
the registry can skip a provider without attempting delivery. A provider that is enabled
but unconfigured is skipped with a warning — one bad SMTP password must not stop the
console provider working.

**`send()` raises `NotificationDeliveryError` on failure.** The service records the reason,
marks the row failed, and retries on a later pass up to `MAX_DELIVERY_ATTEMPTS`. A provider
failure never aborts the check that produced the alert.

**No secret in an exception message.** The text is written to `notifications.error` and to
logs. Tokens live in URLs (Telegram), passwords in server replies (SMTP), and the webhook
URL is itself the credential. Raise the exception *type*, or a status code, not `str(exc)`.

**Delivery order is "reach me", not "tell me four times."** Providers are tried in the
order named in `NOTIFY_DEFAULT_PROVIDERS` and the first success wins. A rule can pin one
channel with `--provider`.
