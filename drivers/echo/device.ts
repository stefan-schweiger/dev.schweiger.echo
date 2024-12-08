import Homey, { Image } from 'homey';
import { AlexaApi, DeviceInfo } from '../../lib/api';

module.exports = class MyDevice extends Homey.Device {
  private albumArtImage?: Image;

  private get api() {
    return (this.homey.app as any).api as AlexaApi;
  }

  get id(): string {
    return this.getData().id;
  }

  async updateCapability(capability: string, value: any, disabled?: boolean) {
    if (value === undefined || !this.hasCapability(capability)) {
      return;
    }

    if (disabled !== undefined) {
      // currently disabling ui elements etc is not supported by homey, so for now we only disable the setable option
      await this.setCapabilityOptions(capability, { setable: !disabled });
    }

    if (value !== undefined) {
      await this.setCapabilityValue(capability, value);
    }
  }

  /**
   * onInit is called when the device is initialized.
   */
  async onInit() {
    const { capabilities, model } = this.getStore();

    this.setSettings({
      serial_number: this.id,
      model_number: model?.id,
      capabilities: capabilities.join(', '),
    });

    if (capabilities.includes('VOLUME_SETTING')) {
      await this.addCapability('volume_set');
      await this.removeCapability('volume_set.notifications');
      // TODO: unsure what actually is influenced by this, so disable for now
      // await this.addCapability('volume_set.notifications');
    }
    if (capabilities.includes('AUDIO_CONTROLS')) {
      await this.addCapability('speaker_playing');
      await this.addCapability('speaker_prev');
      await this.addCapability('speaker_next');
      await this.addCapability('speaker_shuffle');
      await this.addCapability('speaker_repeat');
      await this.addCapability('speaker_track');
      await this.addCapability('speaker_album');
      await this.addCapability('speaker_artist');
    }

    this.registerCapabilityListener('volume_set', async (value) => this.api.changeVolume(this.id, value));
    this.registerCapabilityListener('volume_set.notifications', async (value) => this.api.changeNotificationVolume(this.id, value));
    this.registerCapabilityListener('speaker_playing', async (value) => this.api.changePlayback(this.id, value ? 'play' : 'pause'));
    this.registerCapabilityListener('speaker_prev', async () => this.api.changePlayback(this.id, 'previous'));
    this.registerCapabilityListener('speaker_next', async () => this.api.changePlayback(this.id, 'next'));
    this.registerCapabilityListener('speaker_shuffle', async (value) => this.api.changePlayback(this.id, 'shuffle', value));
    this.registerCapabilityListener('speaker_repeat', async (value) => this.api.changePlayback(this.id, 'repeat', value !== 'none'));

    this.albumArtImage = await this.homey.images.createImage();
    this.setAlbumArtImage(this.albumArtImage);

    this.on('device-info', async (payload: DeviceInfo) => {
      this.updateCapability('volume_set', payload.volume);
      this.updateCapability('volume_set.notifications', payload.notificationVolume);
      this.updateCapability('speaker_playing', payload.playing);
      this.updateCapability('speaker_shuffle', payload.shuffle === 'disabled' ? false : payload.shuffle, payload.shuffle === 'disabled');
      this.updateCapability('speaker_repeat', payload.repeat === 'disabled' ? 'none' : payload.repeat, payload.repeat === 'disabled');

      if (payload.media !== undefined) {
        await this.setCapabilityValue('speaker_track', payload.media.track);
        this.setCapabilityOptions;
        await this.setCapabilityValue('speaker_artist', payload.media.artist);
        await this.setCapabilityValue('speaker_album', payload.media.album);

        this.albumArtImage?.setUrl(payload.media.artwork ?? null);
        this.albumArtImage?.update();
      }
    });

    this.homey.app.on('connected', async (payload) => {
      if (payload) {
        await this.setAvailable();
      } else {
        await this.setUnavailable('No connection');
      }
    });

    this.log(`${this.getName()} with id ${this.id} has been initialized`);
  }
};
