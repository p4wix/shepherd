'use strict';

// herdr-radar render hook: puts the agent's name in front of its sidebar title,
// e.g. "lead · Claude Code" under the "uigen" header for the agent uigen-lead.
// Enabled by `render_hook` in ~/.config/herdr/plugins/config/hhdebb.herdr-radar/config.toml.
// Agents without a name keep their title unchanged.

const { execFileSync } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');

const HERDR = process.env.SHEPHERD_HERDR || path.join(os.homedir(), '.local/bin/herdr');
const TTL_MS = 3000;

let names = new Map(); // pane id -> name shown in the sidebar
let fetchedAt = 0;

function herdr(...args) {
  const out = execFileSync(HERDR, args, { encoding: 'utf8', timeout: 1000 });
  return JSON.parse(out).result;
}

function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/^[^a-z]*/, '');
}

function refresh() {
  if (Date.now() - fetchedAt < TTL_MS) return;
  fetchedAt = Date.now();
  try {
    const labels = new Map(herdr('workspace', 'list').workspaces.map((w) => [w.workspace_id, w.label]));
    const next = new Map();
    for (const a of herdr('agent', 'list').agents) {
      if (!a.name) continue;
      // The workspace header already shows the label, so drop it from the name.
      const prefix = slug(labels.get(a.workspace_id) ?? '') + '-';
      const short = prefix.length > 1 && a.name.startsWith(prefix) ? a.name.slice(prefix.length) : a.name;
      next.set(a.pane_id, short);
    }
    names = next;
  } catch {
    // Keep the last good map; the sidebar must not break because herdr was busy.
  }
}

function title(text, paneId) {
  refresh();
  const name = names.get(paneId);
  return name ? `${name} · ${text}` : text;
}

module.exports = { title };
