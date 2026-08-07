/**
 * Discord-Interactions-Endpoint auf Cloudflare Workers.
 *
 * Discord schickt jeden Slash-Command als HTTPS-POST hierher — es braucht also
 * KEINEN dauerlaufenden Bot-Prozess (und damit keinen gemieteten Server). Der
 * Worker beantwortet den Command und schreibt in das private Gist; gepostet
 * wird weiterhin über die bestehenden Webhooks des Actions-Bots.
 *
 * Stand: Schritt 2 — Signaturprüfung, Rollenprüfung, `/setup`, `/ping`.
 *
 * Erwartete Secrets (via `wrangler secret put`):
 *   DISCORD_PUBLIC_KEY   Public Key der Discord-App (hex) — Pflicht
 *   DISCORD_BOT_TOKEN    Bot-Token, nur für `/setup` (Rolle anlegen/zuweisen)
 *   GIST_TOKEN           PAT mit ausschließlich `gist`-Recht
 *   GIST_ID              ID des privaten Gists
 */

import { gistConfigured, loadState, roleIdFor, saveState } from "./gist.js";
import {
  ROLE_NAME,
  addRoleToMember,
  botConfigured,
  createRole,
  editOriginalResponse,
  getGuild,
  listRoles,
} from "./discord.js";

const InteractionType = { PING: 1, APPLICATION_COMMAND: 2 };
const InteractionResponseType = {
  PONG: 1,
  CHANNEL_MESSAGE_WITH_SOURCE: 4,
  DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: 5,
};
const EPHEMERAL = 1 << 6; // Antwort sieht nur, wer den Command aufgerufen hat

function hexToBytes(hex) {
  const clean = (hex || "").trim();
  if (clean.length % 2 !== 0) return null;
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) {
    const byte = parseInt(clean.substr(i * 2, 2), 16);
    if (Number.isNaN(byte)) return null;
    out[i] = byte;
  }
  return out;
}

/**
 * Ed25519-Signatur prüfen.
 *
 * Zwei Algorithmus-Schreibweisen, weil die Workers-Runtime historisch
 * "NODE-ED25519" verlangte und erst neuere Versionen das standardisierte
 * "Ed25519" kennen. Erst der Standard, dann der Altname — so läuft derselbe
 * Code auf alter wie neuer Runtime, statt bei einem Plattform-Update still
 * kaputtzugehen.
 */
