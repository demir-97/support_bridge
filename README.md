# Support Bridge

Native Discuss chat between two separate Odoo databases.

An Odoo partner runs **Support Bridge Hub** on their own instance. Each of their
customers installs **Support Bridge Client** on theirs. A Discuss channel opens on
both sides and messages, reactions and attachments flow between them — no email,
no portal login, no third-party service. Both parties stay in the chat window they
already use every day.

> 📄 Ayrıntılı Türkçe teknik dokümantasyon: **[SUPPORT_BRIDGE_DOKUMANTASYON.md](SUPPORT_BRIDGE_DOKUMANTASYON.md)**
> (mimari kararlar, kod haritası, olası sorular ve cevapları)

## Modules

| Module | Installed on | Purpose |
| --- | --- | --- |
| `support_bridge_hub` | the vendor / partner instance | One record and one sub-channel per customer, each with its own API key |
| `support_bridge_client` | each customer instance | One connection to the vendor, one support channel, outbox + polling |

Neither module depends on the other. A customer never installs the hub, and vice
versa. Both target **Odoo 19.0** and depend only on `mail`.

## How a message travels

Customer → vendor is always **push**: the client posts to the hub over HTTPS the
moment the transaction commits, in a background thread so the sender never waits
on the network. If the hub is unreachable the message lands in an outbox and a
cron retries it every 5 minutes.

Vendor → customer has two paths, because a customer's Odoo often sits behind NAT
with no inbound reachability:

- **Push**, when the customer ticks *Publicly Reachable* and supplies their own
  public URL. The hub posts replies straight to them and they arrive instantly.
- **Poll**, always active as the fallback. A cron asks the hub for anything newer
  than its cursor once a minute.

Both paths stay on regardless. Push is the fast path, polling is the guarantee —
a customer behind a firewall simply never gets the fast path, and nothing else
about the product changes for them.

## Configuration

**On the hub.** Create a customer under *Support Hub*. An API key is generated
automatically. Hand the customer that key together with your server address. The
customer record's name is replaced with their real company name the first time
they connect, which is the clearest confirmation that the handshake worked.

**On the client.** Fill in the vendor's URL and the key you were given, then press
*Connect*. If your Odoo has a public address, tick *Publicly Reachable* and enter
it to get instant replies.

Every customer gets a distinct key. Revoking one, or archiving the customer,
cuts that customer off without touching anyone else's history.

## HTTP interface

All requests authenticate with `Authorization: Bearer <api_key>`.

| Route | On | Purpose |
| --- | --- | --- |
| `POST /support_bridge/ping` | hub | Handshake; also reports the client's public URL |
| `POST /support_bridge/inbound` | hub | A customer message |
| `POST /support_bridge/reaction` | hub | A customer emoji reaction |
| `GET /support_bridge/outbound?since=<id>` | hub | Events the client has not seen |
| `POST /support_bridge/deliver` | client | A vendor event pushed to a reachable client |

## Known limits

Attachments are capped at **20 MB per file** and **20 files per message**. Anything
over the cap is not delivered, and both the sender and the recipient are told so
by name — a silent drop would be worse than the limit itself.

Remote authors are matched on the **partner id from the originating instance**, not
on name, so two people sharing a name stay distinct and renaming someone updates
the existing contact instead of orphaning it. Email travels along as an attribute
but is never a matching key.

Agents on the hub share one pool: every internal user listed on any customer can
see the shared parent channel, and therefore the customer sub-channels under it.
This follows from how Odoo nests sub-channels and is documented rather than
worked around — see the Turkish documentation for the reasoning.

## Development

The repository root is an Odoo addons directory. Point `addons_path` at it, or
copy the two module folders into an existing one.

```
support_bridge_hub/       # vendor side
support_bridge_client/    # customer side
```

## License

Odoo Proprietary License v1.0 (OPL-1). See [LICENSE](LICENSE).

Copyright © CodeQuarters.
