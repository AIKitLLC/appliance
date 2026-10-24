// Backup and restore — everything the appliance knows, saved to the host's
// own disk and brought back from it.
//
// WHAT A BACKUP IS. The appliance keeps everything a user would cry about in
// named volumes — embabel_assistant_data (worlds, documents, artifacts,
// credentials, and on the Embabel engine the graph too) and, on an engine with a
// graph container, that container's volume — plus the host-side files that make the checkout THIS appliance: .env, secrets.env
// and the mounts override. A backup is a plain folder holding a cold tarball
// of each volume, those files, and a manifest saying what made it.
//
// THE WORK ITSELF IS NOT HERE. It is `embabel backup` / `embabel restore`,
// which is setup.py's back_up() and restore() — the stop, the cold copy
// through a helper container, the config set-aside, the restart. That is where
// the appliance's other lifecycle verbs live, and two implementations of "copy
// the volumes safely" is how they come to disagree about which files matter.
// This module is the menu's end of that call: locate the checkout, run the
// verb with --json, hand back what it said.
//
// inspect() stays local, and only looks like an exception. It reads a manifest
// off disk to name a date in the confirmation dialog — no docker, no volumes,
// nothing that could be got wrong twice — and it is synchronous, which is what
// lets the dialog be built without a round trip.
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import * as mounts from './mounts'

/* The volume tarballs a backup from before the manifest named them holds — Neo4j's,
 * the only engine anybody backed up then. A newer manifest lists its own under
 * `volumes`, because which volumes there are is the engine's answer; setup.py
 * decides what goes IN them, and this only needs to know a folder is complete. */
const LEGACY_VOLUME_FILES = ['assistant-data.tgz', 'neo4j-data.tgz']

const MANIFEST = 'manifest.json'

/* Copying a graph can legitimately take a long time; this is a backstop
 * against a wedged docker CLI, not a pace expectation. */
const TIMEOUT = 30 * 60_000

/** What the CLI's --json verbs come back with. */
export interface Result {
  ok: boolean
  message: string
  path?: string
}

/**
 * Run an `embabel` verb with --json and read its one-line verdict.
 *
 * stdout is the JSON and stderr is the narration, so a verb that fails before
 * it can print JSON still leaves something to report — which is the case where
 * a bare "failed" would be least helpful.
 */
function embabel(args: string[]): Promise<Result> {
  const dir = mounts.applianceDir()
  if (!dir) return Promise.resolve({ ok: false, message: 'Appliance checkout not found — run the app from the repo.' })
  return new Promise<Result>((resolve) => {
    execFile(
      path.join(dir, 'embabel'),
      [...args, '--json'],
      { cwd: dir, timeout: TIMEOUT, maxBuffer: 8 * 1024 * 1024 },
      (error, stdout, stderr) => {
        try {
          resolve(JSON.parse(stdout.trim()) as Result)
        } catch {
          const said = (stderr || stdout || (error ? error.message : '')).trim()
          resolve({ ok: false, message: said.length > 300 ? `…${said.slice(-300)}` : said || 'The appliance CLI said nothing.' })
        }
      },
    )
  })
}

/**
 * Back up the appliance into a new timestamped folder under destDir.
 * @param {string} destDir
 */
const backUp = (destDir: string) => embabel(['backup', destDir])

/**
 * Replace the appliance's data and config with a backup's. Destructive by
 * definition — the caller owns the confirmation, and --yes says so: the dialog
 * in main.ts IS the question, and asking it twice in two registers is worse
 * than asking it once.
 * @param {string} backupDir
 */
const restore = (backupDir: string) => embabel(['restore', backupDir, '--yes'])

/**
 * Is this folder a backup we can restore? Read before the confirmation dialog,
 * so the dialog can say WHICH backup it is about to overwrite the appliance
 * with — a date, not a folder name, is what someone confirms.
 * @param {string} backupDir
 * @returns {{ok: boolean, message: string, createdAt?: string}}
 */
function inspect(backupDir: string) {
  let manifest
  try {
    manifest = JSON.parse(fs.readFileSync(path.join(backupDir, MANIFEST), 'utf8'))
  } catch {
    return { ok: false, message: `No readable ${MANIFEST} — this folder is not an Embabel backup.` }
  }
  // Every volume it was taken with, or nothing: a graph without its documents (or
  // the reverse) is an appliance that contradicts itself, which is worse than a refusal.
  const named = Array.isArray(manifest.volumes) && manifest.volumes.length ? manifest.volumes : LEGACY_VOLUME_FILES
  for (const file of named as string[]) {
    if (!fs.existsSync(path.join(backupDir, file))) {
      return { ok: false, message: `Backup is incomplete — ${file} is missing.` }
    }
  }
  return { ok: true, message: '', createdAt: manifest.createdAt }
}

export { backUp, restore, inspect }
