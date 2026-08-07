/** Anzeige-Helfer. Wortlaut und Einheiten wie in `src/embeds.py`, damit
 *  Discord-Ausgaben von Bot und Worker gleich klingen. */

const TERMINAL = [
  "zugestellt",
  "zustellung erfolgt",
  "ausgeliefert",
  "empfangen",
  "zurückgesendet",
  "retoure",
  "abgeholt",
];

/** Sendung abgeschlossen? Gleiche Wortliste wie `hermes.is_terminal`. */
export function isTerminal(status) {
  const s = (status || "").toLowerCase();
  return TERMINAL.some((t) => s.includes(t));
}

/** „vor 4 min", „vor 3 h", „vor 2 T". Leer, wenn nichts Verwertbares kommt. */
export function ago(iso) {
  if (!iso) return "";
  const then = Date.parse(String(iso).replace(" ", "T"));
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return "gerade eben";
  const min = Math.floor(s / 60);
  if (min < 60) return `vor ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `vor ${h} h`;
  const d = Math.floor(h / 24);
  if (d < 14) return `vor ${d} T`;
  return `vor ${Math.floor(d / 7)} Wo`;
}

/** Kürzt auf `max` Zeichen und hängt „…" an, statt mitten im Wort abzureißen. */
export function clip(text, max) {
  const t = (text || "").trim();
  if (t.length <= max) return t;
  return t.slice(0, max).replace(/\s+\S*$/, "") + " …";
}

/**
 * Discord kappt Embed-Beschreibungen bei 4096 Zeichen und wirft darüber einen
 * Fehler, statt zu kürzen. Bei vielen Sendungen oder Konten ist das erreichbar,
 * deshalb hier eine harte Grenze mit ehrlichem Hinweis statt einer Fehlermeldung.
 */
export function joinLines(lines, limit = 3800) {
  const out = [];
  let len = 0;
  for (const line of lines) {
    if (len + line.length + 1 > limit) {
      out.push(`… und ${lines.length - out.length} weitere`);
      break;
    }
    out.push(line);
    len += line.length + 1;
  }
  return out.join("\n");
}
