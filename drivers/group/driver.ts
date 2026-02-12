import Homey from 'homey';
import { AlexaApi } from '../../lib/api';

module.exports = class GroupDriver extends Homey.Driver {
  private get api() {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (this.homey.app as any).api as AlexaApi;
  }

  /**
   * onInit is called when the driver is initialized.
   */
  async onInit() {
    this.log('GroupDriver has been initialized');
  }

  async onPair(session: Homey.Driver.PairSession): Promise<void> {
    session.setHandler('check_connection', async () => this.api.connected);

    session.setHandler('list_devices', async () => await this.onPairListDevices());
  }

  /**
   * onPairListDevices is called when a user is adding a device and the 'list_devices' view is called.
   * This should return an array with the data of devices that are available for pairing.
   */
  async onPairListDevices() {
    const devices = await this.api.getDevices();

    return devices
      .filter((device) => ['WHA'].includes(device.model.family))
      .map((device) => ({
        name: device.name,
        data: {
          id: device.id,
        },
        store: {
          ...device,
        },
      }));
  }
};
