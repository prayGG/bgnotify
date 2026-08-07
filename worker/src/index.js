/**
 * Discord-Interactions-Endpoint auf Cloudflare Workers.
 *
 * Discord schickt jeden Slash-Command als HTTPS-POST hierher — es braucht also
 * KEINEN dauerlaufenden Bot-Prozess (und damit keinen gemieteten Server). Der
 * Worker beantwortet den Command und schreibt in das private Gist; Meldungen
 * in die Channels laufen weiterhin über die Webhooks des Actions-Bots.
 *
 * Stand: Schritt 3 — Signatur- und Rollenprüfung, `/setup`, `/panel`, `/ping`
 * und die nur-lesenden Ansichten `/status`, `/track list`, `/account list`.
 *
 * Erwartete Secrets (via `wrangler secret put`):
 *   DISCORD_PUBLIC_KEY   Public Key der Discord-App (hex) — Pflicht
 *   DISCORD_BOT_TOKEN    Bot-Token (Rolle anlegen, Panel posten)
 *   GIST_TOKEN           klassischer PAT mit ausschließlich `gist`-Recht
 *   GIST_ID              ID des privaten Gists
 */

import { gistConfigured, loadAll, roleIdFor, saveState } from "./gist.js";
import {
  ROLE_NAME,
  addRoleToMember,
  botConfigured,
  createRole,
  editOriginalResponse,
  getGuild,
  listRoles,
} from "./discord.js";
import { publish, refreshIfStale } from "./panel.js";
import { accountListView, statusView, trackListView } from "./views.js";
import { addTracking, parseTrackingLink, removeTracking, setAccountEnabled } from "./actions.js";
import { SUB_COMMAND } from "./catalog.js";
import { loadAccountLabels } from "./repo.js";

