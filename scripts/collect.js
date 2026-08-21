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
    match_mode: matchMode = 'all',
    start_page: startPage = 1,
    page_size: pageSize = 500,
    max_pages: maxPages = 2000,
    pages_before: pagesBefore = 0,
    checkpoint_pages: checkpointPages = 500,
    cache_chunk_pages: cacheChunkPages = 5,
    request_concurrency: requestConcurrency = 2,
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
    !Number.isInteger(requestConcurrency) ||
    requestConcurrency < 1 ||
    requestConcurrency > 4 ||
    pageSize !== 500 ||
    delayMinMs !== 600 ||
    delayMaxMs !== 2000 ||
    typeof sinkUrl !== 'string' ||
    !sinkUrl.startsWith('http://127.0.0.1:') ||
    (minComments !== null &&
      (!Number.isInteger(minComments) || minComments < 0)) ||
    (minFavorites !== null &&
      (!Number.isInteger(minFavorites) || minFavorites < 0)) ||
    !['all', 'any'].includes(matchMode) ||
    (matchMode === 'any' && (minComments === null || minFavorites === null)) ||
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
        matchMode,
        startPage,
        pageSize,
        maxPages,
        pagesBefore,
        checkpointPages,
        cacheChunkPages,
        requestConcurrency,
        delayMinMs,
        delayMaxMs,
        sinkUrl,
      }) => {
        const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
        const jitter = () =>
          delayMinMs + Math.floor(Math.random() * (delayMaxMs - delayMinMs + 1));
        const newTelemetry = () => ({
          list_requests: 0,
          detail_requests: 0,
          request_ms: 0,
          pacing_ms: 0,
          retry_backoff_ms: 0,
          response_chars: 0,
          wall_ms: 0,
          throttle_responses: 0,
          concurrency_reductions: 0,
          max_in_flight: 0,
          overfetch_pages: 0,
        });
        let chunkTelemetry = newTelemetry();
        let chunkWallStartedAt = performance.now();
        let activeRequests = 0;
        let cooldownUntil = 0;
        let effectiveConcurrency = requestConcurrency;

        const pacingSleep = async () => {
          const milliseconds = jitter();
          chunkTelemetry.pacing_ms += milliseconds;
          await sleep(milliseconds);
        };

        const waitForSharedCooldown = async () => {
          while (cooldownUntil > Date.now()) {
            const milliseconds = cooldownUntil - Date.now();
            chunkTelemetry.retry_backoff_ms += milliseconds;
            await sleep(milliseconds);
          }
        };

        const requestJson = async (url, label) => {
          let result = null;
          for (let attempt = 0; attempt < 3; attempt += 1) {
            await waitForSharedCooldown();
            const startedAt = performance.now();
            const requestCounter = label.startsWith('list ') ? 'list_requests' : 'detail_requests';
            chunkTelemetry[requestCounter] += 1;
            activeRequests += 1;
            chunkTelemetry.max_in_flight = Math.max(
              chunkTelemetry.max_in_flight,
              activeRequests,
            );
            try {
              const response = await fetch(url, { headers: authHeaders });
              const text = await response.text();
              chunkTelemetry.response_chars += text.length;
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
            } finally {
              activeRequests -= 1;
              chunkTelemetry.request_ms += Math.max(0, Math.round(performance.now() - startedAt));
            }

            const transient =
              result.status === 0 || result.status === 429 || result.status >= 500;
            if (result.status === 429) {
              chunkTelemetry.throttle_responses += 1;
              if (effectiveConcurrency > 1) {
                effectiveConcurrency -= 1;
                chunkTelemetry.concurrency_reductions += 1;
              }
            }
            if (!transient || attempt === 2) break;
            const serverDelay = Number(result.retryAfter || 0) * 1000;
            const backoff = Math.max(serverDelay, 15_000 * 2 ** attempt);
            const backoffMilliseconds = Math.min(60_000, backoff);
            cooldownUntil = Math.max(cooldownUntil, Date.now() + backoffMilliseconds);
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

        const mapLimit = async (items, limit, worker) => {
          let nextIndex = 0;
          const runners = Array.from(
            { length: Math.min(limit, items.length) },
            async () => {
              while (nextIndex < items.length) {
                const currentIndex = nextIndex;
                nextIndex += 1;
                await worker(items[currentIndex], currentIndex);
              }
            },
          );
          await Promise.all(runners);
        };

        const nonNegativeInteger = (rawValue, fallback = null) => {
          if (rawValue === null || rawValue === undefined || rawValue === '') return fallback;
          const value = Number(rawValue);
          return Number.isInteger(value) && value >= 0 ? value : fallback;
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
        let reachedStart = false;
        let feedExhausted = false;
        let chunkStartPage = startPage;
        let chunkScanned = 0;
        let pendingRows = [];
        let pendingMatchedPids = new Set();
        let pendingUnavailableByPid = new Map();
        const matchesThresholds = (post) => {
          const conditions = [];
          if (minComments !== null) conditions.push(post.reply > minComments);
          if (minFavorites !== null) {
            conditions.push(post.favorites !== null && post.favorites > minFavorites);
          }
          return matchMode === 'any' ? conditions.some(Boolean) : conditions.every(Boolean);
        };

        while (pages < maxPages && !reachedStart && !feedExhausted) {
          const batchSize = Math.min(effectiveConcurrency, maxPages - pages);
          const firstBatchPage = startPage + pages;
          const pageNumbers = Array.from(
            { length: batchSize },
            (_value, index) => firstBatchPage + index,
          );
          const pageResults = await Promise.all(
            pageNumbers.map(async (pageNumber) => {
              await pacingSleep();
              const listJson = await requestJson(
                `${endpoint}?page=${pageNumber}&limit=${pageSize}&comment_limit=0&comment_stream=1`,
                `list page ${pageNumber}`,
              );
              return { pageNumber, posts: listJson?.data?.list || [] };
            }),
          );

          for (let batchIndex = 0; batchIndex < pageResults.length; batchIndex += 1) {
            const { pageNumber, posts } = pageResults[batchIndex];
            pages += 1;
            scanned += posts.length;
            chunkScanned += posts.length;

            if (!posts.length) {
              feedExhausted = true;
              reachedStart = true;
            }

            const rowsByPid = new Map();
            const detailsFetchedPids = new Set();
            for (const post of posts) {
              const row = {
                pid: String(post.pid),
                timestamp: Number(post.timestamp),
                reply: Number(post.reply),
                favorites: nonNegativeInteger(post.likenum),
                type: post.type || 'text',
                text: post.text || '',
              };
              rowsByPid.set(row.pid, row);
              pendingRows.push(row);
            }

            const fetchAndApplyDetail = async (post, favoritesFallback) => {
              await pacingSleep();
              const detailJson = await requestJson(
                `/chapi/api/v3/hole/one?pid=${encodeURIComponent(post.pid)}&comment_stream=1`,
                `detail #${post.pid}`,
              );
              const hole = detailJson?.data?.hole || {};
              post.text = hole.text || post.text;
              post.type = hole.type || post.type;
              post.reply = Number(hole.reply ?? post.reply);
              post.favorites = nonNegativeInteger(hole.likenum, favoritesFallback);
              detailsFetchedPids.add(post.pid);
              if (minFavorites !== null && post.favorites === null) {
                pendingUnavailableByPid.set(post.pid, {
                  pid: post.pid,
                  reason: 'detail_missing',
                });
              }
            };

            const missingFavorites = [...rowsByPid.values()].filter(
              (post) => minFavorites !== null && post.favorites === null,
            );
            await mapLimit(missingFavorites, effectiveConcurrency, async (post) => {
              await fetchAndApplyDetail(post, null);
            });

            let pageMatches = [...rowsByPid.values()].filter(
              (row) =>
                row.timestamp >= reportStartTimestamp &&
                row.timestamp < endTimestamp &&
                matchesThresholds(row),
            );

            const missingText = pageMatches.filter(
              (post) =>
                !post.text.trim() &&
                !detailsFetchedPids.has(post.pid),
            );
            await mapLimit(missingText, effectiveConcurrency, async (post) => {
              await fetchAndApplyDetail(post, post.favorites);
            });
            pageMatches = pageMatches.filter(matchesThresholds);
            for (const post of pageMatches) pendingMatchedPids.add(post.pid);

            const oldest = Number(posts.at(-1)?.timestamp || 0);
            if (oldest && oldest < scanStartTimestamp) reachedStart = true;
            const terminal = reachedStart || feedExhausted || pages === maxPages;
            if (terminal && batchIndex + 1 < pageResults.length) {
              chunkTelemetry.overfetch_pages += pageResults.length - batchIndex - 1;
            }
            const checkpoint =
              (pagesBefore + pages) % checkpointPages === 0 || terminal;
            const chunkFull = pages % cacheChunkPages === 0;

            if (chunkFull || terminal || checkpoint) {
              const result = terminal
                ? {
                    batch_end_page: pageNumber,
                    next_page: pageNumber + 1,
                    pages,
                    scanned,
                    reached_start: reachedStart,
                    feed_exhausted: feedExhausted,
                  }
                : null;
              chunkTelemetry.wall_ms = Math.max(
                0,
                Math.round(performance.now() - chunkWallStartedAt),
              );
              await sendToSink({
                schema_version: 2,
                start_page: chunkStartPage,
                end_page: pageNumber,
                pages: pageNumber - chunkStartPage + 1,
                scanned: chunkScanned,
                oldest,
                reached_start: reachedStart,
                feed_exhausted: feedExhausted,
                checkpoint,
                terminal,
                result,
                rows: pendingRows,
                matched_pids: [...pendingMatchedPids],
                favorite_unavailable: [...pendingUnavailableByPid.values()],
                telemetry: chunkTelemetry,
              });
              chunkStartPage = pageNumber + 1;
              chunkScanned = 0;
              pendingRows = [];
              pendingMatchedPids = new Set();
              pendingUnavailableByPid = new Map();
              chunkTelemetry = newTelemetry();
              chunkWallStartedAt = performance.now();
            }
            if (terminal) break;
          }
        }

        return null;
      },
      {
        endpoint,
        authHeaders,
        reportStartTimestamp,
        scanStartTimestamp,
        endTimestamp,
        minComments,
        minFavorites,
        matchMode,
        startPage,
        pageSize,
        maxPages,
        pagesBefore,
        checkpointPages,
        cacheChunkPages,
        requestConcurrency,
        delayMinMs,
        delayMaxMs,
        sinkUrl,
      },
    );
}
