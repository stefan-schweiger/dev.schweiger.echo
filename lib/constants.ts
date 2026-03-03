export const SUCCESS_HTML = `<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <title>Alexa Connected</title>
    <style type="text/css">
      * { margin: 0; padding: 0; }
      html, body { height: 100%; font-family: 'Arial' }
      body { display: flex; align-items: center; justify-content: center; flex-direction: column;
        color: light-dark(#FFF, #000): background: light-dark(#000, #FFF); gap: 2rem; }
    </style>
  </head>
  <body>
    <img src="https://etc.athom.com/logo/transparent/64.png" alt="logo" />
    <p>Successfully authorized. You can close this window.</p>
  </body>
</html>
`;

export const SERVERS: Record<string, string> = {
  'amazon.com': 'alexa.amazon.com',
  'amazon.co.uk': 'alexa.amazon.co.uk',
  'amazon.ca': 'alexa.amazon.ca',
  'amazon.com.au': 'alexa.amazon.com.au',
  'amazon.de': 'alexa.amazon.de',
  'amazon.es': 'alexa.amazon.es',
  'amazon.fr': 'alexa.amazon.fr',
  'amazon.it': 'alexa.amazon.it',
  'amazon.nl': 'alexa.amazon.nl',
  // for whatever reason no alexa.amazon.se alias exists
  'amazon.se': 'layla.amazon.com',
  // for whatever reason no alexa.amazon.pl alias exists
  'amazon.pl': 'layla.amazon.com',
  // for whatever reason no alexa.amazon.dk alias exists
  'amazon.dk': 'layla.amazon.dk',
  'amazon.co.jp': 'alexa.amazon.co.jp',
  'amazon.in': 'alexa.amazon.in',
  'amazon.com.br': 'alexa.amazon.com.br',
  'amazon.com.mx': 'alexa.amazon.com.mx',
};

export const LANG_MAP: Record<string, string> = {
  de: 'de-DE',
  en: 'en-US',
  es: 'es-ES',
  fr: 'fr-FR',
  it: 'it-IT',
  ja: 'ja-JP',
  nl: 'nl-NL',
  pl: 'pl-PL',
  sv: 'sv-SE',
  in: 'hi-IN',
  no: 'no-NO',
  da: 'da-DK',
};

/**
 * Known error strings from alexa-remote2 and Node.js network layer.
 * Values must match exactly what the library/runtime throws.
 */
export const ERROR_CODES = {
  UNKNOWN_DEVICE: 'Unknown Device or Serial number',
  COOKIE_RENEWAL_FAILED: 'Cookie invalid, Renew unsuccessful',
  HTTP_UNAUTHORIZED: '401 Unauthorized',
  REMOTE_CONNECTION_CLOSED: 'Connection Closed',
  REQUEST_TIMEOUT: 'Timeout',
  READ_ETIMEDOUT: 'read ETIMEDOUT',
  DNS_LOOKUP_FAILED: 'getaddrinfo ENOTFOUND',
  SOCKET_TERMINATED: 'socket hang up',
  CONN_RESET: 'ECONNRESET',
  EMPTY_RESPONSE: 'no body',
  HTTP2_SESSION_INVALID: 'ERR_HTTP2_INVALID_SESSION',
} as const;

export const DEVICES: Record<string, { name: string; generation: number }> = {
  AB72C64C86AW2: { name: 'Echo', generation: 1 },
  A7WXQPH584YP: { name: 'Echo', generation: 2 },
  A3FX4UWTP28V1P: { name: 'Echo', generation: 3 },
  A3RMGO6LYLH7YN: { name: 'Echo', generation: 4 },
  A38EHHIB10L47V: { name: 'Echo Dot', generation: 1 },
  AKNO1N0KSFN8L: { name: 'Echo Dot', generation: 1 },
  A3S5BH2HU6VAYF: { name: 'Echo Dot', generation: 2 },
  A1RABVCI4QCIKC: { name: 'Echo Dot', generation: 3 },
  A30YDR2MK8HMRV: { name: 'Echo Dot', generation: 3 },
  A32DOYMUN6DTXA: { name: 'Echo Dot', generation: 3 },
  A2H4LV5GIZ1JFT: { name: 'Echo Dot', generation: 4 },
  A2U21SRK4QGSE1: { name: 'Echo Dot', generation: 4 },
  A2DS1Q2TPDJ48U: { name: 'Echo Dot', generation: 5 },
  A4ZXE0RM7LQ7A: { name: 'Echo Dot', generation: 5 },
  A2M35JJZWCQOMZ: { name: 'Echo Plus', generation: 1 },
  A18O6U1UQFJ0XK: { name: 'Echo Plus', generation: 2 },
  A4ZP7ZC4PI6TO: { name: 'Echo Show 5', generation: 1 },
  A1XWJRHALS1REP: { name: 'Echo Show 5', generation: 2 },
  A11QM4H9HGV71H: { name: 'Echo Show 5', generation: 3 },
  A1Z88NGR2BK6A2: { name: 'Echo Show 8', generation: 1 },
  A15996VY63BQ2D: { name: 'Echo Show 8', generation: 2 },
  A2UONLFQW0PADH: { name: 'Echo Show 8', generation: 3 },
  AWZZ5CVHX2CD: { name: 'Echo Show', generation: 2 },
  // Echo Show was rebranded as Echo Show 10
  AIPK7MM90V7TB: { name: 'Echo Show 10', generation: 3 },
  ASQZWP4GPYUT7: { name: 'Echo Pop', generation: 1 },
  A10A33FOX2NUBK: { name: 'Echo Spot', generation: 1 },
  A3EH2E0YZ30OD6: { name: 'Echo Spot', generation: 2 },
};
