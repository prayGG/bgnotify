/**
 * Die Discord-REST-Aufrufe, die der Worker braucht.
 *
 * Zwei verschiedene Berechtigungen sind hier im Spiel, und die Unterscheidung
 * ist wichtig:
 *
 *   - Bot-Token  — für alles, was den Server verändert (Rolle anlegen/zuweisen).
 *                  Geheim, liegt als Worker-Secret.
 *   - Interaction-Token — kommt mit jedem Command mit, gilt 15 Minuten und nur
 *                  für DIESE eine Interaktion. Damit wird die nachgereichte
 *                  Antwort geschrieben; ein Bot-Token braucht es dafür NICHT.
 */

const API = "https://discord.com/api/v10";

export const ROLE_NAME = "bgnotify";

function botHeaders(env) {
  return {
    authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
    "content-type": "application/json",
    "user-agent": "bgnotify-commands-worker",
  };
}

async function call(env, path, init = {}) {
  const res = await fetch(`${API}${path}`, { ...init, headers: botHeaders(env) });
  if (!res.ok) {
    // Discords Fehlertext mitnehmen — "Missing Permissions" vs. "Unknown Guild"
    // zu unterscheiden spart bei der Fehlersuche viel Raterei.
    const detail = await res.text().catch(() => "");
    throw new Error(`Discord ${init.method || "GET"} ${path} → HTTP ${res.status} ${detail.slice(0, 200)}`);
  }
  return res.status === 204 ? null : res.json();
}

export function botConfigured(env) {
  return Boolean(env.DISCORD_BOT_TOKEN);
}

/** Gilde holen — gebraucht wird daraus nur `owner_id`. */
export function getGuild(env, guildId) {
  return call(env, `/guilds/${guildId}`);
}

export function listRoles(env, guildId) {
  return call(env, `/guilds/${guildId}/roles`);
}

/**
 * Rolle anlegen. Bewusst ohne jede Berechtigung (`permissions: "0"`): Sie ist
 * reines Kennzeichen für den Worker, kein Discord-Recht. Wer sie trägt, darf
 * die Commands benutzen — auf dem Server selbst kann sie nichts.
 */
export function createRole(env, guildId, name = ROLE_NAME) {
  return call(env, `/guilds/${guildId}/roles`, {
    method: "POST",
    body: JSON.stringify({ name, permissions: "0", mentionable: false }),
  });
}

export function addRoleToMember(env, guildId, userId, roleId) {
  return call(env, `/guilds/${guildId}/members/${userId}/roles/${roleId}`, { method: "PUT" });
}

/**
 * Die vorher deferrte Antwort nachreichen.
 *
 * Läuft über das Interaction-Token, nicht über das Bot-Token — deshalb steht
 * hier bewusst kein `botHeaders`.
 */
export async function editOriginalResponse(interaction, content) {
  const url = `${API}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;
  const res = await fetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json", "user-agent": "bgnotify-commands-worker" },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) {
    throw new Error(`Antwort nachreichen fehlgeschlagen (HTTP ${res.status})`);
  }
}
