---
name: appliance-doctor
description: Diagnose an Embabel appliance that will not install, will not start, or will not answer — a failed `curl | sh`, containers that restart, a console stuck on "Connecting to Worlds", a setup wizard that stops with an error. Use when the appliance itself is the problem, and especially when its MCP tools are unavailable, because none of this needs them.
---

The appliance is down, or never came up. **Assume none of its MCP tools work** — if they did,
this would not be the problem. Everything here runs on the user's own machine with `docker`,
`curl` and a shell.

## Who you are doing this for

Usually somebody who did not sign up to become a Docker expert. That changes the job:

- **Run the commands yourself** where you can, rather than handing over a list to type.
- **Never paste a raw log at them.** Read it, and say the one sentence it means.
- **Give one action at a time**, and say what it will do before you do it.
- **Never print or ask for a password, an API key, or a token.** They are in the files below;
  you never need their contents, only whether they exist.
- Say plainly when you do not know. A confident wrong diagnosis costs more than a shrug —
  four separate wrong turns on this appliance came from reading a message as evidence about a
  component it never spoke for.

## Start here, in this order

1. **`sh doctor.sh`** from the appliance directory, or, if there is no checkout at all:
   `curl -fsSL https://raw.githubusercontent.com/embabel-worlds/appliance/main/doctor.sh | sh`
   It needs nothing installed, changes nothing, and ends with a numbered list of what to fix.
2. **`embabel doctor`** if the `embabel` command exists — it knows more, including the
   credential-helper trap and the embedding model.
3. Only then start reading logs.

**Find out which appliance first.** A machine can run more than one; `embabel instances`
lists them. The commands below name containers as `$P-<service>-1`, where `$P` is the
appliance's compose project — set it before running any of them:

```bash
P=embabel-appliance        # the default; another instance is embabel-<name>
```

Run compose as `embabel --instance <name> compose …`, never bare `docker compose`: the bare
form acts on the default appliance, which may be somebody's working one, and starts
containers without the memory limits that keep them from filling Docker's memory. Prefer a
dedicated `embabel --instance <name>` verb when there is one, and run
`EMBABEL_INSTANCE=<name> sh doctor.sh` for the script.

The same material written for the user directly is at
<https://github.com/embabel-worlds/appliance/blob/main/docs/guide/troubleshooting.md>, and
in `docs/guide/troubleshooting.md` if there is a checkout. Send them the link; do not make
them read it to you.

**If you are reading this from a URL rather than a checkout, that is the expected case** —
the install that would have produced the checkout is the thing that failed. Nothing here
needs one. To keep the skill for next time:

```bash
mkdir -p ~/.claude/skills/appliance-doctor && curl -fsSL \
  https://raw.githubusercontent.com/embabel-worlds/appliance/main/skills/appliance-doctor/SKILL.md \
  -o ~/.claude/skills/appliance-doctor/SKILL.md
```

## The two logs — the single most useful fact here

`docker logs` is **filtered**. Under the appliance profile it carries operator lines and
warnings only, and is two to six times smaller than the real log. Evidence people need is
routinely in a file they do not know exists:

```bash
docker logs --tail 80 $P-worlds-1              # the filtered view
docker exec $P-worlds-1 tail -100 logs/assistant.log   # everything
docker cp $P-worlds-1:/app/logs/assistant.log ./       # works on a STOPPED container
```

`/app/logs` is not on a volume: it survives a restart, not a recreate. If a container is
crash-looping, copy the log out before anyone runs `docker compose up` again.

## Symptom → the command that decides

