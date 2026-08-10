/**
 * Der Command-Katalog — eine Quelle, drei Verbraucher.
 *
 * `register.js` baut daraus die Anmeldung bei Discord, `panel.js` die
 * Übersicht im Channel, `index.js` prüft dagegen die Rechte. Käme jede Stelle
 * mit ihrer eigenen Liste, wäre die Übersicht schon nach dem zweiten neuen
 * Command falsch — und zwar unbemerkt, weil nichts sie widerlegt.
 *
 * `description` ist die Kurzzeile, die Discord beim Tippen einblendet (max. 100
 * Zeichen, harte Grenze der API). `help` steht im Panel und bleibt bewusst
 * knapp: EINE Zeile, die sagt was passiert — nicht warum. Das Warum gehört in
 * die Kommentare im Code, nicht in eine Übersicht, die jeden Tag jemand
 * überfliegt.
 */

// Discord-Optionstypen, die hier vorkommen.
export const SUB_COMMAND = 1;
export const STRING = 3;
export const INTEGER = 4;

const KONTO = { name: "konto", description: "welches Konto", type: STRING, required: true, autocomplete: true };

export const COMMANDS = [
  {
    name: "status",
    description: "Läuft der Bot? Letzter Lauf, Konten, Sendungen",
    help: "Läuft der Bot, wie viele Konten aktiv sind, was unterwegs ist.",
  },
  {
    name: "track",
    description: "Sendungsverfolgung",
    subcommands: [
      {
        name: "list",
        description: "Verfolgte Sendungen mit letztem Stand",
        help: "Verfolgte Sendungen mit letztem Stand.",
      },
      {
        name: "add",
        description: "Hermes-Link eintragen",
        help: "Hermes-Link eintragen. Meldet jedes Ereignis, hört bei Zustellung auf.",
        options: [
          { name: "link", description: "Hermes-Sendungslink", type: STRING, required: true },
          { name: "name", description: "Anzeigename (sonst wird einer aus dem Link abgeleitet)", type: STRING },
        ],
      },
      {
        name: "remove",
        description: "Sendung nicht mehr verfolgen",
        help: "Sendung nicht mehr verfolgen.",
        options: [
          { name: "name", description: "welche Sendung", type: STRING, required: true, autocomplete: true },
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
        help: "Konten mit An/Aus, letztem Login und offenen Bestellungen.",
      },
      {
        name: "add",
        description: "Eigenes BG-Konto hinterlegen",
        help: "Eigenes Konto hinterlegen — verschlüsselt, und sofort eingeschaltet.",
      },
      {
        name: "remove",
        description: "Konto entfernen",
        help: "Eigenes Konto samt Zugangsdaten löschen.",
        options: [KONTO],
      },
      {
        name: "enable",
        description: "Konto einschalten",
        help: "Konto einschalten — wird ab dem nächsten Lauf geprüft.",
        options: [KONTO],
      },
      {
        name: "disable",
        description: "Konto ausschalten",
        help: "Konto ausschalten — keine Logins mehr.",
        options: [KONTO],
      },
    ],
  },
  {
    name: "product",
    description: "Beobachtete Produkte",
    subcommands: [
      {
        name: "list",
        description: "Was beobachtet wird",
        help: "Was beobachtet wird.",
      },
      {
        name: "add",
        description: "Produkt aufnehmen",
        help: "Produkt aufnehmen. Varianten kommen als Auswahlmenü in die Antwort.",
        options: [
          { name: "link", description: "Produktseite bei bgpharmadrugs.to", type: STRING, required: true, autocomplete: true },
          { name: "variante", description: "welche Variante (nach dem Einlesen)", type: STRING, autocomplete: true },
        ],
      },
      {
        name: "move",
        description: "Position im Dashboard setzen",
        help: "Reihenfolge im Dashboard — kleiner heißt weiter oben.",
        options: [
          { name: "produkt", description: "welches Produkt", type: STRING, required: true, autocomplete: true },
          { name: "position", description: "kleiner = weiter oben (Standard 100)", type: INTEGER, required: true },
        ],
      },
      {
        name: "rename",
        description: "Anzeige-Name eines Produkts ändern",
        help: "Kürzeren Namen fürs Dashboard vergeben — die Überwachung bleibt.",
        options: [
          { name: "produkt", description: "welches Produkt", type: STRING, required: true, autocomplete: true },
          { name: "name", description: "wie es heißen soll", type: STRING, required: true },
        ],
      },
      {
        name: "remove",
        description: "Produkt nicht mehr beobachten",
        help: "Produkt nicht mehr beobachten.",
        options: [
          { name: "produkt", description: "welches Produkt", type: STRING, required: true, autocomplete: true },
        ],
      },
    ],
  },
  {
    name: "run",
    description: "Stößt sofort einen Bot-Lauf an",
    help: "Startet den Bot sofort, statt auf den Takt zu warten.",
  },
  {
    name: "ping",
    description: "Testet, ob der Bot erreichbar ist",
    help: "Testet, ob der Bot erreichbar ist.",
  },
  {
    name: "panel",
    description: "Postet oder aktualisiert diese Übersicht",
    help: "Postet diese Übersicht. Sie hält sich danach selbst aktuell.",
    ownerOnly: true,
  },
  {
    name: "setup",
    description: "Richtet die Rolle bgnotify ein (nur der Server-Inhaber)",
    help: "Legt die Rolle `bgnotify` an und gibt sie dir.",
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
