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

export function buildEmbed() {
  const all = flatten();
  const ansehen = all.filter((c) => !c.ownerOnly);
  const verwaltung = all.filter((c) => c.ownerOnly);

  // Mit Argumenten in der Zeile — `<pflicht>` und `[freiwillig]`. Sonst müsste
  // man raten, ob ein Command noch etwas braucht.
  const render = (list) =>
    list.map((c) => `**\`${signature(c)}\`**\n${c.help}`).join("\n\n");

  const fields = [];
  if (ansehen.length) {
    fields.push({ name: "⠀\n📋⠀⠀A N S E H E N", value: render(ansehen) });
  }
  if (verwaltung.length) {
    fields.push({
      name: "⠀\n🔧⠀⠀V E R W A L T U N G",
      value: `${render(verwaltung)}\n\n_Nur für den Server-Inhaber._`,
    });
  }

  return {
    title: "✦⠀⠀b g n o t i f y⠀⠀✦",
    description: [
      "Schreib die Befehle einfach hier in den Channel.",
      "",
      "**Die Antworten sieht nur du.** Sie stehen niemandem sonst im Verlauf —",
      "auch dann nicht, wenn Bestell- oder Sendungsdaten darin vorkommen.",
    ].join("\n"),
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
