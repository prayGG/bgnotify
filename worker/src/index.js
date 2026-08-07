/**
 * Discord-Interactions-Endpoint auf Cloudflare Workers.
 *
 * Discord schickt jeden Slash-Command als HTTPS-POST hierher — es braucht also
 * KEINEN dauerlaufenden Bot-Prozess (und damit keinen gemieteten Server). Der
 * Worker beantwortet den Command und schreibt in das private Gist; Meldungen
 * in die Channels laufen weiterhin über die Webhooks des Actions-Bots.
 *
 * Stand: vollständig (Schritte 1–7).
 *   lesen      /status, /track list, /account list, /product list
 *   schreiben  /account enable|disable|add|remove, /track add|remove,
 *              /product add|remove
 *   auslösen   /run
 *   Rest       /ping, /setup, /panel
 *
 * Erwartete Secrets (via `wrangler secret put`):
 *   DISCORD_PUBLIC_KEY   Public Key der Discord-App (hex) — Pflicht
 *   DISCORD_BOT_TOKEN    Bot-Token (Rolle anlegen, Panel posten)
 *   GIST_TOKEN           klassischer PAT mit ausschließlich `gist`-Recht
 *   GIST_ID              ID des privaten Gists
 *   GITHUB_TOKEN         fein-granular: Actions + Secrets, nur dieses Repo
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
import { accountListView, productListView, statusView, trackListView } from "./views.js";
import {
  SLOTS,
  addAccount,
  addProduct,
  addTracking,
  freeSlot,
  parseProductLink,
  parseTrackingLink,
  removeAccount,
  removeProduct,
  removeTracking,
  requestScan,
  setAccountEnabled,
  slotLabels,
  triggerRun,
} from "./actions.js";
import { dispatchRun, githubConfigured } from "./github.js";
import { ACCOUNT_ADD, accountAddModal, modalValues } from "./modal.js";
import { SUB_COMMAND } from "./catalog.js";
import { loadAccountLabels } from "./repo.js";
import { clip } from "./format.js";

const InteractionType = {
  PING: 1,
  APPLICATION_COMMAND: 2,
  AUTOCOMPLETE: 4,
  MODAL_SUBMIT: 5,
};
const InteractionResponseType = {
  PONG: 1,
  CHANNEL_MESSAGE_WITH_SOURCE: 4,
  DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: 5,
  AUTOCOMPLETE_RESULT: 8,
  MODAL: 9,
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

/**
 * Das abgeschickte Formular verarbeiten: verschlüsseln, Secrets setzen, Platz
 * vermerken.
 *
 * Läuft hinter einer deferrten Antwort, weil bis zu fünf Netzaufrufe anfallen
 * (Repo-Schlüssel, drei Secrets, Gist). Fehler landen in derselben Antwort —
 * wer gerade sein Passwort eingetippt hat, soll nicht auf eine Meldung warten,
 * die nie kommt.
 */
async function runAccountAdd(interaction, env, state, werte) {
  try {
    const slot = freeSlot(state);
    if (slot === null) {
      await editOriginalResponse(interaction, "Inzwischen sind alle Plätze belegt.");
      return;
    }
    await addAccount(
      env, state, slot, werte.label, werte.user, werte.pass,
      interaction.member.user.id
    );

    // Gleich einen Lauf anstoßen, damit der Login sofort geprüft wird statt
    // erst beim nächsten Takt. Scheitert das, ist das Konto trotzdem angelegt —
    // deshalb nur eine abgeschwächte Zeile statt eines Fehlers.
    let pruefung = "Der Login wird gerade geprüft — das Ergebnis kommt gleich hier in den Channel.";
    try {
      await dispatchRun(env);
      state.last_run_at = new Date().toISOString();
      await saveState(env, state);
    } catch {
      pruefung = "Ob der Login stimmt, prüft der nächste Lauf — mit `/run` geht es sofort.";
    }

    await editOriginalResponse(
      interaction,
      [
        `**${werte.label}** ist hinterlegt (Platz ${slot}).`,
        "",
        "Die Zugangsdaten sind verschlüsselt als GitHub-Secret abgelegt — im Gist stehen nur",
        "der Anzeigename und die Platznummer.",
        "",
        pruefung,
      ].join("\n")
    );
  } catch (err) {
    await editOriginalResponse(
      interaction,
      `Konnte das Konto nicht hinterlegen: ${err.message}`
    ).catch(() => {});
  }
}