const InteractionType = { PING: 1, APPLICATION_COMMAND: 2, AUTOCOMPLETE: 4 };
const InteractionResponseType = {
  PONG: 1,
  CHANNEL_MESSAGE_WITH_SOURCE: 4,
  DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: 5,
  AUTOCOMPLETE_RESULT: 8,
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

function replyEmbed(embed) {
  return json({
    type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
    data: { embeds: [embed], flags: EPHEMERAL },
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

/**
 * Arbeit hinter einer deferrten Antwort starten.
 *
 * `waitUntil` hält den Worker über die Antwort hinaus am Leben. Im Test gibt es
 * kein ctx — dann läuft der Ablauf direkt, was ihn überhaupt erst prüfbar macht.
 */
async function deferTo(work, ctx) {
  if (ctx?.waitUntil) ctx.waitUntil(work);
  else await work;
  return deferred();
}

// --------------------------------------------------------------------------
// Commands, die dem Server-Inhaber vorbehalten sind
// --------------------------------------------------------------------------

/**
 * Inhaberschaft echt prüfen statt auf "Administrator" zu vertrauen — Admin kann
 * jeder werden, dem jemand die Rechte gibt. Kostet einen API-Aufruf, fällt aber
 * nur bei den beiden Verwaltungs-Commands an.
 */
async function isOwner(env, guildId, userId) {
  const guild = await getGuild(env, guildId);
  return guild.owner_id === userId;
}

/**
 * Legt die Rolle an, gibt sie dem Aufrufer und merkt sie im Gist.
 *
 * Meldet sein Ergebnis selbst — auch Fehler. Ein stiller Fehlschlag wäre hier
 * besonders ärgerlich: Der Aufrufer sähe eine Antwort, die ewig „denkt nach…"
 * bleibt.
 */
async function runSetup(interaction, env, state) {
  try {
    const guildId = interaction.guild_id;
    const userId = interaction.member.user.id;

    if (!(await isOwner(env, guildId, userId))) {
      await editOriginalResponse(interaction, "Nur der Server-Inhaber darf `/setup` ausführen.");
      return;
    }

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

    state.guilds ||= {};
    const entry = (state.guilds[guildId] ||= {});
    entry.role_id = roleId;
    entry.configured_at = new Date().toISOString();
    entry.configured_by = userId;
    await saveState(env, state);

    await editOriginalResponse(
      interaction,
      [
        `Eingerichtet — ${note}: <@&${roleId}>`,
        "",
        "Du hast sie bereits. Wer die Commands sonst noch benutzen soll, bekommt sie über",
        "*Servereinstellungen → Mitglieder*.",
        "",
        "Als Nächstes: `/panel` in dem Channel aufrufen, in dem die Befehlsübersicht stehen soll.",
      ].join("\n")
    );
  } catch (err) {
    await editOriginalResponse(interaction, `\`/setup\` fehlgeschlagen: ${err.message}`).catch(() => {});
  }
}

/** Postet die Befehlsübersicht in den Channel, in dem der Command lief. */
async function runPanel(interaction, env, state) {
  try {
    const guildId = interaction.guild_id;
    if (!(await isOwner(env, guildId, interaction.member.user.id))) {
      await editOriginalResponse(interaction, "Nur der Server-Inhaber darf `/panel` ausführen.");
      return;
    }

    const { recreated } = await publish(env, state, guildId, interaction.channel_id);
    await editOriginalResponse(
      interaction,
      recreated
        ? "Übersicht gepostet. Sie hält sich ab jetzt selbst aktuell — kommen Commands dazu, wird diese Nachricht bearbeitet."
        : "Übersicht aktualisiert."
    );
  } catch (err) {
    await editOriginalResponse(
      interaction,
      `\`/panel\` fehlgeschlagen: ${err.message}\n\nFehlt dem Bot vielleicht das Recht, in diesem Channel zu schreiben?`
    ).catch(() => {});
  }
}

// --------------------------------------------------------------------------
// Routing
// --------------------------------------------------------------------------

/** „track list" statt nur „track" — Untercommands stehen als Option vom Typ 1 drin. */
function commandPath(data) {
  const sub = (data?.options || []).find((o) => o.type === SUB_COMMAND);
  return sub ? `${data.name} ${sub.name}` : data?.name || "";
}

/** Die eigentlichen Argumente — bei Untercommands liegen sie eine Ebene tiefer. */
function optionValues(data) {
  const sub = (data?.options || []).find((o) => o.type === SUB_COMMAND);
  const opts = (sub ? sub.options : data?.options) || [];
  return Object.fromEntries(opts.map((o) => [o.name, o.value]));
}

/** Die Option, in der der Nutzer gerade tippt (nur bei Autocomplete gesetzt). */
function focusedOption(data) {
  const sub = (data?.options || []).find((o) => o.type === SUB_COMMAND);
  const opts = (sub ? sub.options : data?.options) || [];
  return opts.find((o) => o.focused) || null;
}

function autocompleteResult(choices) {
  // Discord nimmt höchstens 25 Vorschläge an und lehnt mehr komplett ab.
  return json({
    type: InteractionResponseType.AUTOCOMPLETE_RESULT,
    data: { choices: choices.slice(0, 25) },
  });
}

/**
 * Vorschläge beim Tippen.
 *
 * Auch hier greift die Rollenprüfung: Die Vorschläge verraten Kontonamen und
 * Sendungsbezeichnungen. Wer nicht darf, bekommt eine leere Liste statt einer
 * Fehlermeldung — Discord hat für Autocomplete keine Möglichkeit, Text
 * anzuzeigen, und eine leere Liste ist die ehrlichste stille Antwort.
 */
async function handleAutocomplete(interaction, env) {
  if (!gistConfigured(env) || !interaction.guild_id) return autocompleteResult([]);

  let data;
  try {
    data = await loadAll(env);
  } catch {
    return autocompleteResult([]);
  }

  const roleId = roleIdFor(data.commands, interaction.guild_id);
  if (!roleId || !(interaction.member?.roles || []).includes(roleId)) {
    return autocompleteResult([]);
  }

  const path = commandPath(interaction.data);
  const focused = focusedOption(interaction.data);
  const eingabe = String(focused?.value || "").toLowerCase();
  const passt = (s) => s.toLowerCase().includes(eingabe);

  if (path === "account enable" || path === "account disable") {
    const labels = await loadAccountLabels();
    const choices = Object.keys(data.orders.accounts || {})
      .map((key) => ({ name: labels[key] ? `${labels[key]} (${key})` : key, value: key }))
      .filter((c) => passt(c.name) || passt(c.value));
    return autocompleteResult(choices);
  }

  if (path === "track remove") {
    // Nur die selbst eingetragenen: automatisch übernommene Sendungen räumt
    // der Bot nach der Zustellung ohnehin weg, die kann man nicht sinnvoll
    // von Hand entfernen.
    const choices = Object.keys(data.commands.tracking || {})
      .filter(passt)
      .map((label) => ({ name: label, value: label }));
    return autocompleteResult(choices);
  }

  return autocompleteResult([]);
}

async function handleCommand(interaction, env, ctx) {
  const path = commandPath(interaction.data);

  // Ohne Server kein Rollenbegriff — und damit keine Rechteprüfung, auf die
  // sich irgendetwas verlassen könnte.
  if (!interaction.guild_id || !interaction.member) {
    return reply("Die Commands funktionieren nur in einem Server, nicht in Direktnachrichten.");
  }

  const missing = [];
  if (!botConfigured(env)) missing.push("`DISCORD_BOT_TOKEN`");
  if (!gistConfigured(env)) missing.push("`GIST_TOKEN` / `GIST_ID`");
  if (missing.length) return reply(`Dem Worker fehlen noch Secrets: ${missing.join(", ")}.`);

  // EIN Abruf für beide Dateien — die Rollenprüfung braucht commands.json,
  // die Ansichten order-state.json.
  let data;
  try {
    data = await loadAll(env);
  } catch (err) {
    return reply(`Stand nicht lesbar: ${err.message}`);
  }
  const state = data.commands;

  if (path === "setup") return deferTo(runSetup(interaction, env, state), ctx);

  // Ab hier gilt die Rolle. Geprüft wird sie ohne zusätzlichen API-Aufruf:
  // Discord schickt die Rollen des Aufrufers bei jedem Command mit.
  const roleId = roleIdFor(state, interaction.guild_id);
  if (!roleId) {
    return reply("Auf diesem Server ist noch nichts eingerichtet — einmalig `/setup` ausführen.");
  }
  if (!(interaction.member.roles || []).includes(roleId)) {
    return reply(`Dafür brauchst du die Rolle <@&${roleId}>.`);
  }

  // Katalog geändert? Dann die Übersicht im Hintergrund nachziehen. Im
  // Normalfall ist das ein String-Vergleich und sonst nichts.
  //
  // Nicht bei `/panel` selbst: das schreibt die Übersicht ohnehin gleich neu.
  // Beides zusammen liefe auf zwei parallele Veröffentlichungen hinaus, die um
  // denselben Gist-Eintrag konkurrieren — und im Zweifel zwei Nachrichten im
  // Channel hinterlassen.
  if (path !== "panel") refreshIfStale(env, state, interaction.guild_id, ctx);

  const args = optionValues(interaction.data);
  const userId = interaction.member.user.id;

  switch (path) {
    case "ping":
      return reply("pong — Signatur- und Rollenprüfung stehen.");
    case "status":
      return replyEmbed(await statusView(data.orders, state));
    case "track list":
      return replyEmbed(await trackListView(data.orders, state));
    case "account list":
      return replyEmbed(await accountListView(data.orders, state));
    case "panel":
      return deferTo(runPanel(interaction, env, state), ctx);

    case "account enable":
    case "account disable": {
      const known = Object.keys(data.orders.accounts || {});
      return reply(
        await setAccountEnabled(env, state, args.konto, path.endsWith("enable"), known)
      );
    }

    case "track add": {
      const { url, suggested, error } = parseTrackingLink(args.link);
      if (error) return reply(error);
      const label = (args.name || suggested || "").trim();
      if (!label) {
        return reply(
          "Aus dem Link lässt sich kein Name ableiten — gib einen mit an, z.B. `name: mave`."
        );
      }
      return reply(await addTracking(env, state, label, url, userId));
    }

    case "track remove": {
      const { text } = await removeTracking(env, state, args.name);
      return reply(text);
    }

    default:
      return reply(`Unbekannter Command: \`/${path}\``);
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
    if (interaction.type === InteractionType.AUTOCOMPLETE) {
      return handleAutocomplete(interaction, env);
    }
    return new Response("unsupported interaction type", { status: 400 });
  },
};
