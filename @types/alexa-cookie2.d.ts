declare module 'alexa-cookie2' {
  /**
   * Configuration options for generating Alexa cookies.
   */
  interface AlexaCookieOptions {
    logger?: (message?: unknown, ...optionalParams: unknown[]) => void;
    /**
     * Required if proxy enabled: provide the own IP with which you later access the proxy.
     * Providing/Using a hostname here can lead to issues!
     * Needed to set up all rewriting and proxy stuff internally
     */
    proxyOwnIp?: string;
    /**  possible to use with different countries, @default 'amazon.de' */
    amazonPage?: string;
    /** webpage language, should match to amazon-Page @default'de-DE' */
    acceptLanguage?: string;
    /** own userAgent to use for all request, overwrites default one, should not be needed */
    userAgent?: string;
    /** should only the proxy method be used? When no email/password are provided this will set to true automatically @default false */
    proxyOnly?: boolean;
    /** should the library setup a proxy to get cookie when automatic way did not worked? @default false */
    setupProxy?: boolean;
    /** use this port for the proxy, default is 0 means random port is selected */
    proxyPort?: number;
    /** set this to bind the proxy to a special IP @default '0.0.0.0' */
    proxyListenBind?: string;
    /** Loglevel of Proxy @default 'warn' */
    proxyLogLevel?: 'debug' | 'info' | 'warn' | 'error';
    /** Change the Proxy Amazon Page - all "western countries" directly use amazon.com including Australia! Change to amazon.co.jp for Japan */
    baseAmazonPage?: string;
    /** language to be used for the Amazon Sign-in page the proxy calls @default 'de_DE' */
    amazonPageProxyLanguage?: string;
    /** name of the device app name which will be registered with Amazon, leave empty to use a default one */
    deviceAppName?: string;
    /** overwrite path where some of the formerRegistrationData are persisted to optimize against Amazon security measures */
    formerDataStorePath?: string;
    /** provide the result object from subsequent proxy usages here and some generated data will be reused for next proxy call too */
    formerRegistrationData?: Record;
    /** use in order to override the default html displayed when the proxy window can be closed
     * @default '<b>Amazon Alexa Cookie successfully retrieved. You can close the browser.</b>'
     */
    proxyCloseWindowHTML?: string;
  }

  /**
   * Generates Alexa cookies.
   * @param email - The email address.
   * @param password - The password.
   * @param options - The configuration options.
   * @param callback - The callback function.
   */
  export function generateAlexaCookie(
    email: string,
    password: string,
    options: AlexaCookieOptions,
    callback?: (error: unknown, result: { cookie: string; csrf: string }) => void,
  ): void;
}
