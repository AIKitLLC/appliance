# What stays on your machine

The short version: your data and your code stay on your installation. What leaves is
what you send to a model provider with your own key, whatever a realm you installed
goes and fetches, and a small anonymous usage report. This page is the long version,
because "we take privacy seriously" is not information.

## Where things actually live

Your graph, your documents, your memories, your realms, your credentials and your world
configuration are in a Docker volume on your machine. There is no Embabel server in the
path of any of it. If you unplug the network, everything except hosted models and
outbound realms keeps working.

Credentials you connect with — OAuth secrets, realm API keys, tokens that come back from
authorising — are held encrypted, under a key generated once into your `.env` and never
transmitted. That key is why they survive a restart, and losing it costs you the stored
credentials rather than the appliance.

## What leaves, exhaustively

**1. Model calls, to the provider you configured.** When work needs a hosted model, the
relevant text goes to OpenAI or Anthropic with your key, directly, under your account
and their terms. This is the big one, and it is the one you control most directly: see
[running your own models](local-models.md) for putting some or all of it on hardware
you own. Embeddings are already local by default.

**2. Whatever a realm reaches for.** A GitHub realm talks to GitHub; a Stripe realm
talks to Stripe. That is what you installed it to do. The thing to hold onto is that
realms are the *only* outbound integrations, they are individually installed, and you
can see exactly which ones are live. A realm you have not installed cannot reach
anything.

**3. A usage report, every 24 hours.** Counts and versions — how big the graph is, not
what is in it; how many documents exist, not their titles; how many realms are
installed, not their names. First report is ten minutes after startup, so a short
evaluation never reports at all.

That is the list.

## Checking rather than trusting

The usage report is the one thing that leaves without you asking, so it is documented to
the field in [PHONE_HOME.md](https://github.com/embabel-worlds/appliance/blob/main/PHONE_HOME.md),
and you can read the exact bytes from your own instance:

```bash
curl -u <you> http://localhost:11042/api/v1/phone-home           # what was last sent
curl -u <you> http://localhost:11042/api/v1/phone-home/preview   # what would be sent now
```

The `json` field is the literal request body, not a re-rendering of it — compare it with
a packet capture and it matches byte for byte.

**There is no flag to turn it off.** That is a deliberate choice, and stated rather than
buried: the payload is built so nothing sensitive *can* be in it, and a test fails the
build if a seeded distinctive name reaches it. If your environment forbids outbound
telemetry, block the endpoint at your network — knowingly, rather than discovering it
later.

## The two things people expect and do not get

**Nothing is mirrored.** Realms do not copy your systems into the graph. There is no
shadow copy of your Stripe account sitting on disk going stale, because there is no
copy at all.

**Nothing is shared between installations.** No fleet, no aggregation, no "anonymous
improvement of the model". Your world's contents are not training data for anyone,
including us.

## Sharing your machine

Two settings are worth knowing before you show someone your console.

The appliance publishes its ports on `127.0.0.1` — your machine only, not your network.
Changing that is a decision, not a default.

The Worlds console can be configured to skip its own sign-in, which is convenient on a
laptop you are alone with and means **anyone who can reach the console is you** — able
to chat, read memory and ingest documents as you. It ships empty, so the console asks.

## If you are evaluating this for an organisation

The questions worth asking, and the honest answers:

- *Where is our data?* On the machine running the appliance, in a Docker volume.
- *What crosses the boundary?* Model calls to your own provider account, the realms you
  installed, and a counts-only usage report.
- *Can we run with no outbound model calls at all?* Yes — local models for every job,
  and embeddings are already local. Quality becomes a function of your hardware.
- *Can we audit it?* The appliance is public, the usage payload is inspectable live from
  your own instance, and every agent run is inspectable after the fact.
- *What happens if Embabel disappears?* The graph is a SQLite file on your own disk (Neo4j,
  on an appliance installed before that was the default), the realms are your git
  repositories, and the images are already on your disk.
