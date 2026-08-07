/**
 * Die Befehlsübersicht, die dauerhaft oben im Channel steht.
 *
 * Gebaut wird sie ausschließlich aus `catalog.js`. Damit ist der Fall
 * ausgeschlossen, der solche Übersichten sonst unbrauchbar macht: dass jemand
 * einen Command ergänzt und das Panel vergisst. Ändert sich der Katalog,
 * ändert sich sein Fingerabdruck — und beim nächsten Command wird die
 * bestehende Nachricht **bearbeitet** statt eine zweite gepostet.
 */

import { catalogVersion, flatten, signature } from "./catalog.js";
import { createMessage, editMessage } from "./discord.js";
import { saveState } from "./gist.js";

const COLOR = 0x5865f2; // Discord-Blurple, wie die Stats-Karte des Bots

// Harte Grenzen von Discord. Wer sie reißt, bekommt keinen gekürzten Text,
// sondern HTTP 400 — die Nachricht geht gar nicht erst raus.
const FIELD_MAX = 1024;
const FIELDS_MAX = 25;

/**
 * Zeilen als Felder anhängen und dabei bei 1024 Zeichen umbrechen.
 *
 * Vorher stand jede Gruppe in EINEM Feld. Das ging so lange gut, bis mit
 * `/product` der zwölfte Command dazukam und die Übersicht mit HTTP 400
 * liegenblieb — ohne dass irgendetwas vorher gewarnt hätte. Genau diese Sorte
 * Fehler wächst still mit: Jeder neue Command bringt sie näher.
 *
 * Folgefelder tragen einen leeren Namen, damit die Überschrift nicht bei jedem
 * Umbruch wiederholt wird.
 */
function push(fields, name, lines) {
  let block = [];
  let laenge = 0;
  const abgeben = () => {
    if (!block.length || fields.length >= FIELDS_MAX) return;
    fields.push({ name: fields.length && name === "" ? "⠀" : name, value: block.join("\n\n") });
    name = "⠀";
    block = [];
    laenge = 0;
  };

  for (const line of lines) {
    if (laenge + line.length + 2 > FIELD_MAX) abgeben();
    block.push(line);
    laenge += line.length + 2;
  }
  abgeben();
}

export function buildEmbed() {
  const all = flatten();
  const ansehen = all.filter((c) => !c.ownerOnly);
  const verwaltung = all.filter((c) => c.ownerOnly);

  // Mit Argumenten in der Zeile — `<pflicht>` und `[freiwillig]`. Sonst müsste
  // man raten, ob ein Command noch etwas braucht.
  const render = (c) => `**\`${signature(c)}\`**\n${c.help}`;

  const fields = [];
  if (ansehen.length) push(fields, "⠀\n📋⠀⠀A N S E H E N", ansehen.map(render));
  if (verwaltung.length) {
    push(fields, "⠀\n🔧⠀⠀V E R W A L T U N G", [
      ...verwaltung.map(render),
      "_Nur für den Server-Inhaber._",
    ]);
  }

  return {
    title: "✦⠀⠀b g n o t i f y⠀⠀✦",
    description: "Befehle einfach hier in den Channel schreiben. **Die Antworten sieht nur du.**",
    color: COLOR,
    fields,
    footer: { text: `hält sich selbst aktuell · Stand ${catalogVersion()}` },
    timestamp: new Date().toISOString(),
  };
}

/**
 * Panel anlegen oder auffrischen und den Verweis im Gist ablegen.
 *
 * Wurde die Nachricht im Channel gelöscht, scheitert das Bearbeiten mit 404 —
 * dann wird eine neue gepostet, statt den Command mit einem Fehler zu beenden.
 */
export async function publish(env, state, guildId, channelId) {
  const embed = buildEmbed();
  const entry = (state.guilds[guildId] ||= {});
  const panel = entry.panel || {};

  let messageId = "";
  if (panel.channel_id === channelId && panel.message_id) {
    try {
      await editMessage(env, channelId, panel.message_id, embed);
      messageId = panel.message_id;
    } catch {
      messageId = ""; // Nachricht weg → unten neu anlegen
    }
  }
  if (!messageId) {
    messageId = (await createMessage(env, channelId, embed)).id;
  }

  entry.panel = { channel_id: channelId, message_id: messageId, version: catalogVersion() };
  await saveState(env, state);
  return { messageId, recreated: messageId !== panel.message_id };
}

/**
 * Nach jedem Command: Steht ein Panel und ist es veraltet, im Hintergrund
 * nachziehen. Kostet im Normalfall nichts — der Vergleich ist ein
 * String-Vergleich, geschrieben wird nur nach einer echten Katalogänderung.
 */
export function refreshIfStale(env, state, guildId, ctx) {
  const panel = state.guilds?.[guildId]?.panel;
  if (!panel?.message_id || panel.version === catalogVersion()) return;

  const work = publish(env, state, guildId, panel.channel_id).catch(() => {
    // Stillschweigend aufgeben: Das Panel ist Beiwerk. Wer gerade einen
    // Command abgesetzt hat, soll dafür keine Fehlermeldung bekommen.
  });
  if (ctx?.waitUntil) ctx.waitUntil(work);
}
