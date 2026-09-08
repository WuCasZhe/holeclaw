const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {unpackSinkMessage} = require('./sink_protocol_fixture');

const collectorPath = path.join(__dirname, '..', 'scripts', 'collect.js');
const collectorSource = fs.readFileSync(collectorPath, 'utf8');

const response = (body, { status = 200, retryAfter = null } = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: {
    get: (name) => (name.toLowerCase() === 'retry-after' ? retryAfter : null),
  },
  text: async () => JSON.stringify(body),
});

async function runCollector({ config, remoteFetch, sinkFetch, wireSinkFetch, onSleep = () => {}, deadlineMs, clock = Date }) {
  const sinkPayloads = [];
  const wirePayloads = [];
  const immediateTimer = (callback, milliseconds = 0) => {
    onSleep(milliseconds);
    callback();
    return 0;
  };
  const fetch = async (url, options = {}) => {
    if (String(url).startsWith(config.sink_url) || String(url).startsWith(config.sink_url.replace('/ingest?', '/media?'))) {
      const binary = options.headers?.['X-Holeclaw-Message'];
      const wire = JSON.parse(binary ? decodeURIComponent(binary) : options.body);
      wirePayloads.push(wire);
      const payload = unpackSinkMessage(wire);
      if (binary) payload.data = Buffer.from(await options.body.arrayBuffer()).toString('base64');
      sinkPayloads.push(payload);
      if (wireSinkFetch) return wireSinkFetch(url, options);
      return sinkFetch ? sinkFetch(url, {...options, body: JSON.stringify(payload)}) : response({ ok: true });
    }
    return remoteFetch(url, options);
  };
  const collector = vm.runInNewContext(collectorSource, {
    fetch, setTimeout: immediateTimer, clearTimeout, AbortController, URL, performance, Blob,
    AbortSignal: {timeout: ms => AbortSignal.timeout(deadlineMs ?? ms)},
    Date: clock, Math, btoa,
  }, {filename: collectorPath});

  let evaluateCount = 0;
  const page = {
    evaluate: async (fn, argument) => {
      evaluateCount += 1;
      return evaluateCount === 1 ? config : fn(argument);
    },
    waitForRequest: async () => ({
      allHeaders: async () => ({ authorization: 'test-token' }),
    }),
    reload: async () => undefined,
  };

  const result = await collector(page);
  return { result, wirePayloads, sinkPayloads: sinkPayloads.filter(payload => !payload.telemetry_final),
    telemetryPayloads: sinkPayloads.filter(payload => payload.telemetry_final) };
}

module.exports = { response, runCollector };