| What they see | Run this |
|---|---|
| `error getting credentials … docker-credential-*` | `ls "$(dirname "${DOCKER_CONFIG:-$HOME/.docker}")"` — it is PATH, not the network. See below. |
| Install stopped, no `embabel` command | `sh doctor.sh` — the container may never have started |
| A container "is running" but nothing works | `docker inspect -f '{{.RestartCount}}' <name>` — a climbing count is a crash loop |
| Console stuck on "Connecting to Worlds" | `docker logs --tail 30 $P-worlds-console-1` — read the status of the repeating `/api/v1/realms` lines |
| Wizard: "did not answer … still waiting for OpenAI" | `docker exec $P-worlds-1 sh -c 'curl -s -o /dev/null -w "%{http_code}\n" --max-time 20 https://api.openai.com/v1/models'` |
| `Bearer token resolved to user 'x' but the user could not be loaded` | Compare the token's user against the credential store — see below |
| Neo4j errors in the log (a Neo4j appliance; `embabel version` says which engine) | `docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' $P-neo4j-1` |

## The failures that have actually happened, and what each one really is

**`error getting credentials - docker-credential-desktop: executable file not found`.**
Nothing was downloaded and no registry was contacted. Docker's config names a credential
helper that is not on PATH, and the CLI runs it for *every* registry — including anonymous
pulls of public images, which all of Embabel's are. Fix: put Docker Desktop's bin directory on
PATH (`/Applications/Docker.app/Contents/Resources/bin`), or remove the `credsStore` line from
the config. Not a network problem, not a permissions problem.

**A memory-starved appliance looks like a network problem.** On a Neo4j appliance, Neo4j at its default 2 GB heap in
a container smaller than that refuses to start — `Invalid memory configuration - exceeds
physical memory` — and crash-loops. **`OOMKilled` stays `false`**, so do not read that flag as
the verdict; `RestartCount` is the tell. `NEO4J_HEAP=1G` in `.env` is a tested fix. The
appliance's own JVM starved of memory restarts repeatedly and takes the console's `/api` calls
to 502 with it, which reads as a networking fault and is not one.

**A repeated 502 from the console.** Either the worlds container was replaced and the console
is still dialling its old address — `docker restart $P-worlds-console-1` fixes
that outright — or the two disagree about the port. Compare:

```bash
docker exec $P-worlds-console-1 sh -c 'grep -m1 -o "proxy_pass http://worlds:[0-9]*" /etc/nginx/conf.d/default.conf'
docker exec $P-worlds-1 sh -c 'echo $SERVER_PORT'
```

**`Bearer token resolved to user 'x' but the user could not be loaded`.** An MCP client
retrying with a token that names a user the appliance cannot load — usually left over from an
earlier install attempt, because the data volume survives a plain reinstall. A symptom, not a
cause. Compare the two names:

```bash
docker exec $P-worlds-1 sh -c '
grep -o "EMBABEL_SETUP_MCP_TOKEN_USER=.*" /data/embabel/assistant/admin/providers.env
sed -nE "/^credentials:/,/^[a-zA-Z]/p" /data/embabel/assistant/admin/.credentials.yml | sed -E "s/^( +)([A-Za-z0-9_.-]+): .*/\1\2/"'
```

That prints usernames only, never hashes. Keep it that way.

## Things that look like evidence and are not

Each of these is a true observation that does not support the conclusion it appears to. They
have each cost hours.

- **A fast `401`** proves the HTTP surface is up and nothing more. Security rejects before
  anything touches the graph, so `401 in 4ms` is entirely consistent with an unreachable
  database.
- **`curl localhost:11043` working** says the *host* can reach the container. It says nothing
  about whether the console container can, and that is the hop that strands people.
- **A clean `docker logs`** is not a clean log.
- **A port that looks wrong** usually is not: `7687` inside the Docker network and `11046` on
  the machine are both correct from their own side.
- **"It broke right after I entered the API key"** rarely means the key is bad. The key causes
  a restart, a validation call, and model registration — three new things to fail, none of
  them about whether the key is valid.

## Before you conclude

Say which of these you did, and what each showed. If the evidence does not support a
diagnosis, say that instead of picking the most likely story — and ask for the one artefact
that would settle it. The right closing move is often:

```bash
embabel bugreport        # one folder, no secrets, ready to attach to an issue
```
