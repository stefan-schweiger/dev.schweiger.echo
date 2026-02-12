/* eslint-disable @typescript-eslint/no-explicit-any */
declare module 'homey-log' {
  import { Homey } from 'homey';

  interface LogOptions {
    /** Track unhandled promise rejections (not enabled by default) */
    captureUnhandledRejections?: boolean;
  }

  interface LogConstructorOptions {
    homey: Homey;
    options?: LogOptions;
  }

  export class Log {
    /**
     * Construct a new Log instance.
     * @param {object} homey - `this.homey` instance in
     * your app (e.g. `App#homey`/`Driver#homey`/`Device#homey`).
     *
     * @param {object} options - Additional options for Raven
     *
     * @example
     * class MyApp extends Homey.App {
     *   onInit() {
     *     this.homeyLog = new Log({ homey: this.homey });
     *   }
     * }
     */
    constructor(options: LogConstructorOptions);

    /**
     * Init Raven, provide falsy value as `url` to prevent sending events upstream in debug mode.
     */
    private init(url: string | boolean, opts: LogOptions): Log;

    /**
     * Set `tags` that will be send as context with every message or error. See the raven-node
     * documentation: https://docs.sentry.io/clients/node/usage/#raven-node-additional-context.
     */
    setTags(tags: Record<string, any>): Log;

    /**
     * Set `extra` that will be send as context with every message or error. See the raven-node
     * documentation: https://docs.sentry.io/clients/node/usage/#raven-node-additional-context.
     */
    setExtra(extra: Record<string, any>): Log;

    /**
     * Set `user` that will be send as context with every message or error. See the raven-node
     * documentation: https://docs.sentry.io/clients/node/usage/#raven-node-additional-context.
     */
    setUser(user: Record<string, any>): Log;

    /**
     * Create and send message event to Sentry. See the raven-node documentation:
     * https://docs.sentry.io/clients/node/usage/#capturing-messages
     */
    captureMessage(message: string): Promise<string> | undefined;

    /**
     * Create and send exception event to Sentry. See the raven-node documentation:
     * https://docs.sentry.io/clients/node/usage/#capturing-errors
     */
    captureException(err: Error): Promise<string> | undefined;

    private static _log(...args: any[]): void;

    private static _error(...args: any[]): void;

    private static _logTime(): string;

    private static _mergeContext(key: string, value: Record<string, any>): void;
  }
}
