import AlexaRemote, { MessageCommands, SequenceNodeCommand, Sound as AlexaSound, Value, SequenceValue } from 'alexa-remote2';
import Homey from 'homey';
import IP from 'neoip';
import { promisify, promisifyWithOptions, sleep } from './helpers';
import Cache from 'node-cache';
import { Logger } from './logger';
import { ConnectionState, checkReachability, categorizeError } from './connection';
import { SUCCESS_HTML, SERVERS, LANG_MAP, DEVICES, VOICES } from './constants';

/** Homey uses 0–1 for volume, Alexa uses 0–100. */
const toHomeyVolume = (v: number) => v / 100;
const toAlexaVolume = (v: number) => v * 100;

// ── Types for raw alexa-remote2 API responses (not fully typed upstream) ──

/** Subset of fields we read from the raw device list response. */
type RawAlexaDevice = {
  serialNumber: string;
  accountName: string;
  capabilities: string[];
  online: boolean;
  deviceType: string;
  deviceFamily: string;
  parentClusters?: string[];
};

/** Shape returned by alexa.getPlayerInfo(). */
type RawPlayerInfo = {
  state?: string;
  volume?: { volume: number };
  transport?: { shuffle?: string; repeat?: string };
  mainArt?: { url?: string };
  infoText?: { title?: string; subText1?: string; subText2?: string };
};

/** Shape returned by alexa.getDeviceNotificationState(). */
type NotificationState = {
  volumeLevel: number;
};

/** Shape returned by alexa.getAutomationRoutines(). */
type RawAlexaRoutine = {
  automationId: string;
  sequence: unknown;
  name: string;
};

/** WebSocket media event payload (audio-player-state-change, media-queue-change, media-change). */
type MediaEventPayload = {
  deviceSerialNumber: string;
  audioPlayerState?: string;
  playBackOrder?: string;
  loopMode?: string;
};

export type Routine = {
  id: {
    automationId: string;
    sequence: unknown;
  };
  name: string;
};

export type DeviceInfo = {
  id: string;
  volume?: number;
  notificationVolume?: number;
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
    .replace(/"customerName":\s?".*"/g, '"customerName":"REDACTED"')
    .replace(/"customerEmail":\s?".*"/g, '"customerEmail":"REDACTED"')
    .replace(/"customerId":\s?".*"/g, '"customerId":"REDACTED"')
    .replace(/"address[1-3]":\s?".*"/g, '"address":"REDACTED"')
    .replace(/"deviceAddress":\s?".*"/g, '"deviceAddress":"REDACTED"')
    .replace(/"state":\s?".*"/g, '"state":"REDACTED"');
};

export { ConnectionState } from './connection';

export class AlexaApi extends Homey.SimpleClass {
  private _state: ConnectionState = ConnectionState.DISCONNECTED;
  private alexa = new AlexaRemote();
  private cache = new Cache({ stdTTL: 60 });
  private alexaServiceHost: string | undefined;

  /** Backward-compatible getter — drivers/pairing read this. */
  public get connected(): boolean {
    return this._state === ConnectionState.CONNECTED;
  }

  /** Get the current connection state. */
  public get state(): ConnectionState {
    return this._state;
  }

  /**
   * Transition to a new connection state.
   * Logs transitions, emits 'state-change', and emits backward-compatible
   * 'connected' boolean event only when connected status actually changes.
   * When disconnecting, an optional reason is forwarded so devices can display it.
   */
  private setState(newState: ConnectionState, reason?: string) {
    const oldState = this._state;
    if (oldState === newState) return;

    this._state = newState;
    this.logger.info(`Connection state: ${oldState} -> ${newState}`);
    this.emit('state-change', { from: oldState, to: newState });

    const wasConnected = oldState === ConnectionState.CONNECTED;
    const isConnected = newState === ConnectionState.CONNECTED;
    if (wasConnected !== isConnected) {
      this.emit('connected', isConnected, reason);
    }
  }

  /** Allow app.ts to set RECONNECTING state before attempting reconnect. */
  public setReconnecting() {
    this.setState(ConnectionState.RECONNECTING);
  }

