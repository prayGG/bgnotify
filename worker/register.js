/**
 * Meldet die Slash-Commands bei Discord an. Einmal ausführen, und danach immer
 * dann wieder, wenn sich Namen, Optionen oder Beschreibungen ändern.
 *
 *   DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... [DISCORD_GUILD_ID=...] node register.js
 *
 * Mit DISCORD_GUILD_ID werden die Commands nur für diesen einen Server
 * registriert — die sind SOFORT da. Ohne die Variable werden sie global
 * registriert, was bis zu einer Stunde dauern kann. Beim Entwickeln also immer
 * mit Guild-ID arbeiten.
 */

const APP_ID = process.env.DISCORD_APP_ID;
const BOT_TOKEN = process.env.DISCORD_BOT_TOKEN;
const GUILD_ID = process.env.DISCORD_GUILD_ID || "";

// Schritt 2: /ping und /setup. Die echten Commands kommen Schritt für Schritt dazu.
//
// `default_member_permissions: "0"` blendet den Command bei allen aus, die keine
// Adminrechte haben. Das ist ausdruecklich NUR Sichtbarkeit — die verbindliche
// Pruefung macht der Worker anhand der Rolle `bgnotify`.
const commands = [
  {
    name: "ping",
    description: "Testet, ob der Bot erreichbar ist",
    default_member_permissions: "0",
  },
  {
    name: "setup",
    description: "Richtet die Rolle bgnotify ein (nur der Server-Inhaber)",
    default_member_permissions: "0",
  },
];

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
    body: JSON.stringify(commands),
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
  for (const c of registered) console.log(`  /${c.name} — ${c.description}`);
  if (!GUILD_ID) console.log("\nGlobal registriert — kann bis zu einer Stunde dauern.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
