import { Log } from 'homey-log';

export class Logger {
  constructor(
    public homeyLogger: Log,
    public diagnosticLogging: boolean,
    public logLevel: 'debug' | 'info' | 'warn' | 'error' = 'info',
  ) {}

  private checkLogLevel = (level: 'debug' | 'info' | 'warn' | 'error') =>
    ['debug', 'info', 'warn', 'error'].indexOf(level) >= ['debug', 'info', 'warn', 'error'].indexOf(this.logLevel);

  public debug = (message: string) =>
    this.checkLogLevel('debug')
      ? this.diagnosticLogging
        ? this.homeyLogger.captureMessage('DEBUG: ' + message)
        : console.debug(message)
      : undefined;

  public info = (message: string) =>
    this.checkLogLevel('info')
      ? this.diagnosticLogging
        ? this.homeyLogger.captureMessage('INFO: ' + message)
        : console.info(message)
      : undefined;

  public warn = (message: string) =>
    this.checkLogLevel('warn')
      ? this.diagnosticLogging
        ? this.homeyLogger.captureMessage('WARN: ' + message)
        : console.warn(message)
      : undefined;

  public error = (message: string) =>
    this.checkLogLevel('error')
      ? this.diagnosticLogging
        ? this.homeyLogger.captureMessage('ERROR: ' + message)
        : console.error(message)
      : undefined;

  public exception = (error: Error) =>
    this.checkLogLevel('error')
      ? this.diagnosticLogging
        ? this.homeyLogger.captureException(error)
        : console.error(error.message)
      : undefined;
}
