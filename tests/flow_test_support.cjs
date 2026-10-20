// Only expose dependencies of the actual upstream functions under test.
const vm = require('node:vm');
exports.installDownloadHelpers = (context, source) => {
  context.URL ||= URL;
  context.discoveredVideoUrlsById ||= new Map();
  context.inFlightOrCompletedDownloads ||= new Set();
  context.downloadedUrlsSet ||= new Set();
  context.tileHoverHistory ||= new WeakMap();
  context.batchPromise ??= null;
  const start = source.indexOf('  function isValidFlowVideoUrl(');
  const end = source.indexOf('  function videoIdentity(', start);
  if (start < 0 || end < start) throw Error('upstream video validation seam missing');
  vm.runInContext(source.slice(start, end), context);
  const receiptsStart = source.indexOf('  const managedReadyReceipts = new Map();');
  const receiptsEnd = source.indexOf('  let state = {', receiptsStart);
  if (receiptsStart >= 0) vm.runInContext(source.slice(receiptsStart, receiptsEnd), context);
};
