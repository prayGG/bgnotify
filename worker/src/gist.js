/**
 * Zugriff auf das private Gist.
 *
 * Zwei Dateien, streng getrennte Rechte:
 *
 *   commands.json     — gehört dem Worker, lesen und schreiben
 *   order-state.json  — gehört dem Actions-Bot, **nur lesen**
 *
 * WARUM: Der Bot arbeitet auf `order-state.json` nach dem Muster
 * laden → ändern → speichern. Schriebe der Worker mit hinein, ginge jede
 * Änderung verloren, die zufällig zwischen dem Laden und dem Speichern des
 * Bots entsteht. Deshalb gibt es hier bewusst keine Schreibfunktion dafür, und
 * `saveState` benennt beim PATCH ausschließlich `commands.json` — die
 * Gist-API überträgt nur die genannten Dateien, alle anderen bleiben unberührt.
 */

const API = "https://api.github.com";

export const STATE_FILE = "commands.json";
const ORDER_FILE = "order-state.json";

/** GitHub lehnt Anfragen ohne User-Agent ab — Pflicht, nicht Kosmetik. */
function headers(env) {
  return {
    authorization: `Bearer ${env.GIST_TOKEN}`,
    accept: "application/vnd.github+json",
    "user-agent": "bgnotify-commands-worker",
    "content-type": "application/json",
  };
}

export function gistConfigured(env) {
  return Boolean(env.GIST_TOKEN && env.GIST_ID);
}

/**
 * Inhalt einer Gist-Datei als JSON. Fehlt sie, kommt `fallback` zurück — das
 * ist kein Fehler, sondern der Normalfall beim allerersten Lauf.
 *
 * Bei großen Dateien liefert die API nur einen Ausschnitt und setzt
 * `truncated`; dann muss über `raw_url` nachgeladen werden. Beide Dateien
 * bleiben zwar klein, aber ein stiller Teilinhalt wäre besonders unangenehm:
 * Er sähe aus wie gültiges JSON.
 */
async function parseFile(env, gist, name, fallback) {
  const file = gist.files?.[name];
  if (!file) return fallback;

  let content = file.content;
  if (file.truncated && file.raw_url) {
    content = await (await fetch(file.raw_url, { headers: headers(env) })).text();
  }
  try {
    return JSON.parse(content || "null") ?? fallback;
  } catch {
    // Kaputtes JSON nicht stillschweigend durch einen leeren Stand ersetzen —
    // das würde eine bestehende Einrichtung beim nächsten Schreiben überbügeln.
    throw new Error(`${name} im Gist ist kein gültiges JSON`);
  }
}

/**
 * Beide Dateien mit EINEM Abruf.
 *
 * Getrennte Aufrufe wären derselbe GET zweimal: Die Rollenprüfung braucht
 * `commands.json`, die Ansichten `order-state.json`. Bei Discords
 * 3-Sekunden-Grenze ist ein gesparter Netzweg kein Detail.
 */
export async function loadAll(env) {
  const res = await fetch(`${API}/gists/${env.GIST_ID}`, { headers: headers(env) });
  if (!res.ok) throw new Error(`Gist lesen fehlgeschlagen (HTTP ${res.status})`);
  const gist = await res.json();
  return {
    commands: await parseFile(env, gist, STATE_FILE, { guilds: {} }),
    orders: await parseFile(env, gist, ORDER_FILE, {}),
  };
}

/** Nur den Worker-Stand laden. */
export async function loadState(env) {
  return (await loadAll(env)).commands;
}

/** Worker-Stand speichern. Rührt ausschließlich `commands.json` an. */
export async function saveState(env, state) {
  const res = await fetch(`${API}/gists/${env.GIST_ID}`, {
    method: "PATCH",
    headers: headers(env),
    body: JSON.stringify({
      files: { [STATE_FILE]: { content: JSON.stringify(state, null, 2) } },
    }),
  });
  if (!res.ok) throw new Error(`Gist schreiben fehlgeschlagen (HTTP ${res.status})`);
}

/** Eingerichtete Rolle dieses Servers, oder "" wenn noch nichts eingerichtet ist. */
export function roleIdFor(state, guildId) {
  return state?.guilds?.[guildId]?.role_id || "";
}
