/**
 * Der Command-Katalog — eine Quelle, drei Verbraucher.
 *
 * `register.js` baut daraus die Anmeldung bei Discord, `panel.js` die
 * Übersicht im Channel, `index.js` prüft dagegen die Rechte. Käme jede Stelle
 * mit ihrer eigenen Liste, wäre die Übersicht schon nach dem zweiten neuen
 * Command falsch — und zwar unbemerkt, weil nichts sie widerlegt.
 *
 * `help` ist die Langfassung fürs Panel, `description` die Kurzzeile, die
 * Discord beim Tippen einblendet (max. 100 Zeichen, harte Grenze der API).
 */

// Discord-Optionstypen, die hier vorkommen.
export const SUB_COMMAND = 1;
export const STRING = 3;

export const COMMANDS = [
  {
    name: "status",
    description: "Läuft der Bot? Letzter Lauf, Konten, Sendungen",
    help: "Zeigt, wann der Bot zuletzt lief, wie viele Konten aktiv sind, wie viele Sendungen verfolgt werden — und ob beim letzten Lauf Fehler auftraten.",
  },
  {
    name: "track",
    description: "Sendungsverfolgung",
    subcommands: [
      {
        name: "list",
        description: "Verfolgte Sendungen mit letztem Stand",
        help: "Alle Sendungen, die der Bot beobachtet — von Hand eingetragene wie automatisch aus Bestellungen übernommene, jeweils mit letztem Stand und letzter Abfrage.",
      },
      {
        name: "add",
        description: "Hermes-Link eintragen",
        help: "Trägt eine Sendung zur Verfolgung ein. Ab dem nächsten Lauf meldet der Bot jedes neue Ereignis und hört bei Zustellung von selbst auf.",
        options: [
          { name: "link", description: "Hermes-Sendungslink", type: STRING, required: true },
          { name: "name", description: "Anzeigename (sonst wird einer aus dem Link abgeleitet)", type: STRING },
        ],
      },
      {
        name: "remove",
        description: "Sendung nicht mehr verfolgen",
        help: "Entfernt einen von Hand eingetragenen Eintrag. Automatisch übernommene Sendungen verschwinden ohnehin nach der Zustellung.",
        options: [
          {
            name: "name",
            description: "welche Sendung",
            type: STRING,
            required: true,
            autocomplete: true,
          },
        ],
      },
    ],
  },
  {
    name: "account",
    description: "BG-Konten",
    subcommands: [
      {
        name: "list",
        description: "Konten mit An/Aus-Zustand",
        help: "Welche Konten hinterlegt sind, ob sie an oder aus sind, wann sie zuletzt geprüft wurden und wie viele Bestellungen offen sind.",
      },
      {
        name: "add",
        description: "Eigenes BG-Konto hinterlegen",
        help: "Öffnet ein Formular für Zugangsdaten. Die Werte werden sofort verschlüsselt und als GitHub-Secret abgelegt — im Gist stehen nur Anzeigename und Platznummer. Ob der Login stimmt, meldet der Bot beim nächsten Lauf.",
      },
      {
        name: "remove",
        description: "Konto entfernen",
        help: "Löscht die hinterlegten Zugangsdaten und gibt den Platz frei. Nur für selbst hinterlegte Konten — die fest verdrahteten bleiben.",
        options: [
          { name: "konto", description: "welches Konto", type: STRING, required: true, autocomplete: true },
        ],
      },
      {
        name: "enable",
        description: "Konto einschalten",
        help: "Ab dem nächsten Lauf prüft der Bot dieses Konto wieder. Einschalten, wenn du bestellt hast.",
        options: [
          { name: "konto", description: "welches Konto", type: STRING, required: true, autocomplete: true },
        ],
      },
      {
        name: "disable",
        description: "Konto ausschalten",
        help: "Der Bot loggt sich für dieses Konto nicht mehr ein — null Zugriffe, bis du es wieder einschaltest.",
        options: [
          { name: "konto", description: "welches Konto", type: STRING, required: true, autocomplete: true },
        ],
      },
    ],
  },
  {
    name: "run",
    description: "Stößt sofort einen Bot-Lauf an",
    help: "Startet den Bot jetzt, statt auf den nächsten Takt zu warten. Dauert rund eine halbe Minute; die Meldungen kommen wie immer in die Channels.",
  },
  {
    name: "ping",
    description: "Testet, ob der Bot erreichbar ist",
    help: "Antwortet „pong“. Nützlich, um zu sehen, ob der Worker läuft und ob du die nötige Rolle hast.",
  },
  {
    name: "panel",
    description: "Postet oder aktualisiert diese Übersicht",
    help: "Legt die Übersicht in dem Channel an, in dem du den Command aufrufst. Danach hält sie sich selbst aktuell — kommen Commands dazu, wird die Nachricht bearbeitet statt eine neue gepostet.",
    ownerOnly: true,
  },
  {
    name: "setup",
    description: "Richtet die Rolle bgnotify ein (nur der Server-Inhaber)",
    help: "Legt die Rolle `bgnotify` an und gibt sie dir. Wer die Commands benutzen soll, braucht diese Rolle. Gefahrlos wiederholbar.",
    ownerOnly: true,
  },
];

/** Flache Liste `{ path, description, help, ownerOnly, options }` — `path` wie „track list". */
export function flatten() {
  const out = [];
  for (const c of COMMANDS) {
    if (c.subcommands) {
      for (const s of c.subcommands) {
        out.push({
          path: `${c.name} ${s.name}`,
          description: s.description,
          help: s.help,
          options: s.options || [],
          ownerOnly: c.ownerOnly || s.ownerOnly || false,
        });
      }
    } else {
      out.push({
        path: c.name,
        description: c.description,
        help: c.help,
        options: c.options || [],
        ownerOnly: !!c.ownerOnly,
      });
    }
  }
  return out;
}

/** „/track add <link> [name]" — Pflichtoptionen spitz, freiwillige eckig. */
export function signature(entry) {
  const args = entry.options
    .map((o) => (o.required ? `<${o.name}>` : `[${o.name}]`))
    .join(" ");
  return `/${entry.path}${args ? " " + args : ""}`;
}

/**
 * Fingerabdruck des Katalogs. Ändert er sich, ist das Panel veraltet und wird
 * beim nächsten Command automatisch neu gezeichnet — deshalb muss der Wert
 * allein vom Inhalt abhängen und über Deploys hinweg stabil sein.
 * djb2, absichtlich simpel: Es geht um „gleich oder nicht", nicht um Kryptografie.
 */
export function catalogVersion() {
  const raw = JSON.stringify(COMMANDS);
  let h = 5381;
  for (let i = 0; i < raw.length; i++) h = ((h << 5) + h + raw.charCodeAt(i)) >>> 0;
  return h.toString(16);
}

/** Anmelde-Format für Discord. Untercommands werden zu Options vom Typ 1. */
export function toDiscordPayload() {
  return COMMANDS.map((c) => {
    const base = {
      name: c.name,
      description: c.description,
      // Blendet den Command bei allen ohne Adminrechte aus. NUR Sichtbarkeit —
      // verbindlich prüft der Worker anhand der Rolle.
      default_member_permissions: "0",
    };
    if (c.subcommands) {
      base.options = c.subcommands.map((s) => ({
        type: SUB_COMMAND,
        name: s.name,
        description: s.description,
        ...(s.options ? { options: s.options } : {}),
      }));
    } else if (c.options) {
      base.options = c.options;
    }
    return base;
  });
}
