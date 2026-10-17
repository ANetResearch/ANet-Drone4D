// Name of the web client (M11): the realtime hello `client` and the REST header `X-AWR-Client` on every request of the page.
// The sandbox server counts session activity without it (SDK, scripts, other tools) as external API activity and publishes
// its latest time in `sandbox/session.api_activity_unix_ns`; the page's power-save idle clock follows it (issue #59), while
// the page's own background requests do not hold off power saving.
export const CLIENT_NAME = 'awr-web/0.1.0'
export const CLIENT_HEADER = 'x-awr-client'
