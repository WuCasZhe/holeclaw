const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const collectorPath = path.join(__dirname, '..', 'scripts', 'collect.js');
const collector = vm.runInThisContext(fs.readFileSync(collectorPath, 'utf8'), {
  filename: collectorPath,
});

const response = (body, { status = 200, retryAfter = null } = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: {
    get: (name) => (name.toLowerCase() === 'retry-after' ? retryAfter : null),
  },
  text: async () => JSON.stringify(body),
});

async function runCollector({ config, remoteFetch, onSleep = () => {} }) {
  const sinkPayloads = [];
  const originalFetch = global.fetch;
  const originalSetTimeout = global.setTimeout;
  global.setTimeout = (callback, milliseconds = 0) => {
    onSleep(milliseconds);
    callback();
    return 0;
  };
  global.fetch = async (url, options = {}) => {
    if (String(url).startsWith(config.sink_url)) {
      sinkPayloads.push(JSON.parse(options.body));
      return response({ ok: true });
    }
    return remoteFetch(url, options);
  };

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

  try {
    const result = await collector(page);
    return { result, sinkPayloads };
  } finally {
    global.fetch = originalFetch;
    global.setTimeout = originalSetTimeout;
  }
}

module.exports = { response, runCollector };
