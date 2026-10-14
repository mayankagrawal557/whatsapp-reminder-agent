/**
 * Arovida PET-CT Centre — WhatsApp Web QR Gateway Bridge.
 * Powered by @whiskeysockets/baileys.
 * 
 * Provides a local HTTP REST bridge for WhatsApp Multi-Device Web connection:
 * - GET  /status : Returns connection state, user phone, and live QR code data URL.
 * - POST /start  : Initializes Baileys connection and generates QR code.
 * - POST /send   : Dispatches a message to a recipient phone via linked WhatsApp.
 * - POST /logout : Disconnects session and clears local credentials.
 */

import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcode from 'qrcode';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const AUTH_DIR = path.join(__dirname, 'auth_info_baileys');
const PORT = process.env.WA_BRIDGE_PORT || 3001;

// Global process exception handlers to prevent Baileys decryption crashes
process.on('uncaughtException', (err) => {
  console.error('[WA-WEB-BRIDGE] Uncaught Exception:', err && err.message ? err.message : err);
});
process.on('unhandledRejection', (reason) => {
  console.error('[WA-WEB-BRIDGE] Unhandled Rejection:', reason && reason.message ? reason.message : reason);
});

// Global Bridge State
let sock = null;
let currentQR = null;
let connectionStatus = 'disconnected'; // 'disconnected' | 'connecting' | 'qr_ready' | 'connected'
let connectedUser = null;
let connectedAt = null;
let isStarting = false;

const logger = pino({ level: 'error' });

/**
 * Initialize Baileys Multi-Device Socket
 */
async function startSocket(forceRestart = false) {
  if (sock && !forceRestart && (connectionStatus === 'connected' || connectionStatus === 'qr_ready')) {
    return;
  }

  isStarting = true;
  connectionStatus = 'connecting';
  currentQR = null;

  try {
    if (!fs.existsSync(AUTH_DIR)) {
      fs.mkdirSync(AUTH_DIR, { recursive: true });
    }

    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: [2, 3000, 1015901307] }));

    sock = makeWASocket({
      version,
      auth: state,
      logger,
      printQRInTerminal: false,
      browser: ['Arovida PET-CT Engine', 'Chrome', '120.0.0.0'],
      syncFullHistory: false,
      generateHighQualityLinkPreview: false,
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        try {
          currentQR = await qrcode.toDataURL(qr, { width: 320, margin: 2 });
          connectionStatus = 'qr_ready';
          console.log('[WA-WEB-BRIDGE] QR code generated successfully. Ready for scan.');
        } catch (err) {
          console.error('[WA-WEB-BRIDGE] Error converting QR to data URL:', err);
        }
      }

      if (connection === 'close') {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
        console.log(`[WA-WEB-BRIDGE] Connection closed (status: ${statusCode}). Reconnecting: ${shouldReconnect}`);

        currentQR = null;
        connectedUser = null;

        if (statusCode === DisconnectReason.loggedOut) {
          connectionStatus = 'disconnected';
          cleanupAuth();
        } else {
          connectionStatus = 'connecting';
          setTimeout(() => startSocket(true), 3000);
        }
      } else if (connection === 'open') {
        connectionStatus = 'connected';
        currentQR = null;
        connectedAt = new Date().toISOString();

        const userJid = sock.user?.id || '';
        const phone = userJid.split(':')[0] || userJid.split('@')[0];
        connectedUser = {
          jid: userJid,
          phone: phone ? (phone.startsWith('+') ? phone : `+${phone}`) : 'Linked Device',
          name: sock.user?.name || 'Arovida Broadcast Session',
        };

        console.log(`[WA-WEB-BRIDGE] ✓ Successfully connected as: ${connectedUser.phone} (${connectedUser.name})`);
      }
    });

  } catch (err) {
    console.error('[WA-WEB-BRIDGE] Error starting socket:', err);
    connectionStatus = 'disconnected';
  } finally {
    isStarting = false;
  }
}

