# Reading your world from other tools

A world is one graph, and the console is one way to look at it. Most of the tools
people already use to look at data — a SQL client, a spreadsheet, a BI dashboard, a
Slack channel, a coding agent — can read the same world through a **door** built for
them. This chapter says which doors exist, who each one is for, and how to open one.

Three things are true of every door, and they are worth knowing before the details:

- **Your login is forwarded, never held.** A door signs in to the appliance with the
  credentials the client sent, so what a client reads is what that user may read.
  A door that runs beside the appliance keeps no credentials of its own beyond the
  one some of them need to read the catalog.
- **Nothing is copied.** A door reads the world when asked and returns what it holds
  now. A materialised view returns its snapshot until its TTL, the same as in the
  console.
- **A world that could not answer is not an empty answer.** When a view fails, or a
  read would be too expensive, the door refuses with the reason, in the vocabulary of
  the protocol it speaks. An empty table means the world holds nothing there.

## Which door

| You are | Use | Speaks |
|---|---|---|
| At a terminal | The [CLI](/cli/): `embabel run-view`, `embabel diagram` | the REST API |
| Writing code or a script | The [REST API](/api/), or GraphQL below | HTTPS + JSON |
| A coding agent | MCP, in [Working with a coding agent](/guide/coding-agents/) | MCP |
| A data team with SQL tools | **SQL**: psql, JDBC, Metabase, DuckDB, dbt, Trino, Postgres itself | the Postgres wire protocol |
| A developer wanting typed access | **GraphQL**, with nested traversal both ways | GraphQL over HTTPS |
| In Excel, Power BI, Power Apps or Salesforce | **OData** | OData 4 over HTTPS |
| A Slack channel, a flow, or a service of your own | **Webhooks**: a saved view watched and its changes posted | HTTPS + JSON |

SQL and webhooks are part of the appliance: the server answers SQL itself, on by
default. The GraphQL and OData doors are sidecars: separate containers that run beside
the appliance and read it over its own API. Nothing is installed into the appliance for
them, and switching one off means not running it.

## What every door shows

Each door projects the same catalog. A **saved view** becomes a table (or a type, or an
entity set). An **entity label** with an extent — one a query may open bare — becomes
one too. A view with parameters becomes a table function, a query argument, or an
OData function, as the protocol allows. A column that carries another entity's key
becomes a join, a nested field, or a navigation property. And a column a **model made**
— a `classify` or `synthesize` in the view — is marked as a judgement, with its
provenance, in whatever way the protocol can carry a mark.

`embabel diagram` draws this catalog as an entity-relationship diagram in Mermaid,
re-derived from the world's declarations on every read.

## SQL

The SQL door speaks the Postgres wire protocol, so anything with a Postgres driver
connects to it as if it were a database named `world`. The appliance's server serves it,
on port 11052 of the first appliance on a machine (the next one's is 16 higher, like
every port in its block), bound to `127.0.0.1`. There is nothing to start:

```sh
psql -h 127.0.0.1 -p 11052 -U <your appliance user> -d world
```

The password is your appliance password, or an API key. Then:

```sql
\dt                                                -- every view and entity, as tables
select * from claim_triage limit 10;               -- a saved view
select * from claim c join claim_amount a using (claim_identifier);
\d+ claim_triage                                   -- a judgement column's provenance is its column comment
```

Clients this door has been verified with, each with a worked example in the
appliance's own documentation: psql, JDBC, **Metabase**, **DuckDB** (`ATTACH` the world
and query it alongside local files), **dbt** with DuckDB, **Trino**, SQLAlchemy, Kestra,
and **Postgres itself** through `postgres_fdw`, so a world becomes foreign tables in a
database you already run.

It is on unless you turn it off: `./setup.py --no-sql`, or `EMBABEL_SQL=false` in `.env`
and then `embabel up`. A server image older than the door has no SQL at all; `embabel up`
says so where the address would be, and `embabel upgrade` brings it.

Two limits to know. A statement that would return more than the row cap, or run
longer than the time budget, is refused with a message saying which limit and how to
narrow the query; a truncated table is a wrong answer, not a smaller one. And a
parametrised view is a table function: `select * from claims_since('2024-01-01')`.

## GraphQL

The GraphQL door generates a schema from the catalog: a type per view and entity, a
query field per table, and a nested field wherever a column carries another entity's
key, in both directions. It serves GraphiQL for exploring.

```sh
docker run --rm -p 15480:15480 --add-host=host.docker.internal:host-gateway \
  -e APPLIANCE_BASE=http://host.docker.internal:11043 \
  -e APPLIANCE_USER=<user> -e APPLIANCE_PASS=<password> \
  ghcr.io/embabel-worlds/world-graphql
```