/** Konto entfernen: Secrets löschen, Platz freigeben. */
async function runAccountRemove(interaction, env, state, slot) {
  try {
    const eintrag = state.accounts?.[String(slot)];
    if (!eintrag) {
      await editOriginalResponse(
        interaction,
        "Das ist kein selbst hinterlegtes Konto. Fest verdrahtete Konten stehen in `config.yml` und bleiben."
      );
      return;
    }
    await removeAccount(env, state, Number(slot));
    await editOriginalResponse(
      interaction,
      `**${eintrag.label}** ist entfernt — Zugangsdaten gelöscht, Platz ${slot} wieder frei.`
    );
  } catch (err) {
    await editOriginalResponse(interaction, `Entfernen fehlgeschlagen: ${err.message}`).catch(() => {});
  }
}

/** Bot-Lauf anstoßen und das Ergebnis nachreichen. */
async function runDispatch(interaction, env, state) {
  try {
    await editOriginalResponse(interaction, await triggerRun(env, state));
  } catch (err) {
    await editOriginalResponse(interaction, `\`/run\` fehlgeschlagen: ${err.message}`).catch(() => {});
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
    // Die Rechtevermutung nur dort, wo sie zutreffen kann. Sie pauschal
    // anzuhängen hat schon einmal in die Irre geführt: Bei HTTP 400 (zu langes
    // Embed-Feld) stand da „fehlt dem Bot das Recht" — und die eigentliche
    // Ursache stand darüber, ungelesen.
    const rechte = /HTTP 40[313]/.test(err.message)
      ? "\n\nDarf der Bot in diesem Channel schreiben?"
      : "";
    await editOriginalResponse(
      interaction,
      `\`/panel\` fehlgeschlagen: ${err.message}${rechte}`
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
    // Beide Quellen: config.yml fuer die fest verdrahteten, commands.json fuer
    // die selbst hinterlegten. Sonst steht dort "s3" statt des Namens.
    const labels = { ...(await loadAccountLabels()), ...slotLabels(data.commands) };
    const choices = Object.keys(data.orders.accounts || {})
      .map((key) => ({ name: labels[key] ? `${labels[key]} (${key})` : key, value: key }))
      .filter((c) => passt(c.name) || passt(c.value));
    return autocompleteResult(choices);
  }

  if (path === "account remove") {
    // Nur selbst hinterlegte Konten: die fest in config.yml verdrahteten kann
    // der Worker gar nicht entfernen, sie stünden hier also nur im Weg.
    const choices = Object.entries(data.commands.accounts || {})
      .map(([slot, a]) => ({ name: `${a.label} (Platz ${slot})`, value: slot }))
      .filter((c) => passt(c.name));
    return autocompleteResult(choices);
  }

  if (path === "product add") {
    const eingelesen = data.orders.product_scans || {};
    if (focused?.name === "link") {
      // Schon eingelesene Seiten vorschlagen — der zweite Aufruf ist der
      // haeufigere, und niemand tippt eine Produkt-URL gern zweimal.
      return autocompleteResult(
        Object.entries(eingelesen)
          .filter(([u, s]) => !s.error && (passt(u) || passt(s.title || "")))
          .map(([u, s]) => ({ name: clip(s.title || u, 90), value: u }))
      );
    }
    if (focused?.name === "variante") {
      const url = parseProductLink(optionValues(interaction.data).link || "").url || "";
      const scan = eingelesen[url];
      return autocompleteResult(
        (scan?.variants || []).filter(passt).map((v) => ({ name: clip(v, 90), value: v }))
      );
    }
    return autocompleteResult([]);
  }

  if (path === "product remove") {
    return autocompleteResult(
      Object.entries(data.commands.products || {})
        .filter(([, p]) => passt(p.name || ""))
        .map(([key, p]) => ({ name: clip(p.name, 90), value: key }))
    );
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
          "Aus dem Link lässt sich kein Name ableiten — gib mit `name:` selbst einen an."
        );
      }
      return reply(await addTracking(env, state, label, url, userId));
    }

    case "track remove": {
      const { text } = await removeTracking(env, state, args.name);
      return reply(text);
    }

    case "account add": {
      if (!githubConfigured(env)) {
        return reply("Dem Worker fehlt `GITHUB_TOKEN` — ohne das kann er keine Secrets anlegen.");
      }
      if (freeSlot(state) === null) {
        return reply(
          `Alle ${SLOTS.length} Plätze für selbst hinterlegte Konten sind belegt. Erst eines mit \`/account remove\` freimachen.`
        );
      }
      // Ein Modal MUSS die sofortige Antwort sein — nach einem Defer lässt
      // Discord keines mehr zu.
      return json({ type: InteractionResponseType.MODAL, data: accountAddModal() });
    }

    case "account remove": {
      if (!githubConfigured(env)) {
        return reply("Dem Worker fehlt `GITHUB_TOKEN` — ohne das kann er keine Secrets löschen.");
      }
      return deferTo(runAccountRemove(interaction, env, state, args.konto), ctx);
    }

    case "product list":
      return replyEmbed(await productListView(data.orders, state));

    case "product add": {
      const { url, error } = parseProductLink(args.link);
      if (error) return reply(error);

      const scan = data.orders.product_scans?.[url];
      if (!scan) {
        await requestScan(env, state, url);
        let nachsatz = "Das dauert einen Lauf — ich melde mich hier, sobald ich weiß, was es dort gibt.";
        if (githubConfigured(env)) {
          try {
            await dispatchRun(env);
            state.last_run_at = new Date().toISOString();
            await saveState(env, state);
          } catch {
            nachsatz = "Beim nächsten Lauf lese ich die Seite ein — mit `/run` geht es sofort.";
          }
        }
        return reply(`Angemeldet. Die Seite kenne ich noch nicht.\n\n${nachsatz}`);
      }
      if (scan.error) {
        return reply(`Die Seite war nicht lesbar: _${scan.error}_\n\nStimmt der Link?`);
      }

      // Einzelprodukt: Es gibt nichts auszuwählen, der Titel ist die Bezeichnung.
      if (scan.simple) {
        const schon = await addProduct(env, state, url, scan.title, "");
        return reply(`**${scan.title}** wird ${schon ? "weiterhin" : "ab jetzt"} beobachtet.`);
      }
      if (!args.variante) {
        return reply(
          `**${scan.title}** hat ${scan.variants.length} Varianten. Ruf \`/product add\` nochmal auf und wähl bei \`variante\` aus — die Liste steht jetzt im Autocomplete.`
        );
      }
      if (!scan.variants.includes(args.variante)) {
        return reply(
          `**${args.variante}** gibt es auf der Seite nicht. Wähl bitte aus dem Autocomplete — der Wortlaut muss exakt stimmen, sonst greift der Abgleich ins Leere.`
        );
      }
      const schon = await addProduct(env, state, url, scan.title, args.variante);
      return reply(
        `**${args.variante}** wird ${schon ? "weiterhin" : "ab jetzt"} beobachtet.\n\nAb dem nächsten Lauf steht es im Dashboard, und ein Restock pingt wie gewohnt.`
      );
    }

    case "product remove": {
      const weg = await removeProduct(env, state, args.produkt);
      return reply(
        weg
          ? "Wird nicht mehr beobachtet."
          : "Das ist kein per Command aufgenommenes Produkt. Die fest gepflegten stehen in `config.yml` und bleiben."
      );
    }

    case "run": {
      if (!githubConfigured(env)) {
        return reply(
          "Dem Worker fehlt `GITHUB_TOKEN` — ohne das kann er keinen Lauf anstoßen."
        );
      }
      // Deferred: Der Weg geht über die GitHub-API und danach ins Gist. Einzeln
      // schnell, zusammen aber zu nah an Discords 3-Sekunden-Grenze.
      return deferTo(runDispatch(interaction, env, state), ctx);
    }

    default:
      return reply(`Unbekannter Command: \`/${path}\``);
  }
}

