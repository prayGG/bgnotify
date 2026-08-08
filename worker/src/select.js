/**
 * Das Auswahlmenü für `/product add`.
 *
 * Vorher brauchte ein Produkt mit Varianten ZWEI Aufrufe: einmal, um die Seite
 * einlesen zu lassen, und danach nochmal mit `variante:` — die Liste stand
 * dann im Autocomplete. Das funktionierte, verlangte aber, dass man den
 * Command ein zweites Mal von Hand zusammensetzt, und zwar mit demselben Link.
 *
 * Jetzt kommt die Liste als Dropdown direkt in die Antwort. Ein Klick genügt,
 * und der Wortlaut kann gar nicht mehr danebenliegen: Was Discord zurückmeldet,
 * ist exakt der Wert, der hier hineingeschrieben wurde. Der Weg über
 * `variante:` bleibt daneben bestehen — er ist die Rückfallebene für die Fälle,
 * die unten in den harten Grenzen aufschlagen.
 */

import { clip } from "./format.js";

// Harte Grenzen von Discord. Wer sie reißt, bekommt keine gekürzte Antwort,
// sondern HTTP 400 — die Nachricht geht gar nicht erst raus.
const CUSTOM_ID_MAX = 100;
const VALUE_MAX = 100;
const LABEL_MAX = 100;
const OPTIONS_MAX = 25;

const ACTION_ROW = 1;
const STRING_SELECT = 3;

/** Kennung des Menüs. Der Doppelpunkt trennt nur das Präfix ab — die URL
 *  enthält selbst welche und wird beim Lesen wieder zusammengesetzt. */
export const PRODUCT_PICK = "pv";

export const pickCustomId = (url) => `${PRODUCT_PICK}:${url}`;

/** Die URL aus der Kennung zurückholen, oder "" wenn es nicht unser Menü ist. */
export function parsePick(customId) {
  const teile = String(customId || "").split(":");
  if (teile.shift() !== PRODUCT_PICK) return "";
  return teile.join(":");
}

/**
 * Das Menü bauen — oder `null`, wenn es nicht in Discords Grenzen passt.
 *
 * `null` ist hier kein Fehler, sondern ein Hinweis an den Aufrufer, den alten
 * Weg über `variante:` anzubieten. Lieber ein Command, den man zweimal tippt,
 * als eine Antwort, die Discord mit HTTP 400 verwirft.
 *
 * `shown`/`total` sagen, ob etwas fehlt: Über 25 Einträge nimmt Discord nicht
 * an, und ein Varianten-Wortlaut jenseits von 100 Zeichen kann kein Wert sein.
 * Verschwiegen wird das nicht — sonst sähe eine gekürzte Liste aus wie die
 * ganze.
 */
export function productSelect(url, variants) {
  const custom_id = pickCustomId(url);
  if (custom_id.length > CUSTOM_ID_MAX) return null;

  const passend = (variants || []).filter((v) => v && v.length <= VALUE_MAX);
  if (!passend.length) return null;

  const gezeigt = passend.slice(0, OPTIONS_MAX);
  return {
    row: {
      type: ACTION_ROW,
      components: [
        {
          type: STRING_SELECT,
          custom_id,
          placeholder: "Variante wählen",
          options: gezeigt.map((v) => ({ label: clip(v, LABEL_MAX), value: v })),
        },
      ],
    },
    shown: gezeigt.length,
    total: (variants || []).length,
  };
}
