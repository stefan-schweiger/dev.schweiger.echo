/* eslint-disable @typescript-eslint/no-explicit-any */
// This is a helper for the settings page to handle potential connection errors correctly
export const connect = async ({ homey }: any) => {
  return await homey.app.connect();
};

export const disconnect = async ({ homey }: any) => {
  return await homey.app.disconnect();
};

export const status = async ({ homey }: any) => {
  return await homey.app.status();
};

export const reset = async ({ homey }: any) => {
  return await homey.app.reset();
};
