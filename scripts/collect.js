async (page) => {
  const CONFIG_KEY = 'codex_pku_digest_config';
  const endpoint = '/chapi/api/v3/hole/list_comments';

  const config = await page.evaluate((key) => {
    const raw = sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  }, CONFIG_KEY);
  if (!config) throw new Error('Missing collector configuration.');

  const {
    report_start_timestamp: reportStartTimestamp,
    scan_start_timestamp: scanStartTimestamp,
    end_timestamp: endTimestamp,
    min_comments: minComments,
    min_favorites: minFavorites,
    start_page: startPage = 1,
    page_size: pageSize = 500,
    max_pages: maxPages = 2000,
    pages_before: pagesBefore = 0,
    checkpoint_pages: checkpointPages = 500,
    cache_chunk_pages: cacheChunkPages = 5,
    delay_min_ms: delayMinMs = 600,
    delay_max_ms: delayMaxMs = 2000,
    sink_url: sinkUrl,
  } = config;
  if (!(reportStartTimestamp <= scanStartTimestamp && scanStartTimestamp < endTimestamp)) {
    throw new Error('Invalid time window.');
  }
  if (
    !Number.isInteger(startPage) ||
    startPage < 1 ||
    !Number.isInteger(maxPages) ||
    maxPages < 1 ||
    maxPages > 5000 ||
    !Number.isInteger(checkpointPages) ||
    checkpointPages < 1 ||
    checkpointPages > 500 ||
    !Number.isInteger(cacheChunkPages) ||
    cacheChunkPages < 1 ||
    cacheChunkPages > 20 ||
    pageSize !== 500 ||
    delayMinMs !== 600 ||
    delayMaxMs !== 2000 ||
    typeof sinkUrl !== 'string' ||
    !sinkUrl.startsWith('http://127.0.0.1:') ||
    (minComments !== null &&
      (!Number.isInteger(minComments) || minComments < 0)) ||
    (minFavorites !== null &&
      (!Number.isInteger(minFavorites) || minFavorites < 0)) ||
    (minComments === null && minFavorites === null)
  ) {
    throw new Error('Unsafe request configuration.');
  }

  const normalListRequest = page.waitForRequest(
    (request) => request.url().includes(endpoint) && request.url().includes('limit=10'),
    { timeout: 20_000 },
  );
  await page.reload({ waitUntil: 'domcontentloaded' });
  const observedRequest = await normalListRequest;
  const observedHeaders = await observedRequest.allHeaders();
  const authHeaders = {};
  for (const name of ['authorization', 'x-xsrf-token', 'uuid']) {
    if (observedHeaders[name]) authHeaders[name] = observedHeaders[name];
  }
  if (!authHeaders.authorization) {
    throw new Error('Authenticated request header was not available. Login again.');
  }

  return await page.evaluate(
      async ({
        endpoint,
        authHeaders,
        reportStartTimestamp,
        scanStartTimestamp,
        endTimestamp,
        minComments,
        minFavorites,
        startPage,
        pageSize,
        maxPages,
        pagesBefore,
        checkpointPages,
        cacheChunkPages,
        delayMinMs,
        delayMaxMs,
        sinkUrl,
      }) => {
        const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
        const jitter = () =>
          delayMinMs + Math.floor(Math.random() * (delayMaxMs - delayMinMs + 1));

        const requestJson = async (url, label) => {
          let result = null;
          for (let attempt = 0; attempt < 3; attempt += 1) {
            try {
              const response = await fetch(url, { headers: authHeaders });
              const text = await response.text();
              let json = null;
              try {
                json = JSON.parse(text);
              } catch {
                // Report a short preview only; never expose request headers.
              }
              result = {
                status: response.status,
                retryAfter: response.headers.get('retry-after'),
                json,
                preview: json ? '' : text.slice(0, 80),
              };
            } catch (error) {
              result = {
                status: 0,
                retryAfter: null,
                json: null,
                preview: error?.name || 'NetworkError',
              };
            }

            const transient =
              result.status === 0 || result.status === 429 || result.status >= 500;
            if (!transient || attempt === 2) break;
            const serverDelay = Number(result.retryAfter || 0) * 1000;
            const backoff = Math.max(serverDelay, 15_000 * 2 ** attempt);
            await sleep(Math.min(60_000, backoff));
          }
          if (result.status === 401 || result.status === 403) {
            throw new Error(`Authentication expired while loading ${label} (${result.status}).`);
          }
          if (result.status !== 200 || result.json?.code !== 20000) {
            throw new Error(
              `${label} failed: HTTP ${result.status}, code ${result.json?.code}, ${result.preview}`,
            );
          }
          return result.json;
        };

        const sendToSink = async (payload) => {
          let lastError = null;
          for (let attempt = 0; attempt < 3; attempt += 1) {
            try {
              const response = await fetch(sinkUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
                body: JSON.stringify(payload),
              });
              if (response.ok) return;
              lastError = new Error(`Local cache sink returned HTTP ${response.status}.`);
            } catch (error) {
              lastError = error;
            }
            await sleep(250 * 2 ** attempt);
          }
          throw new Error(`Local cache sink failed: ${String(lastError)}`);
        };

        let pages = 0;
        let scanned = 0;
        let detailsRequested = 0;
        let reachedStart = false;
        let feedExhausted = false;
        let lastPage = startPage - 1;
        let chunkStartPage = startPage;
        let chunkScanned = 0;
        let chunkDetails = 0;
        let pendingRows = [];
        let pendingMatches = [];
        const matchesThresholds = (post) =>
          (minComments === null || post.reply > minComments) &&
          (minFavorites === null || post.favorites > minFavorites);

        for (let offset = 0; offset < maxPages; offset += 1) {
          const pageNumber = startPage + offset;
          await sleep(jitter());
          const listJson = await requestJson(
            `${endpoint}?page=${pageNumber}&limit=${pageSize}&comment_limit=0&comment_stream=1`,
            `list page ${pageNumber}`,
          );
          const posts = listJson?.data?.list || [];
          pages += 1;
          lastPage = pageNumber;
          scanned += posts.length;
          chunkScanned += posts.length;

          if (!posts.length) {
            feedExhausted = true;
            reachedStart = true;
          }

          const rowsByPid = new Map();
          for (const post of posts) {
            const rawFavorites = post.likenum;
            const favorites =
              rawFavorites === null || rawFavorites === undefined || rawFavorites === ''
                ? null
                : Number(rawFavorites);
            if (
              (favorites !== null && (!Number.isInteger(favorites) || favorites < 0)) ||
              (minFavorites !== null && favorites === null)
            ) {
              throw new Error(
                `Favorite count (likenum) is unavailable or invalid on list page ${pageNumber}.`,
              );
            }
            const row = {
              pid: String(post.pid),
              timestamp: Number(post.timestamp),
              reply: Number(post.reply),
              favorites,
              type: post.type || 'text',
              text: post.text || '',
              source_page: pageNumber,
            };
            rowsByPid.set(row.pid, row);
            pendingRows.push(row);
            if (
              row.timestamp >= reportStartTimestamp &&
              row.timestamp < endTimestamp &&
              matchesThresholds(row)
            ) {
              pendingMatches.push({ ...row });
            }
          }

          const missingText = pendingMatches.filter(
            (post) => post.source_page === pageNumber && !post.text.trim(),
          );
          for (const post of missingText) {
            await sleep(jitter());
            const detailJson = await requestJson(
              `/chapi/api/v3/hole/one?pid=${encodeURIComponent(post.pid)}&comment_stream=1`,
              `detail #${post.pid}`,
            );
            const hole = detailJson?.data?.hole || {};
            post.text = hole.text || post.text;
            post.type = hole.type || post.type;
            post.reply = Number(hole.reply ?? post.reply);
            const detailFavorites =
              hole.likenum === null || hole.likenum === undefined || hole.likenum === ''
                ? post.favorites
                : Number(hole.likenum);
            if (
              detailFavorites !== null &&
              (!Number.isInteger(detailFavorites) || detailFavorites < 0)
            ) {
              throw new Error(`Favorite count (likenum) is invalid for detail #${post.pid}.`);
            }
            post.favorites = detailFavorites;
            const cached = rowsByPid.get(post.pid);
            if (cached) {
              cached.text = post.text;
              cached.type = post.type;
              cached.reply = post.reply;
              cached.favorites = post.favorites;
            }
            detailsRequested += 1;
            chunkDetails += 1;
          }
          pendingMatches = pendingMatches.filter(
            (post) => post.source_page !== pageNumber || matchesThresholds(post),
          );

          const oldest = Number(posts.at(-1)?.timestamp || 0);
          if (oldest && oldest < scanStartTimestamp) reachedStart = true;
          const terminal = reachedStart || feedExhausted || offset + 1 === maxPages;
          const checkpoint =
            (pagesBefore + pages) % checkpointPages === 0 || terminal;
          const chunkFull = pages % cacheChunkPages === 0;

          if (chunkFull || terminal || checkpoint) {
            const result = terminal
              ? {
                  collected_at: new Date().toISOString(),
                  batch_start_page: startPage,
                  batch_end_page: pageNumber,
                  next_page: pageNumber + 1,
                  pages,
                  scanned,
                  details_requested: detailsRequested,
                  reached_start: reachedStart,
                  feed_exhausted: feedExhausted,
                }
              : null;
            await sendToSink({
              schema_version: 1,
              start_page: chunkStartPage,
              end_page: pageNumber,
              pages: pageNumber - chunkStartPage + 1,
              scanned: chunkScanned,
              details_requested: chunkDetails,
              oldest,
              reached_start: reachedStart,
              feed_exhausted: feedExhausted,
              checkpoint,
              terminal,
              result,
              rows: pendingRows,
              matches: pendingMatches,
            });
            chunkStartPage = pageNumber + 1;
            chunkScanned = 0;
            chunkDetails = 0;
            pendingRows = [];
            pendingMatches = [];
          }
          if (reachedStart || feedExhausted) break;
        }

        return {
          collected_at: new Date().toISOString(),
          batch_start_page: startPage,
          batch_end_page: lastPage,
          next_page: lastPage + 1,
          pages,
          scanned,
          details_requested: detailsRequested,
          reached_start: reachedStart,
          feed_exhausted: feedExhausted,
        };
      },
      {
        endpoint,
        authHeaders,
        reportStartTimestamp,
        scanStartTimestamp,
        endTimestamp,
        minComments,
        minFavorites,
        startPage,
        pageSize,
        maxPages,
        pagesBefore,
        checkpointPages,
        cacheChunkPages,
        delayMinMs,
        delayMaxMs,
        sinkUrl,
      },
    );
}