  constructor(
    private authData: string | undefined,
    private logger: Logger,
  ) {
    super();

    this.alexa.on('ws-volume-change', (payload) => {
      this.emit<DeviceInfo>('device-info', {
        id: payload.deviceSerialNumber,
        volume: toHomeyVolume(payload.volume),
      });
    });

    this.on('connected', async (connected: boolean) => {
      try {
        if (connected) {
          await sleep(3000);
          await this.updateAllPlayers();
        }
      } catch (e) {
        this.emit('error', e);
      }
    });

    const handleMedia = async (payload: MediaEventPayload) => {
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

    this.alexa.on('ws-connect', () => {
      this.logger.info('WebSocket connected');
      this.checkConnection();
    });

    this.alexa.on('ws-disconnect', (retries: boolean, msg: string) => {
      this.logger.info(`WebSocket disconnected: ${msg} (will retry: ${retries})`);
      if (!retries) {
        // Library gave up retrying, check connection status immediately
        this.checkConnection();
      }
    });
  }

  private async init(options: { cookie: string | undefined; page: string; language: string; reconnect?: boolean }): Promise<void> {
    const isReconnect = !!options.reconnect && !!options.cookie;
    this.logger.info(isReconnect ? 'Reconnecting with saved cookie' : options.cookie ? 'Using cookie' : 'No cookie found');

    // On reconnect, force the library to refresh the cookie instead of reusing
    // a potentially stale access token. The library skips refresh when
    // tokenDate < 24h, but the access token may have expired much sooner.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let cookie: any = options.cookie;
    if (isReconnect && cookie && typeof cookie === 'object' && cookie.tokenDate) {
      cookie = { ...cookie, tokenDate: 0 };
    }

    const opts = {
      cookie,
      apiUserAgentPostfix: '',
      logger: (message?: string) => {
        message = filterLogMessage(message ?? '');
        if (message) {
          this.logger.debug(message);
        }
      },
      deviceAppName: 'Homey Echo Integration',
      proxyLogLevel: 'warn',
      alexaServiceHost: SERVERS[options.page] || undefined,
      amazonPage: `https://www.${options.page}`,
      cookieRefreshInterval: 4 * 24 * 60 * 60 * 1000,
      usePushConnection: true,
      acceptLanguage: LANG_MAP[options.language] || 'en-US',
      // On reconnect with a saved cookie, don't start the proxy server — it's only
      // needed for the initial browser-based authentication flow.
      proxyOnly: !isReconnect,
      proxyOwnIp: IP.address('private'),
      proxyListenBind: '0.0.0.0',
      proxyPort: 3081,
      setupProxy: !isReconnect,
      amazonPageProxyLanguage: LANG_MAP[options.language]?.replace('-', '_') || 'en_US',
      proxyCloseWindowHTML: SUCCESS_HTML,
    };

    this.alexaServiceHost = opts.alexaServiceHost;
    await promisifyWithOptions(this.alexa.init.bind(this.alexa), { ...opts });
  }

  public async checkConnection() {
    this.logger.info('Checking connection');

    if (this.alexaServiceHost) {
      const reachable = await checkReachability(this.alexaServiceHost);
      if (!reachable) {
        this.logger.info(`Connection check: ${this.alexaServiceHost} is not reachable (network issue)`);
        this.setState(
          ConnectionState.ERROR,
          `Unable to connect to ${this.alexaServiceHost}. Wait for automatic retry or check network connectivity.`,
        );
        return;
      }
    }

    try {
      const authenticated = await new Promise<boolean>((resolve, reject) =>
        // for whatever reason this has the reverse signature
        this.alexa.checkAuthentication((result, error) => {
          if (error) {
            reject(error);
          } else {
            resolve(result as unknown as boolean);
          }
        }),
      );
      this.logger.info('Connection checked: ' + authenticated);
      this.setState(
        authenticated ? ConnectionState.CONNECTED : ConnectionState.DISCONNECTED,
        'Authentication expired — please re-authenticate in app settings',
      );
    } catch (e) {
      const categorized = categorizeError(e);
      this.logger.info(`Connection check error [${categorized.type}/${categorized.category}]: ${categorized.message}`);
      this.setState(ConnectionState.ERROR, categorized.message);
    }
  }

  public async connect(options: { page: string; language: string }): Promise<ConnectionResult> {
    this.logger.info('Connecting');
    const auth = this.authData;
    const isReconnect = !!auth;

    if (isReconnect) {
      // On reconnect: just stop the proxy and previous push connection,
      // but keep cookie/cookieData intact — don't destroy library state.
      this.alexa.stopProxyServer(() => {});
      this.alexa.stop();
    } else {
      this.cleanup();
    }

    this.setState(ConnectionState.CONNECTING);

    let result: ConnectionResult;

    try {
      await this.init({ cookie: auth, ...options, reconnect: isReconnect });
      this.logger.info('Done Initializing');
      result = { type: 'connected' };
    } catch (e) {
      if (!(e instanceof Error)) {
        throw new Error('Unknown error: ' + e);
      }

      // alexa-remote2 signals "needs browser auth" by throwing with "Please open <url>".
      // This is the expected path when no valid cookie exists — not a real error.
      if (e.message.startsWith('Please open')) {
        this.logger.info('Proxy started');
        result = {
          type: 'proxy',
          url: e.message.match(/https?:\/\/[^ ]+/)?.[0],
        };
      } else {
        this.logger.info(`Init error: ${e.message}`);
        this.setState(ConnectionState.ERROR, e.message);
        this.emit('error', e);
        throw e;
      }
    }

    // Everything below only runs on success or proxy — never on error

    if (this.alexa.cookieData) {
      this.logger.info('Authenticated');
      this.authData = this.alexa.cookieData;
      this.emit('authenticated', this.authData);
    }

    // Only after we are initialized we can listen for the cookie event
    // otherwise we might get duplicate events
    this.alexa.removeAllListeners('cookie');
    this.alexa.on('cookie', async () => {
      this.logger.info('Cookie Data received');
      this.authData = this.alexa.cookieData;
      this.emit('authenticated', this.authData);
      await this.checkConnection();
    });

    await this.checkConnection();

    return result;
  }

  private cleanup() {
    this.alexa.stopProxyServer(() => {});
    this.alexa.stop();
    this.alexa.cookie = undefined;
    this.alexa.cookieData = undefined;
    this.cache.del(['devices', 'sounds', 'routines']);
    this.setState(ConnectionState.DISCONNECTED);
  }

  public async reset() {
    this.cleanup();
    this.authData = undefined;
    this.emit('authenticated', undefined);
  }

  private async sendCommand(device: string, command: MessageCommands, value: Value): Promise<void> {
    this.logger.debug(`Sending command ${command} to ${device} with value ${value}`);
    try {
      await new Promise<void>((resolve, reject) => {
        this.alexa?.sendCommand(device, command, value, (error) => (error ? reject(error) : resolve()));
      });
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  private async sendSequenceCommand(device: string, command: SequenceNodeCommand, value: SequenceValue): Promise<void> {
    this.logger.debug(`Sending sequence command ${command} to ${device} with value ${value}`);
    try {
      await new Promise<void>((resolve, reject) => {
        this.alexa.sendSequenceCommand(device, command, value, (error) => (error ? reject(error) : resolve()));
      });
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public async getDevices(): Promise<Device[]> {
    this.logger.debug('Getting devices');
    try {
      let devices = this.cache.get<RawAlexaDevice[]>('devices');

      if (!devices) {
        devices = await promisify<{ devices: RawAlexaDevice[] }>(this.alexa.getDevices.bind(this.alexa)).then((result) => result.devices);
        this.cache.set('devices', devices, 60 * 5);
      }

      return (
        (devices ?? [])
          // ECHO – Standard Echo speakers (Dot, Echo, Show, etc.).
          // KNIGHT – Certain Echo‑class devices (often newer or display/tablet‑style, exact mapping is undocumented).
          // ROOK – Another Echo family used by some models or generations.
          // WHA – Multi‑room (Whole‑Home Audio) groups, used when an endpoint represents a speaker group rather than a single device.
          //
          // Intentionally excluded since we only want to support first party Echo devices
          // SPEAKER – Some integrations treat additional Alexa speakers or third‑party Alexa‑enabled speakers as this family.

          .filter((device) => ['ECHO', 'KNIGHT', 'ROOK', 'WHA'].includes(device.deviceFamily))
          .map(
            (device): Device => ({
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
          )
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

  public async sayWithVoice(device: string, message: string, voiceId: string, type: 'speak' | 'whisper' = 'speak') {
    const [voice, lang] = voiceId.split(':');
    const content = type === 'whisper' ? `<amazon:effect name="whispered">${message}</amazon:effect>` : message;
    return this.sendSequenceCommand(
      device,
      'ssml',
      `<speak><voice name="${voice}"><lang xml:lang="${lang}">${content}</lang></voice></speak>`,
    );
  }

  public async executeCommand(device: string, message: string) {
    return this.sendSequenceCommand(device, 'textCommand', message);
  }

  public async executeRoutine(device: string, routine: string) {
    return this.sendSequenceCommand(device, routine as SequenceNodeCommand, '');
  }

  public async playSound(device: string, sound: string) {
    // typing does not contain sound even though it actually works
    return this.sendSequenceCommand(device, 'sound' as SequenceNodeCommand, sound);
  }

  public async changeVolume(device: string, volume: number) {
    return this.sendSequenceCommand(device, 'volume', toAlexaVolume(volume));
  }

  public async getNotificationVolume(device: string): Promise<number | undefined> {
    try {
      const result = await promisifyWithOptions<NotificationState>(this.alexa.getDeviceNotificationState.bind(this.alexa), device);
      return toHomeyVolume(result?.volumeLevel);
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public async changeNotificationVolume(device: string, volume: number) {
    try {
      await new Promise<void>((resolve, reject) => {
        this.alexa.setDeviceNotificationVolume(device, toAlexaVolume(volume), (error) => (error ? reject(error) : resolve()));
      });
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public async changePlayback(device: string, action: 'play' | 'pause' | 'next' | 'previous' | 'repeat' | 'shuffle', value = true) {
    return this.sendCommand(device, action, value);
  }

  public async getPlayerInfo(device: string): Promise<Partial<DeviceInfo>> {
    this.logger.debug(`Getting player info for ${device}`);
    const { playerInfo } = await promisifyWithOptions<{ playerInfo: RawPlayerInfo }>(this.alexa.getPlayerInfo.bind(this.alexa), device);
    const notificationVolume = await this.getNotificationVolume(device);

    return {
      id: device,
      playing: playerInfo?.state === 'PLAYING',
      volume: playerInfo?.volume?.volume != null ? toHomeyVolume(playerInfo.volume.volume) : undefined,
      notificationVolume,
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
      let sounds = this.cache.get<AlexaSound[]>('sounds');

      if (!sounds) {
        sounds = await promisify<AlexaSound[]>(this.alexa.getRoutineSoundList.bind(this.alexa));
        this.cache.set('sounds', sounds, 60 * 60);
      }

      return sounds
        .map((sound) => ({
          id: sound.id,
          name: sound.displayName,
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
    } catch (e) {
      this.emit('error', e);
      throw e;
    }
  }

  public getVoices(query?: string): { id: string; name: string }[] {
    const voices = VOICES.map((voice) => ({
      id: `${voice.id}:${voice.lang}`,
      name: voice.name,
    }));

    if (!query) return voices;
    const q = query.toLowerCase();
    return voices.filter((voice) => voice.name.toLowerCase().includes(q));
  }

  public async getRoutines(): Promise<Routine[]> {
    this.logger.debug('Getting routines');
    try {
      let routines = this.cache.get<RawAlexaRoutine[]>('routines');

      if (!routines) {
        routines = await promisifyWithOptions<RawAlexaRoutine[]>(this.alexa.getAutomationRoutines.bind(this.alexa), undefined);
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
