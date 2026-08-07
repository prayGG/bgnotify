/**
 * Meldet die Slash-Commands bei Discord an. Einmal ausführen, und danach immer
 * dann wieder, wenn Namen, Optionen oder Beschreibungen sich ändern.
 *
 *   DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... [DISCORD_GUILD_ID=...] node register.js
 *
 * Mit DISCORD_GUILD_ID werden die Commands nur für diesen einen Server
 * registriert — die sind SOFORT da. Ohne die Variable werden sie global
 * registriert, was bis zu einer Stunde dauern kann. Beim Entwickeln also immer
 * mit Guild-ID arbeiten.
 *
 * Die Liste selbst steht in `src/catalog.js` und wird von dort auch fuer die
 * Uebersicht im Channel benutzt. Zwei getrennte Listen waeren schon nach dem
 * zweiten neuen Command auseinandergelaufen — unbemerkt, weil nichts sie
 * widerlegt haette.
 */

import { flatten, toDiscordPayload } from "./src/catalog.js";

const APP_ID = process.env.DISCORD_APP_ID;
const BOT_TOKEN = process.env.DISCORD_BOT_TOKEN;
const GUILD_ID = process.env.DISCORD_GUILD_ID || "";

async function main() {
  if (!APP_ID || !BOT_TOKEN) {
    console.error("DISCORD_APP_ID und DISCORD_BOT_TOKEN müssen gesetzt sein.");
    process.exit(2);
  }

  const url = GUILD_ID
    ? `https://discord.com/api/v10/applications/${APP_ID}/guilds/${GUILD_ID}/commands`
    : `https://discord.com/api/v10/applications/${APP_ID}/commands`;

  const res = await fetch(url, {
    method: "PUT", // PUT ersetzt die komplette Liste — entfernte Commands verschwinden
    headers: {
      Authorization: `Bot ${BOT_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(toDiscordPayload()),
  });

  const text = await res.text();
  if (!res.ok) {
    console.error(`Fehlgeschlagen (HTTP ${res.status}):\n${text}`);
    process.exit(1);
  }

  const registered = JSON.parse(text);
  console.log(
    `${registered.length} Command(s) registriert (${GUILD_ID ? `Guild ${GUILD_ID}` : "global"}):`
  );
  for (const c of flatten()) console.log(`  /${c.path} — ${c.description}`);
  if (!GUILD_ID) console.log("\nGlobal registriert — kann bis zu einer Stunde dauern.");
  console.log("\nDanach /panel aufrufen, damit die Übersicht im Channel den neuen Stand zeigt.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
