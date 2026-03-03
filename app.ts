import Homey from 'homey';
import { AlexaApi, ConnectionState } from './lib/api';
import { categorizeError } from './lib/connection';
import { Logger } from './lib/logger';

class EchoRemoteApp extends Homey.App {
  private logger: Logger | undefined;
  public api: AlexaApi | undefined;

  private auditInterval: NodeJS.Timeout | undefined;
  private reconnectTimer: NodeJS.Timeout | undefined;
  private reconnectAttempts = 0;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private getSetting = (key: string): any => this.homey.settings.get(key);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private setSetting = (key: string, value: any): void => this.homey.settings.set(key, value);

  /**
   * onInit is called when the app is initialized.
   */
  async onInit() {
    const page = this.getSetting('page');

    // TODO: Remove legacy fix for settings from old versions
    if (page?.startsWith('https://www.')) {
      this.setSetting('page', page.replace('https://www.', ''));
    }

    // Disable homey logger for now because we were running into rate limiting.
    // For now people should submit diagnostic reports if they run into issues.
    const homeyLogger = undefined; // new Log({ homey: this.homey });
    this.logger = new Logger(homeyLogger, this.getSetting('diagnosticLogging'), 'debug');

    // Global error safety net
    process.on('unhandledRejection', (reason) => {
      this.logger?.error(`Unhandled Promise Rejection: ${reason}`);
      if (reason instanceof Error) {
        this.logger?.exception(reason);
      }
    });

    process.on('uncaughtException', (error) => {
      this.logger?.error(`Uncaught Exception: ${error.message}`);
      this.logger?.exception(error);
    });

    const errorTrigger = this.homey.flow.getTriggerCard('error');

    const auth = this.getSetting('auth');
    this.logger?.info(`Startup: auth ${auth ? 'found' : 'not found'} in settings`);

    this.api = new AlexaApi(auth, this.logger);
    this.api.on('authenticated', (auth) => this.setSetting('auth', auth));
    this.api.on('connected', async (payload: boolean, reason?: string) => {
      this.emit('connected', payload, reason);
      if (payload) {
        this.clearReconnect();
      } else if (this.getSetting('auth')) {
        this.scheduleReconnect();
      }
    });

    this.api.on('device-info', (info) => {
      this.deviceEmit(info.id, 'device-info', info);
    });

    this.api.on('error', (error: Error) => {
      const categorized = categorizeError(error);
      this.error(error);
      this.logger?.info(`Error [${categorized.type}/${categorized.category}]: ${categorized.message}`);
      this.logger?.exception(error);
      // "no body" responses are expected transient noise from Amazon's API — don't surface to user
      if (categorized.type !== 'EMPTY_RESPONSE') {
        errorTrigger.trigger({ error: categorized.message });
      }
    });

    try {
      if (auth) {
        await this.api.connect({
          page: this.getSetting('page'),
          language: this.homey.i18n.getLanguage(),
        });
      }
    } catch (e) {
      this.error(e);
      // Initial connect failed (e.g. transient 400 from Amazon) — schedule retry
      if (this.getSetting('auth')) {
        this.scheduleReconnect();
      }
    }

    if (this.auditInterval) this.homey.clearInterval(this.auditInterval);
    this.auditInterval = this.homey.setInterval(() => this.api?.checkConnection(), 5 * 60 * 1000);
  }

  async connect() {
    this.logger!.diagnosticLogging = this.getSetting('diagnosticLogging');

    return await this.api?.connect({
      page: this.getSetting('page'),
      language: this.homey.i18n.getLanguage(),
    });
  }

  async disconnect() {
    this.clearReconnect();
    return this.api?.reset();
  }

  async status() {
    return {
      connected: this.api?.connected,
      state: this.api?.state,
    };
  }

  async reset() {
    this.clearReconnect();
    this.setSetting('auth', undefined);
    return this.api?.reset();
  }

  private scheduleReconnect() {
    const auth = this.getSetting('auth');
    if (!auth || this.reconnectTimer || this.api?.state === ConnectionState.RECONNECTING || this.reconnectAttempts >= 10) {
      if (this.reconnectAttempts >= 10) {
        this.logger?.info('Max reconnect attempts reached, giving up. User needs to re-authenticate.');
      }
      return;
    }

    // Exponential backoff: 30s, 1m, 2m, 4m, 8m, then cap at 15m
    const delay = Math.min(30_000 * Math.pow(2, this.reconnectAttempts), 15 * 60_000);
    this.logger?.info(`Scheduling reconnect attempt ${this.reconnectAttempts + 1} in ${Math.round(delay / 1000)}s`);

    this.reconnectTimer = this.homey.setTimeout(async () => {
      this.reconnectTimer = undefined;
      this.api?.setReconnecting();
      this.reconnectAttempts++;

      try {
        const result = await this.api?.connect({
          page: this.getSetting('page'),
          language: this.homey.i18n.getLanguage(),
        });

        if (result?.type === 'proxy') {
          // Cookie expired — can't auto-reconnect, user must re-authenticate
          this.logger?.info('Cookie expired, automatic reconnection not possible');
          this.reconnectAttempts = 10; // Stop further attempts
        } else if (this.api?.state === ConnectionState.CONNECTED) {
          this.logger?.info('Reconnected successfully');
          this.reconnectAttempts = 0;
        } else {
          // connect() returned without throwing but we're not actually connected
          // (e.g. reachability or auth check failed) — keep retrying
          this.logger?.info(`Reconnect attempt ${this.reconnectAttempts} did not restore connection`);
          this.scheduleReconnect();
        }
      } catch (e) {
        this.logger?.info(`Reconnect attempt ${this.reconnectAttempts} failed`);
        this.error(e);
        this.scheduleReconnect();
      }
    }, delay);
  }

  private clearReconnect() {
    if (this.reconnectTimer) {
      this.homey.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    this.reconnectAttempts = 0;
  }

  /**
   * Route device events to the correct Homey device instance.
   * If the target is a paired device, emit directly to it.
   * In both cases, also propagate to any child devices whose parent group matches the id.
   */
  private deviceEmit = async (id: string, event: string, payload: unknown) => {
    try {
      const devices = [...this.homey.drivers.getDriver('echo').getDevices(), ...this.homey.drivers.getDriver('group').getDevices()];
      const device = devices.find((d) => d.getData().id === id);

      const apiDevices = (await this.api?.getDevices()) ?? [];

      if (device) {
        device.emit(event, payload);
        apiDevices
          .filter((d) => d.parentGroups?.includes(id))
          ?.forEach((d) =>
            this.homey.drivers
              .getDriver('echo')
              .getDevice({
                id: d.id,
              })
              .emit(event, payload),
          );
      } else {
        apiDevices
          .filter((d) => d.parentGroups?.includes(id))
          .forEach((x) => devices.find((d) => d.getData().id === x.id)?.emit(event, payload));
      }
    } catch (e) {
      this.error('Error emitting device event', e);
    }
  };
}

module.exports = EchoRemoteApp;
