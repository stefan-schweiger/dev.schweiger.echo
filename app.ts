import Homey from 'homey';
import { AlexaApi } from './lib/api';

class EchoRemoteApp extends Homey.App {
  public api: AlexaApi | undefined;

  private auditInterval: NodeJS.Timeout | undefined;

  private getSetting = (key: string): any => this.homey.settings.get(key);
  private setSetting = (key: string, value: any): void => this.homey.settings.set(key, value);

  private deviceEmit = async (id: string, event: string, payload: any) => {
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
  };

  /**
   * onInit is called when the app is initialized.
   */
  async onInit() {
    const errorTrigger = this.homey.flow.getTriggerCard('error');

    const auth = this.getSetting('auth');

    this.api = new AlexaApi(auth);
    this.api.on('authenticated', (auth) => this.setSetting('auth', auth));
    this.api.on('connected', async (payload) => {
      this.log('connected', payload);
      this.emit('connected', payload);
    });

    this.api.on('device-info', (info) => {
      this.deviceEmit(info.id, 'device-info', info);
    });

    this.api.on('error', (error) => {
      this.error(error);
      errorTrigger.trigger({ error: error.message });
    });

    try {
      auth &&
        (await this.api.connect({
          server: this.getSetting('server'),
          page: this.getSetting('page'),
          language: this.homey.i18n.getLanguage(),
        }));
    } catch (e) {
      this.error(e);
    }

    if (this.auditInterval) this.homey.clearInterval(this.auditInterval);
    this.auditInterval = this.homey.setInterval(() => this.api?.audit(), 5 * 60 * 1000);
  }

  async connect() {
    this.api?.reset();
    return await this.api?.connect({
      server: this.getSetting('server'),
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
}

module.exports = EchoRemoteApp;
