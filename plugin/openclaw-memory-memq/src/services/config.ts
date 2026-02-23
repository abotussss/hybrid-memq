export function getCfg<T>(api: any, key: string, fallback: T): T {
  const fromPluginConfig = api?.pluginConfig?.[key];
  if (fromPluginConfig !== undefined) return fromPluginConfig as T;

  const fromRoot = key.split('.').reduce((acc: any, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), api?.config);
  if (fromRoot !== undefined) return fromRoot as T;

  return fallback;
}

export function logInfo(api: any, msg: string): void {
  const logger = api?.logger;
  if (logger?.info) logger.info(msg);
  else console.log(msg);
}