The login given to the container reads the catalog, since a schema is shared by every
client of a world. Rows are read with each request's own HTTP Basic login.

```graphql
{
  claimAmount(limit: 20) {
    claimAmount
    claim { status }                 # the entity this column's key denotes
  }
  claim { claimIdentifier claimAmounts { claimAmount } }   # and back the other way
  _schema { version }               # pin it with the X-World-Schema-Version header
}
```

A nested field resolves in one batched read for the whole page, and is refused past a
fan-out cap rather than becoming a storm of reads. `GET /catalog/schema.graphqls`
returns the schema as text for code generation; a client that pins a schema version is
told, with a 409, when the world's schema has moved on.

## OData

The OData door is for tools that speak OData 4: Excel (Data → From OData Feed),
Power BI, Power Apps, Tableau, and Salesforce Connect, which maps entity sets to
external objects. No driver, no database port, plain HTTPS with Basic login.

```sh
docker run --rm -p 15490:15490 --add-host=host.docker.internal:host-gateway \
  -e APPLIANCE_BASE=http://host.docker.internal:11043 \
  -e APPLIANCE_USER=<user> -e APPLIANCE_PASS=<password> \
  ghcr.io/embabel-worlds/world-odata
```

Point the tool at `http://localhost:15490/odata`. It reads `$metadata`, lists every
entity set, and folds filters, sorts and column choices into the request — over an
entity label the filter is done by the world, not by the tool. Navigation properties
expand to the entity a column denotes, or to everything that carries an entity's key.
A view with parameters is a function: `/odata/ClaimsSince(since='2024-01-01')`. Pages
are server-driven, so a tool walks a set without being told anything.

## Webhooks

A **watch** runs a saved view on a schedule, keeps a snapshot, and delivers what
changed. With a webhook delivery, the change is posted to a URL: a Slack or Teams
incoming webhook, a Power Automate or Zapier trigger, or a service of your own. Nothing
polls the world but the appliance.

### Registering a receiver

You need your appliance login, the name of a saved view, and the URL to post to.

1. **Pick the view.** `GET /api/v1/admin/kg/views` lists them; the `name` is the
   watch's `lensId`. The view should project an identity — an entity's declared key —
   so a row can be seen to change rather than to vanish and reappear.

2. **Create the watch.**

   ```http
   POST /api/v1/watches
   Authorization: Basic <user:password>
   Content-Type: application/json

   {
     "lensId": "SupplierRisk",
     "name": "Supplier risk",
     "cron": "0 */15 * * * *",
     "delivery": {
       "channel": "webhook",
       "url": "https://hooks.slack.com/services/…",
       "format": "slack",
       "secret": "<a string you choose>"
     }
   }
   ```

   `format: slack` posts `{"text": …}`, a summary an incoming webhook shows as-is.
   `format: json` posts the whole diff — `added`, `removed` and `updated` arrays, each
   change with its key, before, after and the fields that differ — for a flow to
   branch on.

3. **Establish the baseline.** The first run takes a snapshot and delivers nothing; a
   baseline is not a change. Trigger it now rather than waiting for the cron:

   ```http
   POST /api/v1/watches/{watchId}/runs
   ```

   To see the view's current rows arrive as additions instead, create the watch with
   `"firstRunPolicy": "EMIT_CURRENT"`.

4. **Verify what arrives.** Every delivery carries `X-World-Watch` (the watch id) and
   `X-World-Delivery` (the diff id, the same on every attempt of one delivery). With a
   secret it also carries `X-World-Signature: sha256=<hex>`, an HMAC-SHA256 of the raw
   body. Check it before trusting the body:

   ```python
   import hashlib, hmac
   def genuine(secret: str, body: bytes, header: str) -> bool:
       expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
       return hmac.compare_digest(header, expected)
   ```

5. **When nothing arrives**, `GET /api/v1/watches/{watchId}/deliveries` lists every
   attempt with its status. A receiver that answered 5xx or did not answer was tried
   three times; one that refused with a 4xx was not asked again. The diff itself stays
   readable at `GET /api/v1/watches/{watchId}/diffs/{diffId}`.

A view that makes a judgement on every read will report the model's variation as
change. Materialise it with a TTL, and the watch diffs one snapshot against the next.

## Where the doors are

The SQL door is in the appliance's server image. The GraphQL and OData doors publish
container images at `ghcr.io/embabel-worlds/world-graphql` and `world-odata`, built
for amd64 and arm64. Their source is in the embabel-worlds organisation; the shared
piece — reading a world's catalog — is
[world-catalog](https://github.com/embabel-worlds/world-catalog).
