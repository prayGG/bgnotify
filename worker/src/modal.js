/**
 * Das Formular für `/account add`.
 *
 * WARUM EIN MODAL UND KEINE COMMAND-OPTION: Ein Passwort als Option stünde im
 * Eingabefeld des Channels, in der Command-Historie des Discord-Clients und in
 * jedem Autocomplete-Vorschlag danach. Ein Modal ist ein eigenes Fenster; der
 * Wert wird einmal übertragen und taucht nirgends wieder auf.
 *
 * EHRLICHE GRENZE: Discord kennt kein maskiertes Eingabefeld. Beim Tippen ist
 * das Passwort im Modal sichtbar — bei laufendem Screenshare also auch für
 * andere. Dagegen hilft nur, es nicht währenddessen einzugeben.
 */

export const ACCOUNT_ADD = "account_add";

const ACTION_ROW = 1;
const TEXT_INPUT = 4;
const SHORT = 1;

const feld = (custom_id, label, placeholder, extra = {}) => ({
  type: ACTION_ROW,
  components: [
    {
      type: TEXT_INPUT,
      custom_id,
      label,
      style: SHORT,
      required: true,
      placeholder,
      ...extra,
    },
  ],
});

export function accountAddModal() {
  return {
    custom_id: ACCOUNT_ADD,
    title: "BG-Konto hinterlegen",
    components: [
      feld("label", "Anzeigename", "kurzer Name — steht später auf deinen Karten", { max_length: 24 }),
      feld("user", "BG-Benutzername oder E-Mail", "dein Login bei bgpharmadrugs.to", { max_length: 120 }),
      feld("pass", "BG-Passwort", "wird sofort verschlüsselt", { max_length: 120 }),
    ],
  };
}

/** Die eingegebenen Werte aus dem abgeschickten Modal ziehen. */
export function modalValues(data) {
  const out = {};
  for (const row of data?.components || []) {
    for (const c of row.components || []) {
      out[c.custom_id] = (c.value || "").trim();
    }
  }
  return out;
}
