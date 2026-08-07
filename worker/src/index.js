/**
 * Discord-Interactions-Endpoint auf Cloudflare Workers.
 *
 * Discord schickt jeden Slash-Command als HTTPS-POST hierher — es braucht also
 * KEINEN dauerlaufenden Bot-Prozess (und damit keinen gemieteten Server). Der
 * Worker beantwortet den Command und schreibt später ins private Gist; gepostet
 * wird weiterhin über die bestehenden Webhooks des Actions-Bots.
 *
 * Schritt 1 dieses Umbaus: nur Signaturprüfung + /ping. Das ist bewusst die
 * erste Baustelle, weil Discord die Endpoint-URL gar nicht erst akzeptiert,
 * wenn die Prüfung nicht stimmt — alles andere baut darauf auf.
 *
 * Erwartete Secrets (via `wrangler secret put`):
 *   DISCORD_PUBLIC_KEY   Public Key der Discord-App (hex)
 */

const InteractionType = { PING: 1, APPLICATION_COMMAND: 2 };
const InteractionResponseType = { PONG: 1, CHANNEL_MESSAGE_WITH_SOURCE: 4 };
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

async function handleCommand(interaction) {
  const name = interaction.data?.name;

  switch (name) {
    case "ping": {
      // Bewusst nutzlos: beweist nur, dass Signaturprüfung und Routing stehen.
      return reply("pong — Signaturprüfung steht, Endpoint läuft.");
    }
    default:
      return reply(`Unbekannter Command: \`/${name}\``);
  }
}

export default {
  async fetch(request, env) {
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
      return handleCommand(interaction);
    }
    return new Response("unsupported interaction type", { status: 400 });
  },
};
