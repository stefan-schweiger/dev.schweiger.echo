import Homey from 'homey';
import { AlexaApi } from './lib/api';
import { Log } from 'homey-log';
import { Logger } from './lib/logger';

class EchoRemoteApp extends Homey.App {
  private logger: Logger | undefined;
  public api: AlexaApi | undefined;

  private auditInterval: NodeJS.Timeout | undefined;

  private getSetting = (key: string): any => this.homey.settings.get(key);
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

    this.logger = new Logger(new Log({ homey: this.homey }), this.getSetting('diagnosticLogging'), 'debug');

    const errorTrigger = this.homey.flow.getTriggerCard('error');

    const auth = this.getSetting('auth');

    this.api = new AlexaApi(auth, this.logger);
    this.api.on('authenticated', (auth) => this.setSetting('auth', auth));
    this.api.on('connected', async (payload) => this.emit('connected', payload));

    this.api.on('device-info', (info) => {
      this.deviceEmit(info.id, 'device-info', info);
    });

    this.api.on('error', (error) => {
      this.error(error);
      this.logger?.exception(error);
      errorTrigger.trigger({ error: error.message });
    });

    try {
      auth &&
        (await this.api.connect({
          page: this.getSetting('page'),
          language: this.homey.i18n.getLanguage(),
        }));
    } catch (e) {
      this.error(e);
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
    return this.api?.reset();
  }

  async status() {
    return {
      connected: this.api?.connected,
    };
  }

  private deviceEmit = async (id: string, event: string, payload: any) => {
    try {
      const devices = this.homey.drivers.getDriver('echo').getDevices();
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