/**
 * Ein abgeschicktes Formular. Läuft durch dieselbe Rollenprüfung wie ein
 * Command — ein Modal ist nur eine zweite Runde derselben Interaktion, und wer
 * die Rolle inzwischen verloren hat, soll auch hier nicht durchkommen.
 */
async function handleModal(interaction, env, ctx) {
  if (interaction.data?.custom_id !== ACCOUNT_ADD) {
    return reply("Unbekanntes Formular.");
  }
  if (!interaction.guild_id || !interaction.member) {
    return reply("Das geht nur in einem Server.");
  }

  let data;
  try {
    data = await loadAll(env);
  } catch (err) {
    return reply(`Stand nicht lesbar: ${err.message}`);
  }
  const roleId = roleIdFor(data.commands, interaction.guild_id);
  if (!roleId || !(interaction.member.roles || []).includes(roleId)) {
    return reply(`Dafür brauchst du die Rolle <@&${roleId || "bgnotify"}>.`);
  }

  const werte = modalValues(interaction.data);
  if (!werte.label || !werte.user || !werte.pass) {
    return reply("Es fehlt ein Feld — bitte `/account add` nochmal aufrufen.");
  }

  return deferTo(runAccountAdd(interaction, env, data.commands, werte), ctx);
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
    if (interaction.type === InteractionType.MODAL_SUBMIT) {
      return handleModal(interaction, env, ctx);
    }
    return new Response("unsupported interaction type", { status: 400 });
  },
};
