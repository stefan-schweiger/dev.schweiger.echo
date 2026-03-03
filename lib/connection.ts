import net from 'node:net';
import { ERROR_CODES } from './constants';

export enum ConnectionState {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  RECONNECTING = 'reconnecting',
  ERROR = 'error',
}

export type ErrorType = keyof typeof ERROR_CODES;

export type ErrorCategory = 'auth' | 'network' | 'transient' | 'unknown';

export type CategorizedError = {
  type: ErrorType | 'UNKNOWN';
  category: ErrorCategory;
  message: string;
  shouldRetry: boolean;
};

/**
 * Categorize an error from alexa-remote2 or Node.js networking.
 * Returns structured info including whether a retry makes sense.
 */
export function categorizeError(e: unknown): CategorizedError {
  const errorStr = String(e);

  // Auth errors — do NOT retry automatically
  if (errorStr.startsWith(ERROR_CODES.COOKIE_RENEWAL_FAILED)) {
    return { type: 'COOKIE_RENEWAL_FAILED', category: 'auth', message: 'Cookie invalid, re-authentication required', shouldRetry: false };
  }
  if (errorStr.startsWith(ERROR_CODES.HTTP_UNAUTHORIZED)) {
    return { type: 'HTTP_UNAUTHORIZED', category: 'auth', message: 'Session expired or unauthorized', shouldRetry: false };
  }

  // Network errors — retry makes sense
  if (errorStr.includes(ERROR_CODES.DNS_LOOKUP_FAILED)) {
    return {
      type: 'DNS_LOOKUP_FAILED',
      category: 'network',
      message: 'DNS resolution failed - check network connectivity',
      shouldRetry: true,
    };
  }
  if (errorStr.includes(ERROR_CODES.CONN_RESET)) {
    return { type: 'CONN_RESET', category: 'network', message: 'Connection reset by peer', shouldRetry: true };
  }
  if (errorStr.includes(ERROR_CODES.SOCKET_TERMINATED)) {
    return { type: 'SOCKET_TERMINATED', category: 'network', message: 'Socket connection terminated unexpectedly', shouldRetry: true };
  }

  // Transient errors — retry makes sense
  if (errorStr.startsWith(ERROR_CODES.REQUEST_TIMEOUT)) {
    return { type: 'REQUEST_TIMEOUT', category: 'transient', message: 'Request timed out', shouldRetry: true };
  }
  if (errorStr.includes(ERROR_CODES.READ_ETIMEDOUT)) {
    return { type: 'READ_ETIMEDOUT', category: 'transient', message: 'Network read timed out', shouldRetry: true };
  }
  if (errorStr.startsWith(ERROR_CODES.REMOTE_CONNECTION_CLOSED)) {
    return { type: 'REMOTE_CONNECTION_CLOSED', category: 'transient', message: 'Connection closed by remote server', shouldRetry: true };
  }
  if (errorStr.includes(ERROR_CODES.EMPTY_RESPONSE)) {
    return { type: 'EMPTY_RESPONSE', category: 'transient', message: 'Empty response from Amazon API', shouldRetry: true };
  }
  if (errorStr.includes(ERROR_CODES.HTTP2_SESSION_INVALID)) {
    return {
      type: 'HTTP2_SESSION_INVALID',
      category: 'transient',
      message: 'HTTP2 session invalid - needs reconnection',
      shouldRetry: true,
    };
  }

  // Device errors — swallow, do not retry
  if (errorStr.startsWith(ERROR_CODES.UNKNOWN_DEVICE)) {
    return { type: 'UNKNOWN_DEVICE', category: 'unknown', message: 'Device not found', shouldRetry: false };
  }

  return { type: 'UNKNOWN', category: 'unknown', message: errorStr, shouldRetry: false };
}

/**
 * Check if a host is reachable by attempting a TCP connection to port 443.
 * Proves end-to-end network connectivity, not just DNS resolution.
 */
export function checkReachability(host: string, timeoutMs = 5000): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port: 443, timeout: timeoutMs });
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('timeout', () => { socket.destroy(); resolve(false); });
    socket.once('error', () => { socket.destroy(); resolve(false); });
  });
}