async function verifySignature(publicKeyHex, signatureHex, timestamp, rawBody) {
  const keyBytes = hexToBytes(publicKeyHex);
  const sigBytes = hexToBytes(signatureHex);
  if (!keyBytes || !sigBytes) return false;

  const message = new TextEncoder().encode(timestamp + rawBody);
  const algorithms = [
    { name: "Ed25519" },
    { name: "NODE-ED25519", namedCurve: "NODE-ED25519" },
  ];

  for (const algorithm of algorithms) {
    try {
      const key = await crypto.subtle.importKey("raw", keyBytes, algorithm, false, ["verify"]);
      return await crypto.subtle.verify(algorithm.name, key, sigBytes, message);
    } catch (err) {
      // Unbekannter Algorithmus auf dieser Runtime → nächste Schreibweise.
      if (err instanceof DOMException || err instanceof TypeError) continue;
      throw err;
    }
  }
  return false;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

/** Kurze, nur für den Aufrufer sichtbare Antwort. */
function reply(content) {
  return json({
    type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
    data: { content, flags: EPHEMERAL },
  });
}

/**
 * "Denkt nach…" — Discord verlangt binnen 3 Sekunden eine Antwort. Wer länger
 * braucht, bestätigt sofort und reicht das Ergebnis über
 * `editOriginalResponse` nach (Frist dafür: 15 Minuten).
 */
function deferred() {
  return json({
    type: InteractionResponseType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
    data: { flags: EPHEMERAL },
  });
}

// --------------------------------------------------------------------------
// Rechteprüfung
// --------------------------------------------------------------------------

/**
 * Darf der Aufrufer diesen Command benutzen? Gibt `null` zurück, wenn ja,
 * sonst den Ablehnungstext.
 *
 * Geprüft wird ausschließlich die Rolle — Discord liefert die Rollen des
 * Aufrufers bei jedem Command gleich mit, das kostet also keinen zusätzlichen
 * API-Aufruf. `default_member_permissions: "0"` beim Registrieren versteckt die
 * Commands zwar vor normalen Mitgliedern, aber das ist reine Sichtbarkeit und
 * keine Absicherung: Wer die Command-ID kennt, käme sonst durch. Die eigentliche
 * Prüfung ist deshalb genau hier.
 */
async function denyReason(interaction, env) {
  if (!gistConfigured(env)) {
    return "Dem Worker fehlt der Gist-Zugang (`GIST_TOKEN` / `GIST_ID` nicht gesetzt).";
  }

  let state;
  try {
    state = await loadState(env);
  } catch (err) {
    return `Stand nicht lesbar: ${err.message}`;
  }

  const roleId = roleIdFor(state, interaction.guild_id);
  if (!roleId) {
    return `Auf diesem Server ist noch nichts eingerichtet — einmalig \`/setup\` ausführen.`;
  }
  if (!(interaction.member?.roles || []).includes(roleId)) {
    return `Dafür brauchst du die Rolle <@&${roleId}>.`;
  }
  return null;
}

// --------------------------------------------------------------------------
// /setup
// --------------------------------------------------------------------------

/**
 * Legt die Rolle an, gibt sie dem Aufrufer und merkt sie sich im Gist.
 *
 * Läuft nach der deferrten Antwort und meldet sein Ergebnis selbst — deshalb
 * wirft die Funktion nichts nach außen, sondern schreibt auch Fehler in die
 * nachgereichte Antwort. Ein stiller Fehlschlag wäre hier besonders ärgerlich:
 * Der Aufrufer sähe eine Antwort, die ewig "denkt nach…" bleibt.
 */
async function runSetup(interaction, env) {
  try {
    const guildId = interaction.guild_id;
    const userId = interaction.member.user.id;

    // Inhaberschaft echt prüfen statt auf "Administrator" zu vertrauen — Admin
    // kann jeder werden, dem jemand die Rechte gibt. Der Bootstrap soll aber
    // wirklich nur dem gehören, dem der Server gehört.
    const guild = await getGuild(env, guildId);
    if (guild.owner_id !== userId) {
      await editOriginalResponse(interaction, "Nur der Server-Inhaber darf `/setup` ausführen.");
      return;
    }

    const state = await loadState(env);
    const roles = await listRoles(env, guildId);

    // Reihenfolge mit Absicht: gespeicherte ID → gleichnamige Rolle → neu anlegen.
    // Der mittlere Fall fängt den ab, bei dem der Gist-Eintrag weg ist, die Rolle
    // auf dem Server aber noch steht — sonst sammelten sich dort Duplikate an.
    let roleId = roleIdFor(state, guildId);
    let known = roles.find((r) => r.id === roleId);
    if (!known) known = roles.find((r) => r.name === ROLE_NAME);

    let note;
    if (known) {
      roleId = known.id;
      note = "bestehende Rolle übernommen";
    } else {
      roleId = (await createRole(env, guildId)).id;
      note = "Rolle neu angelegt";
    }

    await addRoleToMember(env, guildId, userId, roleId);

    state.guilds = state.guilds || {};
    state.guilds[guildId] = {
      role_id: roleId,
      configured_at: new Date().toISOString(),
      configured_by: userId,
    };
    await saveState(env, state);

    await editOriginalResponse(
      interaction,
      [
        `Eingerichtet — ${note}: <@&${roleId}>`,
        "",
        "Du hast sie bereits. Wer die Commands sonst noch benutzen soll, bekommt sie über",
        "*Servereinstellungen → Mitglieder*.",
      ].join("\n")
    );
  } catch (err) {
    await editOriginalResponse(interaction, `\`/setup\` fehlgeschlagen: ${err.message}`).catch(
      () => {}
    );
  }
}

async function handleSetup(interaction, env, ctx) {
  const missing = [];
  if (!botConfigured(env)) missing.push("`DISCORD_BOT_TOKEN`");
  if (!gistConfigured(env)) missing.push("`GIST_TOKEN` / `GIST_ID`");
  if (missing.length) {
    return reply(`Dem Worker fehlen noch Secrets: ${missing.join(", ")}.`);
  }

  const work = runSetup(interaction, env);
  if (ctx?.waitUntil) {
    // Normalfall: `waitUntil` hält den Worker über die Antwort hinaus am Leben,
    // die Einrichtung läuft also weiter, während Discord schon "denkt nach…" zeigt.
    ctx.waitUntil(work);
  } else {
    // Kein ctx — das ist der Test, der worker.fetch() ohne dritten Parameter
    // aufruft. Dann direkt abwarten, sonst wäre der Ablauf nicht prüfbar.
    await work;
  }
  return deferred();
}

// --------------------------------------------------------------------------
// Routing
// --------------------------------------------------------------------------

async function handleCommand(interaction, env, ctx) {
  const name = interaction.data?.name;

  // Ohne Server kein Rollenbegriff — und damit keine Rechteprüfung, auf die
  // sich irgendetwas verlassen könnte.
  if (!interaction.guild_id || !interaction.member) {
    return reply("Die Commands funktionieren nur in einem Server, nicht in Direktnachrichten.");
  }

  if (name === "setup") return handleSetup(interaction, env, ctx);

  const denied = await denyReason(interaction, env);
  if (denied) return reply(denied);

  switch (name) {
    case "ping":
      // Bewusst nutzlos: beweist nur, dass Signatur- und Rollenprüfung stehen.
      return reply("pong — Signatur- und Rollenprüfung stehen.");
    default:
      return reply(`Unbekannter Command: \`/${name}\``);
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("bgnotify interactions endpoint", { status: 200 });
    }

    const signature = request.headers.get("x-signature-ed25519");
    const timestamp = request.headers.get("x-signature-timestamp");
    if (!signature || !timestamp) {
      return new Response("missing signature headers", { status: 401 });
    }

    // Body als Text lesen: die Signatur gilt über die ROHEN Bytes. Erst parsen,
    // dann prüfen würde die Signatur wertlos machen (JSON.stringify liefert
    // nicht zwingend dieselbe Byte-Folge zurück).
    const rawBody = await request.text();

    const valid = await verifySignature(env.DISCORD_PUBLIC_KEY, signature, timestamp, rawBody);
    if (!valid) {
      // 401 ist Pflicht — Discord testet die URL beim Eintragen absichtlich mit
      // einer kaputten Signatur und akzeptiert sie nur, wenn wir ablehnen.
      return new Response("invalid request signature", { status: 401 });
    }

    let interaction;
    try {
      interaction = JSON.parse(rawBody);
    } catch {
      return new Response("bad json", { status: 400 });
    }

    if (interaction.type === InteractionType.PING) {
      return json({ type: InteractionResponseType.PONG });
    }
    if (interaction.type === InteractionType.APPLICATION_COMMAND) {
      return handleCommand(interaction, env, ctx);
    }
    return new Response("unsupported interaction type", { status: 400 });
  },
};
