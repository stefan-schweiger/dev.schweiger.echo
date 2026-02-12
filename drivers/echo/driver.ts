import Homey from 'homey';
import { AlexaApi } from '../../lib/api';

module.exports = class EchoDriver extends Homey.Driver {
  private get api() {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (this.homey.app as any).api as AlexaApi;
  }

  /**
   * onInit is called when the driver is initialized.
   */
  async onInit() {
    this.registerMessageAction();
    this.registerCommandAction();
    this.registerPlaySoundAction();
    this.registerRunRoutineAction();
    this.log('EchoDriver has been initialized');
  }

  private registerMessageAction() {
    this.homey.flow.getActionCard('message').registerRunListener((args, _state) => this.api.say(args.device.id, args.message, args.speech));
  }

  private registerCommandAction() {
    this.homey.flow.getActionCard('command').registerRunListener((args, _state) => this.api.executeCommand(args.device.id, args.command));
  }

  private registerPlaySoundAction() {
    this.homey.flow
      .getActionCard('play-sound')
      .registerArgumentAutocompleteListener('sound', (_args, _state) => this.api.getSounds())
      .registerRunListener((args, _state) => this.api.playSound(args.device.id, args.sound.id));
  }

  private registerRunRoutineAction() {
    this.homey.flow
      .getActionCard('run-routine')
      .registerArgumentAutocompleteListener('routine', (_args, _state) => this.api.getRoutines())
      .registerRunListener((args, _state) => this.api.executeRoutine(args.device.id, args.routine.id));
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
      .filter((device) => ['ECHO', 'KNIGHT', 'ROOK'].includes(device.model.family))
      .map((device) => ({
        name: device.name,
        icon:
          device.model.name && device.model.generation
            ? `icon-${device.model.name.replaceAll(' ', '')}-Gen${device.model.generation}.svg`
            : undefined,
        data: {
          id: device.id,
        },
        store: {
          ...device,
        },
      }));
  }
};
