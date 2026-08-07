/**
 * Zustand der Commands im privaten Gist — ausschließlich in `commands.json`.
 *
 * WARUM EINE EIGENE DATEI: Der Actions-Bot arbeitet nach dem Muster
 * laden → ändern → speichern auf `order-state.json`. Schriebe der Worker in
 * dieselbe Datei, ginge jede Änderung verloren, die zufällig zwischen dem Laden
 * und dem Speichern des Bots landet. Getrennte Dateien schließen das baulich
 * aus — der Worker fasst `order-state.json` NIE an.
 *
 * Der PATCH auf die Gist-API überträgt nur die genannte Datei; alle anderen
 * Dateien des Gists bleiben unberührt. Genau deshalb ist das hier sicher.
 */

const API = "https://api.github.com";

// Die einzige Datei, in die dieser Worker schreiben darf.
export const STATE_FILE = "commands.json";

/** GitHub lehnt Anfragen ohne User-Agent ab — deshalb ist der Pflicht, nicht Kosmetik. */
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
 * Stand laden. Fehlt die Datei noch (erster Lauf), ist das kein Fehler,
 * sondern schlicht ein leerer Stand.
 */
export async function loadState(env) {
  const res = await fetch(`${API}/gists/${env.GIST_ID}`, { headers: headers(env) });
  if (!res.ok) {
    throw new Error(`Gist lesen fehlgeschlagen (HTTP ${res.status})`);
  }
  const gist = await res.json();
  const file = gist.files?.[STATE_FILE];
  if (!file) return { guilds: {} };

  // Bei großen Dateien liefert die API nur einen Ausschnitt und setzt
  // `truncated` — dann muss der volle Inhalt über raw_url nachgeladen werden.
  // commands.json bleibt zwar winzig, aber ein stiller Teilinhalt wäre ein
  // besonders unangenehmer Fehler: Er sähe aus wie gültiges JSON.
  let content = file.content;
  if (file.truncated && file.raw_url) {
    content = await (await fetch(file.raw_url, { headers: headers(env) })).text();
  }

  try {
    const parsed = JSON.parse(content || "{}");
    return { guilds: {}, ...parsed };
  } catch {
    // Kaputtes JSON nicht stillschweigend durch einen leeren Stand ersetzen —
    // das würde eine bestehende Einrichtung beim nächsten Schreiben überbügeln.
    throw new Error(`${STATE_FILE} im Gist ist kein gültiges JSON`);
  }
}

/** Stand speichern. Rührt ausschließlich `commands.json` an. */
export async function saveState(env, state) {
  const res = await fetch(`${API}/gists/${env.GIST_ID}`, {
    method: "PATCH",
    headers: headers(env),
    body: JSON.stringify({
      files: { [STATE_FILE]: { content: JSON.stringify(state, null, 2) } },
    }),
  });
  if (!res.ok) {
    throw new Error(`Gist schreiben fehlgeschlagen (HTTP ${res.status})`);
  }
}

/** Eingerichtete Rolle dieses Servers, oder "" wenn noch nichts eingerichtet ist. */
export function roleIdFor(state, guildId) {
  return state?.guilds?.[guildId]?.role_id || "";
}
