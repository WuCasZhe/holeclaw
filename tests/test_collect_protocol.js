const assert = require('node:assert/strict');
const {runCollector, response} = require('./collector_harness');

(async () => {
  const bodies = [];
  let lostResponse = false;
  const result = await runCollector({config: {
    archive_run: 'protocol-fixture', report_start_timestamp: 100, scan_start_timestamp: 100,
    end_timestamp: 200, min_comments: 0, min_favorites: null, max_pages: 1,
    request_concurrency: 1, sink_url: 'http://127.0.0.1:12345/ingest?token=fixture',
  }, remoteFetch: async () => response({code: 20000, data: {list: [
    {pid: '1', timestamp: 150, reply: 1, likenum: 0, text: 'fixture'},
  ]}}), wireSinkFetch: async (_url, options) => {
    bodies.push(options.body);
    if (!lostResponse) {
      lostResponse = true;
      throw new Error('simulated lost response after commit');
    }
    return response({ok: true});
  }});
  assert.equal(bodies[0], bodies[1], 'retry must preserve identity, telemetry and serialized body');
  assert.deepEqual(result.wirePayloads.map(p => p.request_id), [1, 1, 2]);
  for (const message of result.wirePayloads) {
    assert.equal(message.schema_version, 3);
    assert.equal(message.run_id, 'protocol-fixture');
    assert.deepEqual(Object.keys(message).sort(), ['kind', 'payload', 'request_id', 'run_id', 'schema_version']);
    assert.ok(!Object.hasOwn(message.payload, 'schema_version'));
  }
  console.log('collector wire protocol tests: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
