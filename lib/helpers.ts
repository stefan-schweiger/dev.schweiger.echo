/* eslint-disable @typescript-eslint/no-explicit-any */
import { CallbackWithErrorAndBody } from 'alexa-remote2';

export function promisify<T = any>(fn: (callback: CallbackWithErrorAndBody) => unknown): Promise<T> {
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

export function promisifyWithOptions<T = any, TOptions = any>(
  fn: (options: TOptions, callback: CallbackWithErrorAndBody) => unknown,
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

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
