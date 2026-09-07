// Present v3 envelopes in the legacy shape used by behavioral collector fixtures.
// Wire-contract tests inspect the original envelope separately.
function unpackSinkMessage(message) {
  if (message.schema_version !== 3) return message;
  return {
    schema_version: 2,
    ...message.payload,
    ...(message.kind === 'list_chunk' ? {} : {[message.kind]: true}),
    ...(message.kind.startsWith('archive_') ? {archive_run: message.run_id} : {}),
  };
}

module.exports = {unpackSinkMessage};
