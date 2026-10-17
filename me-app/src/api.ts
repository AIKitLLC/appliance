// The sensor's send side. Outbound-only, on purpose: the sensor is a CLIENT of
// the appliance's API and never listens on a port. Effect verbs, when they
// exist, will ride the same direction — the sensor long-polls an outbox — so
// the appliance never needs a route back to the host.
//
// Primary transport is the sensor envelope endpoint (`/api/v1/sensor/events`,
// typed event kinds, per-event receipts). Against an older image that lacks it,
// fact sending falls back to `/api/v1/memory/remember` — same destination
// (DICE propositions), coarser receipts.

import { diagnose } from './appliance'
import { HttpTransport, type Outcome, type RequestSpec } from '@embabel/appliance-kit'
import type { ConnectionResult, Fact, FocusSample, SendResult, Settings } from './types'

const SOURCE = 'me-app'

/**
 * The machine's IANA zone, sent on every batch envelope. The sensor is the
 * most authoritative source of where the user actually is — it keeps the
 * appliance's per-user zone current for users who never open the web UI,
 * and tracks travel. Older servers ignore the extra field.
 */
const timeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone

/** @param {Settings} settings */
const auth = (settings: Settings) =>
  'Basic ' + Buffer.from(`${settings.username}:${settings.password}`).toString('base64')

/** @param {unknown} e @returns {string} */
const errorMessage = (e: unknown) => {
  if (e instanceof Error) return (e.cause as Error | undefined)?.message ?? e.message
  return String(e)
}

/** @param {Settings} settings @param {string} path @param {unknown} body */
/**
 * Facts are slower than they look: each batch resolves entities and projects
 * graph edges server-side, so a first scan of a dozen long facts can outlast a
 * conversational timeout. Generous here, tight everywhere else.
 */
const FACTS_TIMEOUT_MS = 120_000

/** Parse a JSON body without letting a non-JSON one throw past the caller. */
async function readJson(res: Response): Promise<any> {
  try {
    return await res.json()
  } catch {
    return null
  }
}

async function post(settings: Settings, path: string, body: unknown, timeoutMs = 15000, method = 'POST') {
  return fetch(`${settings.baseUrl}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', Authorization: auth(settings) },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  })
}

/**
 * Cheap authenticated GET to prove the appliance is reachable and the credentials work.
 * @param {Settings} settings
 * @returns {Promise<ConnectionResult>}
 */
async function testConnection(settings: Settings) {
  let res
  try {
    res = await fetch(`${settings.baseUrl}/api/v1/hints`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(5000),
    })
  } catch (e) {
    // Unreachable is the interesting case: "connection refused" is true and
    // useless, whereas "Docker is not installed" is the actual next step.
    const why = await diagnose(settings.baseUrl)
    return { ok: false, message: why.message, action: why.action, url: why.url, state: why.state }
  }
  if (res.status === 401 || res.status === 403) {
    return { ok: false, message: 'Connected, but the username/password was rejected.' }
  }
  if (!res.ok) return { ok: false, message: `Appliance answered HTTP ${res.status}.` }
  return { ok: true, message: 'Connected and authenticated.' }
}

/**
 * Send facts through the sensor envelope; falls back to the remember endpoint on 404.
 * @param {Settings} settings
 * @param {Fact[]} facts
 * @returns {Promise<SendResult[]>}
 */
async function sendFacts(settings: Settings, facts: Fact[]) {
  let res
  try {
    res = await post(
      settings,
      '/api/v1/sensor/events',
      { source: SOURCE, timeZone: timeZone(), events: facts.map((f) => ({ kind: 'fact', label: f.label, text: f.text })) },
      FACTS_TIMEOUT_MS,
    )
  } catch (e) {
    const message = errorMessage(e)
    return facts.map((f) => ({ id: f.id, label: f.label, ok: false, message }))
  }
  if (res.status === 404) return sendFactsLegacy(settings, facts)
  if (!res.ok) {
    const message = `HTTP ${res.status}`
    return facts.map((f) => ({ id: f.id, label: f.label, ok: false, message }))
  }
  const body = await readJson(res)
  if (!body?.receipts) {
    return facts.map((f) => ({ id: f.id, label: f.label, ok: false, message: 'sent, but the reply was unreadable' }))
  }
  return facts.map((f, i) => {
    const receipt = body.receipts[i]
    const ok = receipt?.status === 'stored' || receipt?.status === 'duplicate'
    return { id: f.id, label: f.label, ok, message: receipt ? receipt.status + (receipt.detail ? ` (${receipt.detail})` : '') : 'no receipt' }
  })
}

/**
 * Older images: one fact at a time through the remember endpoint.
 * @param {Settings} settings
 * @param {Fact[]} facts
 * @returns {Promise<SendResult[]>}
 */
async function sendFactsLegacy(settings: Settings, facts: Fact[]) {
  const results = []
  for (const fact of facts) {
    try {
      const res = await post(settings, '/api/v1/memory/remember', { text: fact.text }, FACTS_TIMEOUT_MS)
      results.push({ id: fact.id, label: fact.label, ok: res.ok, message: res.ok ? 'stored' : `HTTP ${res.status}` })
    } catch (e) {
      results.push({ id: fact.id, label: fact.label, ok: false, message: errorMessage(e) })
    }
  }
  return results
}

/**
 * Push one focus observation (the ambient stream). Requires the sensor endpoint.
 *
 * WORKING is asserted only when the user is actually at the keyboard and the
 * screen is unlocked — an idle desktop showing IntelliJ is not someone working,
 * and claiming otherwise would corrupt the very interruption decisions this
 * stream exists to inform.
 *
 * @param {Settings} settings
 * @param {FocusSample} sample
 * @returns {Promise<ConnectionResult>}
 */
async function sendFocus(settings: Settings, sample: FocusSample) {
  const working = sample.active === true && sample.locked !== true
  let res
  try {
    res = await post(settings, '/api/v1/sensor/events', {
      source: SOURCE,
      timeZone: timeZone(),
      events: [
        {
          kind: 'context',
          stream: 'focus',
          activities: working ? ['WORKING'] : [],
          app: sample.app,
          detail: sample.detail,
          media: sample.media,
          focusMode: sample.focusMode,
          locked: sample.locked,
        },
      ],
    })
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
  if (res.status === 404) return { ok: false, message: 'This appliance version has no sensor endpoint — streaming needs a newer image.' }
  if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
  // Echo the server's own rendering of what it understood, so the UI shows what
  // was actually communicated rather than what we hoped to communicate.
  const body = await readJson(res)
  return { ok: true, message: body?.receipts?.[0]?.detail ?? (working ? 'active' : 'idle') }
}

/**
 * The documents the appliance already holds — uri + ingestedAt per document.
 * The indexer's reconcile anchor (embabel/appliance#10): server truth, so
 * there is no client ledger to drift.
 * @param {Settings} settings
 * @returns {Promise<{ok: boolean, message: string, documents: Array<{uri?: string, title?: string, ingestedAt?: string}>}>}
 */
async function listDocuments(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/documents`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, documents: [] }
    const body = await readJson(res)
    return { ok: true, message: '', documents: body?.documents ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), documents: [] }
  }
}

