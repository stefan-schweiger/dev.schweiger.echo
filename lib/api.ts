import AlexaRemote, { InitOptions, MessageCommands, SequenceNodeCommand } from 'alexa-remote2';
import Homey from 'homey';
import IP from 'neoip';
import { promisify, promisifyWithOptions, sleep } from './helpers';
import Cache from 'node-cache';
import { Logger } from './logger';

const SUCCESS_HTML = `<!doctype html>
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

const DEVICES: Record<string, { name: string; generation: number }> = {
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
  AIPK7MM90V7TB: { name: 'Echo Show 10', generation: 1 },
  ASQZWP4GPYUT7: { name: 'Echo Pop', generation: 1 },
  A10A33FOX2NUBK: { name: 'Echo Spot', generation: 1 },
  // TODO: find out correct model id
  // ???: { name: 'Echo Spot', generation: 2 },
};

export type DeviceInfo = {
  id: string;
  volume?: number;
  playing?: boolean;
  shuffle?: boolean | 'disabled';
  repeat?: 'track' | 'playlist' | 'none' | 'disabled';
  media?: {
    artwork: string;
    track: string;
    artist: string;
    album: string;
  };
};

export type Device = {
  id: string;
  name: string;
  capabilities: string[];
  online: boolean;
  model: {
    id: string;
    family: string;
    name?: string;
    generation?: number;
  };
  parentGroups?: string[];
};

export type Sound = {
  id: string;
  name: string;
};

export type ConnectionResult =
  | {
      type: 'connected';
    }
  | {
      type: 'proxy';
      url?: string;
    };

const LANG_MAP: Record<string, string> = {
  de: 'de-DE',
  en: 'en-US',
  es: 'es-ES',
  fr: 'fr-FR',
  it: 'it-IT',
  ja: 'ja-JP',
  nl: 'nl-NL',
};

const filterLogMessage = (message: string) => {
  if (
    !message.startsWith('Alexa-Remote:') ||
    message.startsWith('Alexa-Remote: Auth token:') ||
    message.includes('access_token') ||
    message.includes('"firstName"') ||
    message.startsWith('Alexa-Remote: No authentication check needed')
  ) {
    return undefined;
  }

  return message
    .replace(/\"customerName\":\s?\".*\"/g, '"customerName":"REDACTED"')
    .replace(/\"customerEmail\":\s?\".*\"/g, '"customerEmail":"REDACTED"')
    .replace(/\"customerId\":\s?\".*\"/g, '"customerId":"REDACTED"')
    .replace(/\"address[1-3]\":\s?\".*\"/g, '"address":"REDACTED"')
    .replace(/\"deviceAddress\":\s?\".*\"/g, '"deviceAddress":"REDACTED"')
    .replace(/\"state\":\s?\".*\"/g, '"address":"REDACTED"');
};

export class AlexaApi extends Homey.SimpleClass {
  public connected: boolean = false;
  private alexa = new AlexaRemote();
  private cache = new Cache({ stdTTL: 60 });

  constructor(
    private authData: any,
    private logger: Logger,
  ) {
    super();

    this.alexa.on('ws-volume-change', (payload) => {
      this.emit<DeviceInfo>('device-info', {
        id: payload.deviceSerialNumber,
        volume: payload.volume / 100,
      });
    });

    this.on('connected', async (connected) => {
      try {
        if (this.connected === connected) {
          return;
        }

        this.connected = connected;

        if (connected) {
          await sleep(3000);
          await this.updateAllPlayers();
        }
      } catch (e) {
        this.emit('error', e);
      }
    });

    const handleMedia = async (payload: any) => {
      try {
        const playerInfo = await this.getPlayerInfo(payload.deviceSerialNumber);

        this.emit<DeviceInfo>('device-info', {
          ...playerInfo,
          ...{
            id: payload.deviceSerialNumber,
            playing: payload.audioPlayerState ? payload.audioPlayerState === 'PLAYING' : undefined,
            shuffle: payload.playBackOrder ? payload.playBackOrder !== 'NORMAL' : undefined,
            repeat: payload.loopMode
              ? ({
                  REPEAT_ONE: 'track', // TODO: wasn't able to find real value
                  LOOP_QUEUE: 'playlist',
                }[payload.loopMode as string] ?? 'none')
              : undefined,
          },
        });
      } catch (e) {
        this.emit('error', e);
      }
    };

    this.alexa.on('ws-audio-player-state-change', handleMedia);
    this.alexa.on('ws-media-queue-change', handleMedia);
    this.alexa.on('ws-media-change', handleMedia);
  }

  private async init(options: { cookie: any; server: string; page: string; language: string }): Promise<void> {
    const defaultOptions: Partial<InitOptions> = {
      apiUserAgentPostfix: '',
      logger: (message) => {
        message = filterLogMessage(message);
        message && this.logger.debug(message);
      },
      deviceAppName: 'Homey Echo Integration',
      proxyLogLevel: 'warn',
      alexaServiceHost: options.server || undefined,
      amazonPage: options.page || undefined,
      cookieRefreshInterval: 7 * 24 * 60 * 60 * 1000,
      usePushConnection: true,
      acceptLanguage: LANG_MAP[options.language] || 'en-US',
    };

    this.logger.info(options.cookie ? 'Using cookie' : 'No cookie found');

    const opts = options.cookie
      ? {
          cookie: options.cookie,
          ...defaultOptions,
          setupProxy: false,
          proxyOnly: false,
        }
      : {
          ...defaultOptions,
          proxyOnly: true,
          proxyOwnIp: IP.address('private'),
          proxyListenBind: '0.0.0.0',
          proxyPort: 3081,
          setupProxy: true,
          amazonPageProxyLanguage: LANG_MAP[options.language].replace('-', '_') || 'en_US',
          proxyCloseWindowHTML: SUCCESS_HTML,
        };

    await promisifyWithOptions(this.alexa.init.bind(this.alexa), { ...opts });
  }

  public async checkConnection() {
    this.logger.info('Checking connection');
    // // slight delay to give the connection time to establish
    // await sleep(2000);
    try {
      const connected = await new Promise<boolean>((resolve, reject) =>
        // for whatever reason this has the reverse signature
        this.alexa.checkAuthentication((result, error) => {
          if (error) {
            reject(error);
          } else {
            resolve(result as unknown as boolean);
          }
        }),
      );
      this.logger.info('Connection checked: ' + connected);
      this.emit('connected', connected);
    } catch (e) {
      this.logger.info('Connection checked: error');
      this.logger.debug(JSON.stringify(e));
      this.emit('connected', false);
    }
  }

  public async connect(options: { server: string; page: string; language: string }): Promise<ConnectionResult> {
    let result: ConnectionResult | Error = {
      type: 'connected' as const,
    };

    try {
      this.logger.info('Connecting');
      const auth = this.authData;
      this.reset();
      await this.init({ cookie: auth, ...options });
      this.logger.info('Done Initializing');
    } catch (e) {
      if (!(e instanceof Error)) {
        throw new Error('Unknown error: ' + e);
      }

      // if we need to open a proxy, we return the url
      if (e.message.startsWith('Please open')) {
        this.logger.info('Proxy started');
        result = {
          type: 'proxy' as const,
          url: e.message.match(/https?:\/\/[^ ]+/)?.[0],
        };
      } else {
        this.logger.info('Other error during init');
        this.emit('error', e);
        result = e;
      }
    }

    // if we have a cookie, we are already authenticated
    if (this.alexa.cookieData) {
      this.logger.info('Authenticated');
      this.authData = this.alexa.cookieData;
      this.emit('authenticated', this.authData);
    }

    // only after we are initialized we can listen for the cookie event
    // otherwise we might get duplicate events
    this.alexa.removeAllListeners('cookie');
    this.alexa.on('cookie', async () => {
      this.logger.info('Cookie Data received');
      this.authData = this.alexa.cookieData;
      this.emit('authenticated', this.authData);
      await this.checkConnection();
    });

    await this.checkConnection();

    if (result instanceof Error) {
      throw result;
    }

    return result;
  }

  public async reset() {
    this.alexa.stop();
    this.alexa.cookie = undefined;
    this.alexa.cookieData = undefined;
    this.authData = undefined;
    this.cache.del(['devices', 'sounds', 'routines']);
    this.connected = false;
    this.emit('connected', false);
    this.emit('authenticated', undefined);
  }

  private async sendCommand(device: string, command: MessageCommands, value: any) {
    this.logger.debug(`Sending command ${command} to ${device} with value ${value}`);
    try {
      return new Promise<any>((resolve, reject) => {
        this.alexa?.sendCommand(device, command, value, (error, result) => (error ? reject(error) : resolve(result)));
      });
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  private async sendSequenceCommand(device: string, command: SequenceNodeCommand, value: any) {
    this.logger.debug(`Sending sequence command ${command} to ${device} with value ${value}`);
    try {
      return await new Promise<any>((resolve, reject) => {
        this.alexa.sendSequenceCommand(device, command, value, (error, result) => (error ? reject(error) : resolve(result)));
      });
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public async getDevices(): Promise<Device[]> {
    this.logger.debug('Getting devices');
    try {
      let devices = this.cache.get<any[]>('devices');

      if (!devices) {
        devices = await promisify(this.alexa.getDevices.bind(this.alexa)).then((result: any) => result.devices);
        this.cache.set('devices', devices, 60 * 5);
      }

      return (devices ?? [])
        .filter((device: any) => device.deviceFamily === 'ECHO')
        .map(
          (device: any): Device => ({
            id: device.serialNumber,
            name: device.accountName,
            capabilities: device.capabilities,
            online: device.online,
            model: {
              id: device.deviceType,
              family: device.deviceFamily,
              name: DEVICES[device.deviceType]?.name,
              generation: DEVICES[device.deviceType]?.generation,
            },
            parentGroups: device.parentClusters,
          }),
        );
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public async say(device: string, message: string, type: 'speak' | 'whisper' | 'announce' = 'speak') {
    switch (type) {
      case 'announce':
        return this.sendSequenceCommand(device, 'announcement', message);
      case 'whisper':
        return this.sendSequenceCommand(device, 'ssml', `<speak><amazon:effect name="whispered">${message}</amazon:effect></speak>`);
      default:
        return this.sendSequenceCommand(device, 'speak', message);
    }
  }

  public async executeCommand(device: string, message: string) {
    this.sendSequenceCommand(device, 'textCommand', message);
  }

  public async executeRoutine(device: string, routine: string) {
    this.sendSequenceCommand(device, routine as SequenceNodeCommand, '');
  }

  public async playSound(device: string, sound: string) {
    // typing does not contain sound even though it actually works
    return this.sendSequenceCommand(device, 'sound' as SequenceNodeCommand, sound);
  }

  public async changeVolume(device: string, volume: number) {
    return this.sendSequenceCommand(device, 'volume', volume * 100);
  }

  public async changePlayback(device: string, action: 'play' | 'pause' | 'next' | 'previous' | 'repeat' | 'shuffle', value = true) {
    return this.sendCommand(device, action, value);
  }

  public async getPlayerInfo(device: string): Promise<Partial<DeviceInfo>> {
    this.logger.debug(`Getting player info for ${device}`);
    const { playerInfo } = await promisifyWithOptions<any>(this.alexa.getPlayerInfo.bind(this.alexa), device);

    return {
      id: device,
      playing: playerInfo?.state === 'PLAYING',
      volume: isNaN(playerInfo?.volume?.volume) ? undefined : playerInfo?.volume?.volume / 100,
      shuffle: {
        ENABLED: true,
        DISABLED: false,
        HIDDEN: 'disabled' as const,
      }[playerInfo?.transport?.shuffle as string],
      repeat: {
        ENABLED: 'playlist' as const,
        DISABLED: 'none' as const,
        HIDDEN: 'disabled' as const,
      }[playerInfo?.transport?.repeat as string],
      media: {
        artwork: playerInfo?.mainArt?.url ?? '',
        track: playerInfo?.infoText?.title ?? '',
        artist: playerInfo?.infoText?.subText1 ?? '',
        album: playerInfo?.infoText?.subText2 ?? '',
      },
    };
  }

  public async getSounds(): Promise<Sound[]> {
    this.logger.debug('Getting sounds');
    try {
      let sounds = this.cache.get<any[]>('sounds');

      if (!sounds) {
        sounds = await promisify<any[]>(this.alexa.getRoutineSoundList.bind(this.alexa));
        this.cache.set('sounds', sounds, 60 * 60);
      }

      return sounds
        .map((sound: any) => ({
          id: sound.id,
          name: sound.displayName,
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public async getRoutines(): Promise<any[]> {
    this.logger.debug('Getting routines');
    try {
      let routines = this.cache.get<any[]>('routines');

      if (!routines) {
        routines = await promisifyWithOptions<any[]>(this.alexa.getAutomationRoutines.bind(this.alexa), undefined);
        this.cache.set('routines', routines, 60);
      }

      return routines
        .map((routine) => ({
          // this is the needed "command" for the routine
          id: {
            automationId: routine.automationId,
            sequence: routine.sequence,
          },
          name: routine.name,
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  private async updateAllPlayers() {
    this.logger.debug('Updating all players');
    const devices = await this.getDevices();

    for (const device of devices) {
      const info = await this.getPlayerInfo(device.id);

      this.emit('device-info', info);

      if (!info.playing && device.parentGroups) {
        for (const parentGroup of device.parentGroups) {
          const parentInfo = await this.getPlayerInfo(parentGroup);

          if (parentInfo.playing) {
            this.emit('device-info', {
              ...parentInfo,
              id: device.id,
            });
          }
        }
      }
    }
  }
}