/**
 * Remove saved auth credentials
 */
function cleanupAuth() {
  try {
    if (fs.existsSync(AUTH_DIR)) {
      fs.rmSync(AUTH_DIR, { recursive: true, force: true });
    }
  } catch (err) {
    console.error('[WA-WEB-BRIDGE] Error cleaning auth directory:', err);
  }
}

/**
 * Clean phone number for WhatsApp Web JID (e.g. "+91 98765 43210" -> "919876543210@s.whatsapp.net")
 */
function formatRecipientJid(phone) {
  let digits = String(phone).replace(/\D/g, '');
  if (digits.length === 10) {
    digits = `91${digits}`;
  }
  return `${digits}@s.whatsapp.net`;
}

// =============================================================================
// HTTP SERVER & REST API
// =============================================================================
const server = http.createServer(async (req, res) => {
  // CORS Headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = url.pathname;

  // JSON Helper
  const sendJson = (statusCode, data) => {
    res.writeHead(statusCode, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
  };

  // 1. GET /status or /health
  if (req.method === 'GET' && (pathname === '/status' || pathname === '/health')) {
    sendJson(200, {
      status: connectionStatus,
      is_starting: isStarting,
      qr_code: currentQR,
      user: connectedUser,
      connected_at: connectedAt,
      has_auth: fs.existsSync(AUTH_DIR) && fs.readdirSync(AUTH_DIR).length > 0
    });
    return;
  }

  // 2. POST /start (Generate QR / Connect)
  if (req.method === 'POST' && pathname === '/start') {
    startSocket(true);
    sendJson(200, {
      status: connectionStatus,
      message: 'Socket connection process triggered. Awaiting QR code generation or auto-reconnect.'
    });
    return;
  }

  // 3. POST /send (Dispatch Text Message)
  if (req.method === 'POST' && pathname === '/send') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', async () => {
      try {
        const payload = JSON.parse(body || '{}');
        const to = payload.to;
        const message = payload.message;

        if (!to || !message) {
          sendJson(400, { success: false, error: 'Both "to" and "message" are required.' });
          return;
        }

        if (connectionStatus !== 'connected' || !sock) {
          sendJson(400, {
            success: false,
            error: `WhatsApp Web session is not connected (current status: ${connectionStatus}). Please scan the QR code first.`
          });
          return;
        }

        const jid = formatRecipientJid(to);
        const result = await sock.sendMessage(jid, { text: message });

        sendJson(200, {
          success: true,
          message_id: result?.key?.id || `web.${Date.now()}`,
          to: to,
          jid: jid,
          timestamp: new Date().toISOString()
        });
      } catch (err) {
        console.error('[WA-WEB-BRIDGE] Send error:', err);
        sendJson(500, { success: false, error: err.message || 'Failed to dispatch message' });
      }
    });
    return;
  }

  // 4. POST /logout
  if (req.method === 'POST' && pathname === '/logout') {
    try {
      if (sock) {
        await sock.logout().catch(() => {});
        sock = null;
      }
      cleanupAuth();
      connectionStatus = 'disconnected';
      currentQR = null;
      connectedUser = null;
      sendJson(200, { success: true, message: 'Logged out successfully.' });
    } catch (err) {
      sendJson(500, { success: false, error: err.message });
    }
    return;
  }

  // 404
  sendJson(404, { error: 'Not Found' });
});

// Auto-start socket if existing auth files are found
if (fs.existsSync(AUTH_DIR) && fs.readdirSync(AUTH_DIR).length > 0) {
  console.log('[WA-WEB-BRIDGE] Saved session found. Auto-reconnecting...');
  startSocket(false);
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`=======================================================`);
  console.log(`[WA-WEB-BRIDGE] Listening on http://127.0.0.1:${PORT}`);
  console.log(`=======================================================`);
});