/**
 * Ingest one document by URL — for the Local files panel this is a file:// URL
 * under /local, which the server reads straight off its own mount (no bytes
 * shipped). Conversion of a big PDF is genuinely slow, hence the long timeout.
 * @param {Settings} settings
 * @param {string} url
 * @returns {Promise<ConnectionResult>}
 */
async function ingestDocumentUrl(settings: Settings, url: string) {
  let res
  try {
    res = await post(settings, '/api/v1/documents/url', { url }, 300_000)
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
  const body = await readJson(res)
  if (!res.ok || body?.status === 'error') {
    return { ok: false, message: body?.message ?? `HTTP ${res.status}` }
  }
  return { ok: true, message: body?.title ?? 'ingested' }
}

/**
 * Upload one document's bytes for ingestion, with tags. The multipart twin of
 * ingestDocumentUrl, for files that are NOT on a shared mount — an upload has
 * no directory the server could derive a tag from, so the tags the user gave
 * are the only scope it will ever have. Conversion is slow; be patient.
 * @param {Settings} settings
 * @param {string} filename
 * @param {ArrayBuffer | Uint8Array} bytes
 * @param {string[]} tags
 */
async function uploadDocument(settings: Settings, filename: string, bytes: ArrayBuffer | Uint8Array, tags: string[]) {
  const form = new FormData()
  form.append('file', new Blob([bytes]), filename)
  for (const tag of tags) form.append('tags', tag)
  let res
  try {
    // No explicit Content-Type: FormData carries its own multipart boundary.
    res = await fetch(`${settings.baseUrl}/api/v1/documents/upload`, {
      method: 'POST',
      headers: { Authorization: auth(settings) },
      body: form,
      signal: AbortSignal.timeout(300_000),
    })
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
  const body = await readJson(res)
  if (!res.ok || body?.status === 'error') {
    return { ok: false, message: body?.message ?? `HTTP ${res.status}` }
  }
  return { ok: true, message: body?.title ?? 'ingested', uri: body?.uri ?? null }
}

/*
 * Saved views — named Cypher bodies living in the user's world, optionally with
 * declared parameters. Four calls: list, save, delete, and "what would this view
 * run", which returns the invocation's Cypher so the studio can show (and let the
 * user edit) it before running through the ordinary execute path.
 */

/** @param {Settings} settings */
async function listViews(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/admin/kg/views`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (res.status === 404) return { ok: false, message: 'no views endpoint — older appliance', views: [] }
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, views: [] }
    return { ok: true, message: '', views: (await readJson(res)) ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), views: [] }
  }
}

/**
 * Save a query as a named view. The appliance validates before persisting — an
 * unrunnable body or a name a shipped view already owns comes back as a refusal
 * with the reason, never a silently broken view.
 * @param {Settings} settings
 * @param {{name: string, cypher: string, description?: string, params?: object}} spec
 */
async function saveView(settings: Settings, spec: { name?: string }) {
  try {
    const res = await post(settings, '/api/v1/admin/kg/views', spec, 60000)
    if (res.status === 404) return { ok: false, message: 'this appliance cannot save views — it needs a newer image' }
    const body = await readJson(res)
    if (!res.ok) {
      const violations = body?.violations?.length ? ` — ${body.violations.join('; ')}` : ''
      return { ok: false, message: (body?.error ?? `HTTP ${res.status}`) + violations }
    }
    return { ok: true, message: `saved “${body?.name ?? spec?.name}”` }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/** @param {Settings} settings @param {string} name */
async function deleteView(settings: Settings, name: string) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/admin/kg/views/${encodeURIComponent(name)}`, {
      method: 'DELETE',
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (res.status === 404) return { ok: false, message: `no saved view named ${name}` }
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    return { ok: true, message: `deleted ${name}` }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * The runnable Cypher for a view invoked with [args] — args merged over the
 * declared defaults, coerced and substituted server-side. A bad argument comes
 * back as an actionable message rather than a doomed run.
 * @param {Settings} settings @param {string} name @param {object} args
 */
async function viewInvocation(settings: Settings, name: string, args: object) {
  try {
    const res = await post(settings, `/api/v1/admin/kg/views/${encodeURIComponent(name)}/invocation`, { args }, 30000)
    const body = await readJson(res)
    if (res.status === 404) return { ok: false, message: `no such view ${name}` }
    if (!res.ok) return { ok: false, message: body?.error ?? `HTTP ${res.status}` }
    return { ok: true, cypher: body?.cypher ?? '' }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * The acting user's graph schema — labels with typed properties, relationship
 * triples, virtual labels included. The same snapshot the engine's preflight
 * validates against, which is what makes it fit to drive completion: what the
 * editor offers and what validation accepts cannot disagree.
 * @param {Settings} settings
 */
async function kgSchema(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/admin/kg/schema`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (res.status === 404) return { ok: false, message: 'no schema endpoint — older appliance', labels: [], relationships: [] }
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, labels: [], relationships: [] }
    const body = await readJson(res)
    return { ok: true, message: '', labels: body?.labels ?? [], relationships: body?.relationships ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), labels: [], relationships: [] }
  }
}

/**
 * The model that writes text-to-Cypher: the world's lensLlm override when set,
 * else the deployment default. Read for display, written by the studio's model
 * picker. LIVE — lensLlm is resolved per generation, no restart.
 * @param {Settings} settings
 */
async function lensModel(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/config/llms`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(20000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    const body = await readJson(res)
    return { ok: true, model: body?.lensLlm?.model ?? null }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/** Point text-to-Cypher at a model, or clear (blank) to inherit the default. */
async function setLensModel(settings: Settings, model: string) {
  try {
    const res = await post(settings, '/api/v1/config/llms/lensLlm', { model: model || null }, 20000, 'PUT')
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    return { ok: true, message: model ? `text-to-Cypher → ${model}` : 'text-to-Cypher inherits the default' }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Text-to-Cypher, generation ONLY — the appliance writes the query for a plain-
 * English question and explains what executing it will do, without running it.
 * The two-phase console pattern: generate → display (editable) → execute.
 * An LLM call, so patient; a local model can take half a minute.
 * @param {Settings} settings
 * @param {string} question
 */
async function kgGenerate(settings: Settings, question: string) {
  try {
    const res = await post(settings, '/api/v1/admin/kg/generate', { question }, 120_000)
    if (res.status === 404) return { ok: false, message: 'no generate endpoint — older appliance' }
    const body = await readJson(res)
    if (!res.ok) return { ok: false, message: body?.error ?? `HTTP ${res.status}` }
    return {
      ok: true,
      cypher: body?.cypher ?? '',
      explain: body?.explain ?? null,
      durationMs: body?.durationMs ?? null,
      // Server-validated before it was handed over; an older appliance sends
      // neither field and the caller falls back to validating client-side.
      valid: typeof body?.valid === 'boolean' ? body.valid : null,
      violations: body?.violations ?? [],
      attempts: body?.attempts ?? null,
    }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * ROUND-TRIP generation: the CURRENT (possibly hand-edited) cypher travels with
 * an instruction saying what to change; the revision returns with its verdict,
 * shaped exactly like generate.
 * @param {Settings} settings @param {string} cypher @param {string} instruction
 */
async function kgRefine(settings: Settings, cypher: string, instruction: string) {
  try {
    const res = await post(settings, '/api/v1/admin/kg/refine', { cypher, instruction }, 120_000)
    if (res.status === 404) return { ok: false, message: 'no refine endpoint — older appliance' }
    const body = await readJson(res)
    if (!res.ok) return { ok: false, message: body?.error ?? `HTTP ${res.status}` }
    return {
      ok: true,
      cypher: body?.cypher ?? '',
      explain: body?.explain ?? null,
      durationMs: body?.durationMs ?? null,
      valid: typeof body?.valid === 'boolean' ? body.valid : null,
      violations: body?.violations ?? [],
      attempts: body?.attempts ?? null,
    }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}


/**
 * One tip for the chat window — the appliance resolves the acting user's set (installation
 * tier plus whatever installed realms shipped in hints/). Null on any failure: a tip is
 * furniture, and furniture never throws.
 * @param {Settings} settings
 * @param {string[]} exclude recently shown hint ids
 */
async function hintRandom(settings: Settings, exclude: string[]) {
  try {
    const q = new URLSearchParams({ surface: 'me' })
    if (exclude.length) q.set('exclude', exclude.join(','))
    const res = await fetch(`${settings.baseUrl}/api/v1/hints/random?${q}`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(10000),
    })
    if (!res.ok) return null
    const text = await res.text()
    return text ? JSON.parse(text) : null
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Tours — guided walks the appliance holds as content: shipped with the world,
// contributed by installed realms, or imported by this user. The app fetches
// them and RUNS them; what a step means is the kit's business (see
// `@embabel/appliance-kit/tour`) and what a step's precondition answers is the
// appliance's. Nothing here interprets a step.
// ---------------------------------------------------------------------------

/**
 * Every tour this world offers. Empty on any failure — a tour panel that cannot
 * reach the appliance shows nothing, which the panel itself explains.
 * @param {Settings} settings
 */
async function toursList(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/tours`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) return []
    const body = await readJson(res)
    return Array.isArray(body?.tours) ? body.tours : []
  } catch {
    return []
  }
}

/**
 * Is step [index] already satisfied? Asked when the step is REACHED, never at
 * load: a tour changes the world as it walks through it.
 *
 * UNKNOWN on any failure, which the runner treats as "run it". The direction is
 * deliberate — repeating a step is visible and recoverable, silently skipping
 * one means the user never learns what they missed.
 * @param {Settings} settings @param {string} id @param {number} index
 * @param {object} params what the tour has collected so far
 */
async function tourStepStatus(settings: Settings, id: string, index: number, params: object) {
  try {
    const path = `/api/v1/tours/${encodeURIComponent(id)}/steps/${index}/status`
    // A POST for a read: the parameters the tour has collected are the body, and they are what let
    // a precondition ask about the thing the user just named.
    const res = await post(settings, path, { params: params ?? {} }, 30000)
    if (!res.ok) return { status: 'UNKNOWN', detail: `HTTP ${res.status}` }
    return (await readJson(res)) ?? { status: 'UNKNOWN', detail: 'no answer' }
  } catch (e) {
    return { status: 'UNKNOWN', detail: errorMessage(e) }
  }
}

/** One tour as the file it would be — what the user hands to somebody else. */
async function tourExport(settings: Settings, id: string) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/tours/${encodeURIComponent(id)}/export`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    return { ok: true, yaml: await res.text() }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Fetch an image a tour's narration names, and hand it back as a data URL.
 *
 * WHY THIS EXISTS AT ALL. The console can write `<img src="/apps/world/x.svg">` and be done: it is
 * served by the appliance, so the browser sends the session with the request. This app's windows
 * load over `file://` and authenticate with a header, and an `<img>` tag cannot carry one — the
 * same asset would come back 401 and render as a broken icon. So the fetch happens here, where the
 * credentials are, and the renderer gets bytes it can display.
 *
 * ONLY `/apps/`, and only a rooted path. A tour is a file people exchange; the point of serving
 * its images ourselves is that naming one cannot become a request to somebody else's host. This
 * function is the last place that could be subverted into making that request, so it refuses
 * anything that is not a path into the appliance's own app store — the kit's `resolveTourImages`
 * has already dropped the rest, and this does not take its word for it.
 */
async function tourAsset(settings: Settings, path: string) {
  if (!path.startsWith('/apps/') || path.startsWith('//')) return { ok: false, message: 'not an app asset' }
  try {
    const res = await fetch(`${settings.baseUrl}${path}`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    const type = res.headers.get('content-type') ?? 'application/octet-stream'
    if (!type.startsWith('image/')) return { ok: false, message: `not an image (${type})` }
    const bytes = Buffer.from(await res.arrayBuffer())
    return { ok: true, dataUrl: `data:${type};base64,${bytes.toString('base64')}` }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Store a tour file — one somebody exported, or one just recorded here.
 *
 * A 409 is the interesting case and gets the server's own sentence: a realm
 * already ships a tour of that name, so this write would never load.
 */
async function tourImport(settings: Settings, yaml: string) {
  try {
    const res = await post(settings, '/api/v1/tours/import', { yaml }, 30000)
    if (res.status === 404) return { ok: false, message: 'this appliance cannot store tours — it needs a newer image' }
    const body = await readJson(res)
    if (!res.ok) return { ok: false, message: body?.message ?? body?.error ?? `HTTP ${res.status}` }
    return { ok: true, tours: body?.tours ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/** Delete a tour this user saved. A realm's is not theirs to remove; the server says so. */
async function tourDelete(settings: Settings, id: string) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/tours/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    const body = await readJson(res)
    return { ok: true, deleted: body?.deleted === true }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * The engine's strict preflight WITHOUT execution — the editor's validator.
 * @param {Settings} settings
 * @param {string} cypher
 */
async function kgValidate(settings: Settings, cypher: string) {
  try {
    const res = await post(settings, '/api/v1/admin/kg/validate', { cypher }, 30000)
    if (res.status === 404) return { ok: false, message: 'no validate endpoint — older appliance' }
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    const body = await readJson(res)
    return { ok: true, valid: body?.ok === true, violations: body?.violations ?? [], detail: body?.message ?? null }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Run virtual Cypher verbatim through the appliance's engine — the advanced
 * documents surface. Same executor the appliance's own ask path uses: per-user
 * scope rewrite, preflight, live producer joins. An admin endpoint, which the
 * appliance operator account holds (the compose file's ORG_ROLE_FALLBACK).
 * Relevance joins fetch live and can take a while — be patient.
 * @param {Settings} settings
 * @param {string} cypher
 */
async function kgExecute(settings: Settings, cypher: string) {
  let res
  try {
    res = await post(settings, '/api/v1/admin/kg/execute', { cypher }, 180_000)
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
  if (res.status === 404) {
    return { ok: false, message: 'This appliance has no virtual-Cypher endpoint — it needs a newer image.' }
  }
  if (res.status === 401 || res.status === 403) {
    return { ok: false, message: 'The appliance refused the admin query surface for these credentials.' }
  }
  const body = await readJson(res)
  if (!res.ok) return { ok: false, message: body?.error ?? `HTTP ${res.status}` }
  return {
    ok: true,
    rows: body?.rows ?? [],
    rowCount: body?.rowCount ?? (body?.rows?.length ?? 0),
    durationMs: body?.durationMs ?? null,
    warnings: body?.warnings ?? [],
    // The engine's own failure/emptiness explanations — a 200 can still carry them.
    error: body?.error ?? null,
    hint: body?.hint ?? null,
  }
}

/*
 * THE KIT'S CLIENTS, sent from here. The Agents window and the Routine Studio are the kit's own
 * surfaces: their clients shape each request and read each answer, and this only sends it, with the
 * kit's `HttpTransport`, from the one process here that has a network. A page never names a URL, so
 * what it may reach is fixed here, per method, whatever a compromised page might ask for.
 */
const KIT_ROUTES: { method: RequestSpec['method']; path: RegExp }[] = [
  // A dot segment is refused as a name: `agents/..` would resolve to somewhere that is not agents.
  { method: 'GET', path: /^\/api\/v1\/agents(\/(?!\.\.?(?:\/|$))[^/?#]+(\/versions)?)?$/ },
  { method: 'POST', path: /^\/api\/v1\/agents\/(?!\.\.?(?:\/|$))[^/?#]+\/(stage|sign)$/ },
  // A duty's test run, from its card: both names are single segments, neither a dot segment.
  { method: 'POST', path: /^\/api\/v1\/agents\/(?!\.\.?(?:\/|$))[^/?#]+\/duties\/(?!\.\.?(?:\/|$))[^/?#]+\/check$/ },
  // Approvals: the requests agents raise, one of them, and the decision on one.
  { method: 'GET', path: /^\/api\/v1\/requests(\/(?!\.\.?(?:\/|$))[^/?#]+)?$/ },
  { method: 'POST', path: /^\/api\/v1\/requests\/(?!\.\.?(?:\/|$))[^/?#]+\/decision$/ },
  // The kill switch: read, thrown with a reason, lifted. Its own path, not under agents.
  { method: 'GET', path: /^\/api\/v1\/halt$/ },
  { method: 'POST', path: /^\/api\/v1\/halt$/ },
  { method: 'DELETE', path: /^\/api\/v1\/halt$/ },
  { method: 'POST', path: /^\/api\/v1\/admin\/handlers\/(list|open|validate|generate|dry-run|save|delete|set-enabled)$/ },
  { method: 'GET', path: /^\/api\/v1\/admin\/kg\/schema$/ },
  /*
   * Virtual Cypher, for what a world holds about itself: its realms, the APIs it skipped, the signal
   * types it has seen, its skills, which agent holds a routine. Each is a row the kit's RealmCatalog
   * and WorldLists query, so these lists answer from the same rows chat and Query Studio do.
   */
  { method: 'POST', path: /^\/api\/v1\/admin\/kg\/execute$/ },
  /*
   * Sweeps (fills): the slow materializations the appliance drives over hours or days. Listed,
   * started, and stopped — a stop is a DELETE of one, so the id is a single segment and never a
   * dot segment, the same guard the agent routes above use for the same reason.
   */
  { method: 'GET', path: /^\/api\/v1\/admin\/kg\/fills$/ },
  { method: 'POST', path: /^\/api\/v1\/admin\/kg\/fills$/ },
  { method: 'DELETE', path: /^\/api\/v1\/admin\/kg\/fills\/(?!\.\.?(?:\/|$))[^/?#]+$/ },
  { method: 'POST', path: /^\/api\/v1\/cron\/compile-schedule$/ },
]

async function kitSend(settings: Settings, spec: RequestSpec): Promise<Outcome<unknown>> {
  const allowed = KIT_ROUTES.some((r) => r.method === spec?.method && r.path.test(spec?.path ?? ''))
  if (!allowed) return { ok: false, kind: 'refused', message: `Not a request this app sends: ${spec?.method} ${spec?.path}` }
  const transport = new HttpTransport({
    baseUrl: settings.baseUrl,
    headers: () => ({ Authorization: auth(settings) }),
  })
  // The deadline travels too: compiling a schedule is a model call and gets longer than a read.
  return transport.send({ method: spec.method, path: spec.path, query: spec.query, body: spec.body, timeoutMs: spec.timeoutMs })
}

/**
 * The user's typed gateway surface (interfaces.ts) — generated from the same
 * pass that types code-mode, so completion and the compiler cannot drift.
 */
async function gatewaySurface(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/apps-runtime/interfaces.ts`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, text: '' }
    return { ok: true, text: await res.text() }
  } catch (e) {
    return { ok: false, message: errorMessage(e), text: '' }
  }
}

/*
 * Realms — the units of capability a world installs. What is installed, what is on offer and which
 * APIs sit idle are not here: they are rows the page queries in Virtual Cypher (see KIT_ROUTES).
 * What stays is what ACTS — install, which appends the realm to the world's realms.yml and rebuilds
 * it, and the update calls, which reach each realm's remote.
 */

/**
 * The MCP surface: what a connected coding agent may do, and whether the endpoint is live.
 *
 * `mode` is ASSISTANT or DEVELOPER — see McpMode on the server. `requiresReconnect` comes from
 * the appliance rather than being asserted here, because it is the server's fact: the MCP tool
 * list is fixed when a session starts, so a switch reaches an already-connected agent only
 * after it reconnects.
 */
async function mcpMode(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/config/mcp-mode`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(10000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, mode: null as string | null, modes: [] as string[] }
    const body = await readJson(res)
    return {
      ok: true, message: '',
      mode: body?.mode ?? 'ASSISTANT',
      modes: body?.modes ?? ['ASSISTANT', 'DEVELOPER'],
      requiresReconnect: body?.requiresReconnect !== false,
    }
  } catch (e) {
    return { ok: false, message: errorMessage(e), mode: null as string | null, modes: [] as string[] }
  }
}

/** @param {Settings} settings @param {string} mode */
async function setMcpMode(settings: Settings, mode: string) {
  try {
    const res = await post(settings, '/api/v1/config/mcp-mode', { mode }, 15000, 'PUT')
    const body = await readJson(res)
    if (!res.ok) return { ok: false, message: body?.message ?? `HTTP ${res.status}` }
    return { ok: true, message: `switched to ${(body?.mode ?? mode).toLowerCase()} mode` }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Is the MCP endpoint actually there, and is it guarded?
 *
 * Deliberately UNAUTHENTICATED, which reads backwards until you see what it proves: a 401 is
 * the good answer. It says something is serving /mcp and refusing anonymous callers — the
 * console verifies the same way. A 200 would mean the endpoint is open to anyone who can
 * reach the port, which is worth showing differently.
 * @param {Settings} settings
 */
async function mcpProbe(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/mcp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
      signal: AbortSignal.timeout(8000),
    })
    if (res.status === 401 || res.status === 403) return { ok: true, state: 'guarded', message: 'live, bearer auth enforced' }
    if (res.status === 404) return { ok: false, state: 'missing', message: 'nothing served at /mcp' }
    if (res.ok) return { ok: true, state: 'open', message: 'live, but answering unauthenticated callers' }
    return { ok: true, state: 'up', message: `live (HTTP ${res.status})` }
  } catch (e) {
    return { ok: false, state: 'down', message: errorMessage(e) }
  }
}

/**
 * Install a realm by repository.
 *
 * A 409 is one of two answers and they must not be confused. A realm whose author calls it
 * experimental is not refused but ASKED about: `needs-confirmation`, with the appliance's own
 * warning, which the page puts to the person and retries with `confirmed` only on a yes. Any other
 * 409 is a realm already there.
 * @param {Settings} settings @param {string} repo clone URL or owner/repo @param {boolean} confirmed
 */
async function installRealm(settings: Settings, repo: string, confirmed = false) {
  try {
    const res = await post(settings, '/api/v1/realms', confirmed ? { repo, confirmed: true } : { repo }, 120_000)
    const body = await readJson(res)
    if (res.status === 409 && body?.status === 'needs-confirmation') {
      return { ok: false, needsConfirmation: true, message: body?.message ?? `${body?.name ?? 'This realm'} is experimental.` }
    }
    if (res.status === 409) return { ok: false, message: 'already installed' }
    if (!res.ok) return { ok: false, message: body?.detail ?? body?.message ?? `HTTP ${res.status}` }
    return { ok: true, message: body?.name ? `installed ${body.name}` : 'installed' }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * WHICH REALMS HAVE SOMETHING TO PULL, without pulling any of them.
 *
 * `ls-remote` server-side: refs only, no objects, so asking for every realm costs one small round
 * trip each. An Update button on every realm is a button whose usual answer is "nothing happened",
 * and people stop pressing it.
 *
 * Silent on an older appliance: no endpoint means no answer, and the caller then keeps offering
 * Update on everything exactly as before.
 * @param {Settings} settings
 */
async function realmUpdates(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/realms/updates`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(120_000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, results: [] }
    const body = await readJson(res)
    return { ok: true, message: '', results: body?.results ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), results: [] }
  }
}

/**
 * Pull a realm's checkout and rebuild the world around it. Two things the UI
 * has to be honest about, both of which come back in the server's summary:
 * a realm referenced by local path has no checkout to pull and is already
 * live, and a git-backed checkout is shared between every world on that ref,
 * so this moves more than the current world.
 * @param {Settings} settings @param {string} name installed realm name
 */
async function updateRealm(settings: Settings, name: string) {
  try {
    const res = await post(settings, `/api/v1/realms/${encodeURIComponent(name)}/update`, {}, 120_000)
    const body = await readJson(res)
    if (res.status === 404) return { ok: false, message: `no realm named ${name}` }
    if (!res.ok) return { ok: false, message: body?.message ?? body?.detail ?? `HTTP ${res.status}` }
    return { ok: true, message: body?.summary ?? 'updated' }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Pull every git-backed realm at once. Each entry comes back with its own
 * status — `updated`, `local` (a path reference, already live) or `error` —
 * because a partial success is the normal case: one unreachable remote must
 * not read as "nothing updated", nor be hidden behind an overall "ok".
 * @param {Settings} settings
 */
async function updateAllRealms(settings: Settings) {
  try {
    const res = await post(settings, '/api/v1/realms/update-all', {}, 180_000)
    const body = await readJson(res)
    if (!res.ok) return { ok: false, message: body?.message ?? `HTTP ${res.status}`, results: [] as any[] }
    return { ok: true, message: '', results: body?.results ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), results: [] }
  }
}

/**
 * The world's HTML apps — user vibe-coded, world-template and realm-shipped,
 * unioned server-side, each with its canonical scoped `url` (/apps/{scope}/{name}), so this
 * list and what a window opens can never disagree. readOnly=false marks the
 * user's own apps.
 * @param {Settings} settings
 */
async function listApps(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/artifacts/app`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, apps: [] }
    return { ok: true, message: '', apps: (await readJson(res)) ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), apps: [] }
  }
}

/**
 * An icon — a realm's or an app's — as a `data:` URI the renderer can put in a
 * `src`. The bytes have to come through here because the renderer has no HTTP
 * and the credential lives in this process; a plain URL in an `<img>` would
 * simply 401.
 *
 * `path` is a URL the APPLIANCE handed us (`iconUrl` from its own listings), so
 * it is checked to be a relative path on that appliance before it is used —
 * `//tracker.example/x.gif` in a listing must not become a fetch to a stranger.
 *
 * A missing or broken icon is not an error worth surfacing: the caller draws
 * its letter tile, exactly as it did before icons existed.
 * @param {Settings} settings @param {string} path
 */
async function icon(settings: Settings, path: string) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
    return { ok: false, message: 'not an appliance path', dataUri: null }
  }
  try {
    const res = await fetch(`${settings.baseUrl}${path}`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(10000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, dataUri: null }
    const type = res.headers.get('content-type') ?? 'application/octet-stream'
    // Images only. The endpoints promise nothing else, and a tile is the one
    // place a surprise content type would be rendered rather than inspected.
    if (!type.startsWith('image/')) return { ok: false, message: `not an image (${type})`, dataUri: null }
    const bytes = Buffer.from(await res.arrayBuffer())
    // A tile, not a wallpaper: anything this size is a mistake or an attack on
    // the settings file, and either way it is not going in an <img>.
    if (bytes.length > 512_000) return { ok: false, message: 'icon too large', dataUri: null }
    return { ok: true, message: '', dataUri: `data:${type};base64,${bytes.toString('base64')}` }
  } catch (e) {
    return { ok: false, message: errorMessage(e), dataUri: null }
  }
}

export {
  listThemes,
  themeCss,
  loadedLocalModels,
  modelsInUse,
  getWorldConfig,
  setChatModel,
  chatModel,
  listModels,
  getRoles,
  setRole, testConnection, sendFacts, sendFocus, listDocuments, ingestDocumentUrl, kgExecute, realmUpdates,
  kgSchema, kgValidate, kgGenerate, kgRefine, lensModel, setLensModel,
  listViews, saveView, deleteView, viewInvocation, hintRandom,
  toursList, tourStepStatus, tourExport, tourImport, tourDelete, tourAsset,
  kitSend, gatewaySurface,
  uploadDocument, installRealm, updateRealm, updateAllRealms, listApps, icon, mcpMode, setMcpMode, mcpProbe }

// ---------------------------------------------------------------------------
// Models. Everything here is the appliance's own REST surface — the app adds a
// picker, not a mechanism.
// ---------------------------------------------------------------------------

/**
 * Every model the appliance can use, with its provider. Local ones (LM Studio,
 * Ollama) are the user's own hardware: free to run, and nothing leaves the
 * machine.
 * @param {Settings} settings
 */
async function listModels(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/config/models`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, models: [], default: '' }
    const body = await readJson(res)
    return {
      ok: true,
      message: '',
      models: body?.modelsWithProvider ?? [],
      default: body?.default ?? '',
    }
  } catch (e) {
    return { ok: false, message: errorMessage(e), models: [], default: '' }
  }
}

/** The role→model overrides this world has set. Empty means "inherit the defaults". */
async function getRoles(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/config/llm-roles`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, roles: {} }
    return { ok: true, message: '', roles: (await readJson(res)) ?? {} }
  } catch (e) {
    return { ok: false, message: errorMessage(e), roles: {} }
  }
}

/**
 * Point one role at a model, or clear it with an empty model to fall back to the
 * appliance default. Takes effect immediately: the world re-reads its config on
 * every access, so the next piece of work uses it — no restart.
 */
async function setRole(settings: Settings, roleId: string, model: string) {
  try {
    const res = await post(settings, `/api/v1/config/llm-roles/${encodeURIComponent(roleId)}`, { model }, 15000, 'PUT')
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    return { ok: true, message: model ? `now ${model}` : 'cleared' }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/** The world's own config — needed to know HOW chat is currently pinned. */
/**
 * The appliance's installed themes. Same service the Vaadin UI's Theme tab uses
 * (`GET /api/v1/themes`), so a theme chosen anywhere is the same list here.
 */
async function listThemes(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/themes`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, themes: [] }
    return { ok: true, message: '', themes: (await readJson(res)) ?? [] }
  } catch (e) {
    return { ok: false, message: errorMessage(e), themes: [] }
  }
}

/**
 * One theme's stylesheet. TEXT, not JSON — this endpoint serves `text/css`, and
 * the renderer cannot fetch it itself (no HTTP in the renderer, by design), so
 * it travels over IPC and is injected into `<style id="theme">`.
 */
async function themeCss(settings: Settings, name: string) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/themes/${encodeURIComponent(name)}.css`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, css: '' }
    return { ok: true, message: '', css: await res.text() }
  } catch (e) {
    return { ok: false, message: errorMessage(e), css: '' }
  }
}

async function getWorldConfig(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/config`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}`, config: {} }
    return { ok: true, message: '', config: (await readJson(res)) ?? {} }
  } catch (e) {
    return { ok: false, message: errorMessage(e), config: {} }
  }
}

/**
 * Point CHAT at a model — the setting people mean by "change the model".
 *
 * One call, because the appliance owns the rule that makes this fiddly: a chat
 * config pinned to a ROLE ignores any model written beside it, so the server
 * clears the role as it sets the model. The app deliberately does not
 * re-implement that precedence; it would drift.
 */
async function setChatModel(settings: Settings, model: string) {
  try {
    const res = await post(settings, '/api/v1/config/chat-model', { model }, 15000, 'PUT')
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    const body = await readJson(res)
    return { ok: true, message: body?.inherited ? 'chat follows the default again' : `chat → ${body?.model}` }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/** What chat is pinned to right now: an explicit model, a role, or inherited. */
async function chatModel(settings: Settings) {
  const [world, roles] = await Promise.all([getWorldConfig(settings), getRoles(settings)])
  const chat = world.config?.chat ?? {}
  if (chat.model) return { model: chat.model, via: 'pinned' }
  if (chat.role) return { model: roles.roles?.[chat.role]?.model ?? '', via: `role ${chat.role}`, role: chat.role }
  return { model: '', via: 'the default' }
}

/**
 * Which models are actually in use, and whether any is hosted. The appliance
 * resolves this — role-versus-model precedence lives there, and a banner making
 * a privacy claim must not be computed from a second, drifting copy of the rules.
 */
async function modelsInUse(settings: Settings) {
  try {
    const res = await fetch(`${settings.baseUrl}/api/v1/models/in-use`, {
      headers: { Authorization: auth(settings) },
      signal: AbortSignal.timeout(20000),
    })
    if (!res.ok) return { ok: false, message: `HTTP ${res.status}` }
    const body = await readJson(res)
    return { ok: true, ...body }
  } catch (e) {
    return { ok: false, message: errorMessage(e) }
  }
}

/**
 * Which local models are LOADED, from LM Studio's own view.
 *
 * A model being listed does not mean it can answer: LM Studio advertises every
 * model in the library, but one that is not loaded replies "Model has not
 * started loading" to any request. Sixteen of seventeen entries in a picker
 * being duds is not a menu, it is a trap — so the app asks the question the
 * appliance cannot (it is on the host side of the boundary, and this endpoint
 * is LM Studio's own, not the OpenAI-compatible one).
 *
 * Returns a Map of model id → true when loaded. Empty when LM Studio is absent
 * or the endpoint is unknown to it, in which case the UI simply says nothing.
 */
async function loadedLocalModels() {
  for (const base of ['http://127.0.0.1:1234', 'http://localhost:1234']) {
    try {
      const res = await fetch(`${base}/api/v0/models`, { signal: AbortSignal.timeout(2500) })
      if (!res.ok) continue
      const body = await readJson(res)
      const loaded = new Map()
      for (const m of body?.data ?? []) {
        if (m?.id) loaded.set(m.id, m.state === 'loaded')
      }
      return loaded
    } catch {
      /* try the next spelling, then give up quietly */
    }
  }
  return new Map()
}
