import { CallbackWithErrorAndBody } from 'alexa-remote2';

export function promisify<T extends any = unknown>(fn: (callback: CallbackWithErrorAndBody) => any): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    fn((error, result) => {
      if (error) {
        reject(error);
      } else {
        resolve(result as T);
      }
    });
  });
}

export function promisifyWithOptions<T extends any = unknown, TOptions extends any = any>(
  fn: (options: TOptions, callback: CallbackWithErrorAndBody) => any,
  options: TOptions,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    fn(options, (error, result) => {
      if (error) {
        reject(error);
      } else {
        resolve(result as T);
      }
    });
  });
}
