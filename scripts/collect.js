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
    max_pages: maxPages = null,
    pages_before: pagesBefore = 0,
    checkpoint_pages: checkpointPages = 100,
    cache_chunk_pages: cacheChunkPages = 1,
    request_concurrency: requestConcurrency = 8,
    delay_min_ms: delayMinMs = 600,
    delay_max_ms: delayMaxMs = 2000,
    sink_url: sinkUrl,
    archive: archive = false,
    archive_run: archiveRun = null,
    archive_cached_pages: archiveCachedPages = 0,
    archive_cache_only: archiveCacheOnly = false,
    comment_batch_pages: commentBatchPages = 10,
    comment_page_size: commentPageSize = 100,
    extract_images: extractImages = false,
    download_images: downloadImages = false,
    control_url: controlUrl = null,
  } = config;
  if (!(reportStartTimestamp <= scanStartTimestamp && scanStartTimestamp < endTimestamp)) {
    throw new Error('Invalid time window.');
  }
  if (
    (controlUrl !== null && controlUrl !== sinkUrl.replace('/ingest?', '/control?')) ||
    !Number.isInteger(commentBatchPages) || commentBatchPages < 1 || commentBatchPages > 20 ||
    commentPageSize !== 100 ||
    !Number.isInteger(archiveCachedPages) || archiveCachedPages < 0 ||
    !Number.isInteger(startPage) ||
    startPage < 1 ||
    (maxPages !== null &&
      (!Number.isSafeInteger(maxPages) || maxPages < 1)) ||
    !Number.isInteger(checkpointPages) ||
    checkpointPages < 1 ||
    checkpointPages > 500 ||
    !Number.isInteger(cacheChunkPages) ||
    cacheChunkPages < 1 ||
    cacheChunkPages > 20 ||
    !Number.isInteger(requestConcurrency) ||
    requestConcurrency < 1 ||
    requestConcurrency > 8 ||
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
    (!archive && minComments === null && minFavorites === null)
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
        archive,
        archiveRun,
        archiveCachedPages,
        archiveCacheOnly,
        commentBatchPages,
        commentPageSize,
        extractImages,
        downloadImages,
        controlUrl,
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
        const controller = new AbortController();
        const controlController = new AbortController();
        let finished = false;
        let settleWork = async () => {};
        const controlRequest = controlUrl ? fetch(controlUrl, { signal: controlController.signal })
          .then(async (response) => {
            if (!response.ok || JSON.parse(await response.text()).cancelled) controller.abort();
          }).catch(() => { if (!finished) controller.abort(); }) : null;
        try {
        // Keep cancellation and per-attempt deadlines separate: a timeout may
        // retry, whereas cancelling the collection must stop every worker.
        const withDeadline = async (operation, milliseconds = 60_000, parent = controller.signal) => {
          const attempt = new AbortController();
          const deadline = AbortSignal.timeout(milliseconds);
          const abort = () => attempt.abort(parent.aborted ? parent.reason : deadline.reason);
          parent.addEventListener('abort', abort, {once: true});
          deadline.addEventListener('abort', abort, {once: true});
          if (parent.aborted) abort();
          let rejectAbort;
          const aborted = new Promise((_, reject) => {
            rejectAbort = () => reject(attempt.signal.reason);
            attempt.signal.addEventListener('abort', rejectAbort, {once: true});
            if (attempt.signal.aborted) rejectAbort();
          });
          try {
            return await Promise.race([aborted, operation(attempt.signal)]);
          } finally {
            parent.removeEventListener('abort', abort);
            deadline.removeEventListener('abort', abort);
            attempt.signal.removeEventListener('abort', rejectAbort);
            attempt.abort();
          }
        };
        const sleep = (milliseconds) => new Promise((resolve, reject) => {
          const signal = controller.signal;
          if (signal.aborted) { reject(new Error('Collector cancelled.')); return; }
          let timer;
          const abort = () => { clearTimeout(timer); reject(new Error('Collector cancelled.')); };
          signal.addEventListener('abort', abort, { once: true });
          timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, milliseconds);
        });
        const createTelemetry = (now) => {
          const empty = () => ({
            list_requests: 0,
            detail_requests: 0,
            comment_requests: 0,
            image_requests: 0,
            image_bytes: 0,
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
          const counters = empty();
          let reported = empty();
          const startedAt = now();
          return {
            add(key, value = 1) { counters[key] += value; },
            observeMax(key, value) { counters[key] = Math.max(counters[key], value); },
            snapshot: () => ({...counters, wall_ms: Math.max(0, Math.round(now() - startedAt))}),
            delta: (snapshot) => Object.fromEntries(Object.entries(snapshot).map(([key, value]) =>
              [key, key === 'max_in_flight' ? value : value - reported[key]])),
            acknowledge(snapshot) { reported = snapshot; },
          };
        };
        const telemetry = createTelemetry(() => performance.now());
        const createRequestScheduler = ({fetch, sleep, clock, random, signal, telemetry}) => {
          let activeRequests = 0;
          let cooldownUntil = 0;
          let effectiveConcurrency = requestConcurrency;

          const pacingSleep = async () => {
            const milliseconds = delayMinMs + Math.floor(random() * (delayMaxMs - delayMinMs + 1));
            telemetry.add('pacing_ms', milliseconds);
            await sleep(milliseconds);
          };

          const waitForSharedCooldown = async () => {
            while (cooldownUntil > clock.now()) {
              const milliseconds = Math.min(60_000, cooldownUntil - clock.now());
              telemetry.add('retry_backoff_ms', milliseconds);
              await sleep(milliseconds);
            }
          };

          const retryAfterMilliseconds = (rawValue) => {
            if (!rawValue) return 0;
            const seconds = Number(rawValue);
            if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
            const retryAt = Date.parse(rawValue);
            return Number.isFinite(retryAt) ? Math.max(0, retryAt - clock.now()) : 0;
          };

          // Share permits across list prefetch, detail workers and retries.
          let occupiedSlots = 0;
          const requestWaiters = [];
          signal.addEventListener('abort', () => {
            while (requestWaiters.length) requestWaiters.shift()();
          });
          const grantRequestSlots = () => {
            while (requestWaiters.length && occupiedSlots < effectiveConcurrency) {
              occupiedSlots += 1;
              requestWaiters.shift()();
            }
          };
          const acquireRequestSlot = async () => {
            while (true) {
              signal.throwIfAborted();
              await waitForSharedCooldown();
              await new Promise((resolve) => {
                requestWaiters.push(resolve);
                grantRequestSlots();
              });
              signal.throwIfAborted();
              // A throttle may arrive while queued. Do not hold a stale permit
              // through its cooldown and then exceed the reduced concurrency.
              if (cooldownUntil <= clock.now() && occupiedSlots <= effectiveConcurrency) return;
              releaseRequestSlot();
            }
          };
          const releaseRequestSlot = () => {
            occupiedSlots -= 1;
            grantRequestSlots();
          };

          const requestJson = async (url, label, binary = false, allowMissing = false) => {
            let result = null;
            for (let attempt = 0; attempt < 3; attempt += 1) {
              await acquireRequestSlot();
              try {
                const startedAt = clock.monotonic();
                const requestCounter = label.startsWith('list ') ? 'list_requests' : 'detail_requests';
                telemetry.add(requestCounter);
                if (binary) telemetry.add('image_requests', 1);
                if (label.startsWith('detail comments ')) telemetry.add('comment_requests', 1);
                activeRequests += 1;
                telemetry.observeMax('max_in_flight', activeRequests);
                try {
                  await withDeadline(async requestSignal => {
                    const response = await fetch(url, { headers: authHeaders, signal: requestSignal });
                    if (binary && response.status === 200 && !response.headers.get('content-type')?.includes('json')) {
                      const reader = response.body.getReader();
                      const chunks = [];
                      let size = 0;
                      while (true) {
                        signal.throwIfAborted();
                        const {value, done} = await withDeadline(() => reader.read(), 30_000, requestSignal);
                        if (done) break;
                        size += value.length;
                        if (size > 20 * 1024 * 1024) {
                          await reader.cancel();
                          throw Object.assign(new Error('Image exceeds the 20 MiB limit.'), {permanent: true});
                        }
                        chunks.push(value);
                      }
                      telemetry.add('image_bytes', size);
                      result = {status: 200, file: {blob: new Blob(chunks), mime: response.headers.get('content-type') || ''}};
                    } else {
                      const text = await response.text();
                      telemetry.add('response_chars', text.length);
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
                    }
                  });
                } catch (error) {
                  signal.throwIfAborted();
                  if (error.permanent) throw error;
                  result = {
                    status: 0,
                    retryAfter: null,
                    json: null,
                    preview: error?.name || 'NetworkError',
                  };
                } finally {
                  activeRequests -= 1;
                  telemetry.add('request_ms', Math.max(0, Math.round(clock.monotonic() - startedAt)));
                }

                const transient =
                  result.status === 0 || result.status === 429 || result.status >= 500;
                if (result.status === 429) {
                  telemetry.add('throttle_responses', 1);
                  if (effectiveConcurrency > 1) {
                    effectiveConcurrency -= 1;
                    telemetry.add('concurrency_reductions', 1);
                  }
                }
                if (!transient || attempt === 2) break;
                const serverDelay = retryAfterMilliseconds(result.retryAfter);
                const backoffMilliseconds = Math.max(serverDelay, Math.min(60_000, 15_000 * 2 ** attempt));
                cooldownUntil = Math.max(cooldownUntil, clock.now() + backoffMilliseconds);
              } finally {
                releaseRequestSlot();
              }
            }
            if (result.status === 401 || result.status === 403) {
              throw new Error(`Authentication expired while loading ${label} (${result.status}).`);
            }
            if (allowMissing && (result.json?.code === 41001 || [404, 410].includes(result.status))) return {unavailable: true};
            if (binary && ([404, 410].includes(result.status) || result.json?.code === 41001)) return {status: 'unavailable'};
            if (binary && result.file) return result.file;
            if (binary || result.status !== 200 || result.json?.code !== 20000) {
              throw new Error(
                `${label} failed: HTTP ${result.status}, code ${result.json?.code}, ${result.preview}`,
              );
            }
            return result.json;
          };

          return {request: requestJson, pace: pacingSleep, get concurrency() { return effectiveConcurrency; }};
        };
        const scheduler = createRequestScheduler({fetch, sleep, telemetry, signal: controller.signal,
          clock: {now: () => Date.now(), monotonic: () => performance.now()}, random: () => Math.random()});
        const requestJson = scheduler.request;
        const pacingSleep = scheduler.pace;

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

        const mediaIds = (raw) => raw == null || raw === '' ? [] : (Array.isArray(raw) ? raw : String(raw).split(',')).map(String).map(x => x.trim()).filter(Boolean);

        const nonNegativeInteger = (rawValue, fallback = null) => {
          if (rawValue === null || rawValue === undefined || rawValue === '') return fallback;
          const value = Number(rawValue);
          return Number.isInteger(value) && value >= 0 ? value : fallback;
        };

        const createSinkClient = ({fetch, sleep, signal, url, runId}) => {
          let nextRequestId = 0;
          const send = async (kind, payload, blob = null) => {
            // Freeze the body: in-flight prefetch may update telemetry during retry.
            const body = JSON.stringify({schema_version: 3, kind, run_id: runId,
                request_id: ++nextRequestId, payload});
            let lastError = null;
            for (let attempt = 0; attempt < 3; attempt += 1) {
              try {
                const receipt = await withDeadline(async requestSignal => {
                  const response = await fetch(blob ? url.replace('/ingest?', '/media?') : url, {
                    signal: requestSignal,
                    method: 'POST',
                    headers: blob ? {'Content-Type': 'application/octet-stream',
                      'X-Holeclaw-Message': encodeURIComponent(body)} : { 'Content-Type': 'text/plain;charset=UTF-8' },
                    body: blob || body,
                  });
                  const text = await response.text();
                  let result;
                  try { result = JSON.parse(text); } catch {
                    throw new Error(`Local cache sink returned HTTP ${response.status}: invalid response`);
                  }
                  if (!response.ok || !result || result.ok === false) {
                    throw new Error(`Local cache sink returned HTTP ${response.status}: ${result?.error || 'request failed'}`);
                  }
                  return result;
                });
                return receipt;
              } catch (error) {
                signal.throwIfAborted();
                lastError = error;
              }
              await sleep(250 * 2 ** attempt);
            }
            throw new Error(`Local cache sink failed: ${String(lastError)}`);
          };

          return {send};
        };
        const sinkClient = createSinkClient({fetch, sleep, signal: controller.signal, url: sinkUrl, runId: archiveRun});
        const sendToSink = sinkClient.send;

        let pages = 0;
        let scanned = 0;
        let reachedStart = false;
        let feedExhausted = false;
        let chunkStartPage = startPage;
        let chunkScanned = 0;
        let pendingRows = [];
        let pendingMatchedPids = new Set();
        let pendingUnavailableByPid = new Map();
        let pendingDeferredPids = new Set();
        const matchesThresholds = (post) => {
          const conditions = [];
          if (minComments !== null) conditions.push(post.reply > minComments);
          if (minFavorites !== null) {
            conditions.push(post.favorites !== null && post.favorites > minFavorites);
          }
          return matchMode === 'any' ? conditions.some(Boolean) : conditions.every(Boolean);
        };

        const hasPageLimit = maxPages !== null;
        const pageLimit = archiveCacheOnly ? archiveCachedPages + 1 : hasPageLimit
          ? startPage + maxPages
          : Number.POSITIVE_INFINITY;
        let archiveRevision = 0, expiredRevision = -1;
        const postRevisions = new Map();
        const postFinished = pid => {
          postRevisions.delete(pid);
          postRevisions.set(pid, ++archiveRevision);
          if (postRevisions.size > 8192) {
            const [oldPid, revision] = postRevisions.entries().next().value;
            postRevisions.delete(oldPid);
            expiredRevision = revision;
          }
        };
        const resumeStillValid = (pid, revision) => revision > expiredRevision &&
          (postRevisions.get(pid) || 0) <= revision;
        const createListPipeline = ({getConcurrency, requestJson, pacingSleep, sendToSink}) => {
          const maxBufferedPages = Math.max(requestConcurrency * 2, requestConcurrency);
          const pageBuffer = new Map();
          const inFlightPages = new Map();
          let nextPageToFetch = startPage;
          let stopScheduling = false;
          // Probe the first remote page in both modes before expanding lookahead.
          let archiveScheduleThrough = Math.max(startPage, archiveCachedPages + 1);
          let archiveStopPage = Number.POSITIVE_INFINITY;

          const launchListPage = (pageNumber) => {
            const tracked = (async () => {
              if (archive && pageNumber <= archiveCachedPages) {
                const revision = archiveRevision;
                const cached = await sendToSink('archive_source', {
                   page: pageNumber});
                if (!Array.isArray(cached.posts)) throw new Error('Invalid cached post batch.');
                return { posts: cached.posts, sourceCount: cached.source_count,
                         sourceOldest: cached.oldest, cachedResumes: cached.resumes, revision };
              }
              await pacingSleep();
              const listJson = await requestJson(
                `${endpoint}?page=${pageNumber - archiveCachedPages}&limit=${pageSize}&comment_limit=0&comment_stream=1`,
                `list page ${pageNumber}`,
              );
              const posts = listJson?.data?.list;
              if (!Array.isArray(posts)) throw new Error(`Invalid post list on page ${pageNumber}`);
              {
                const newest = Number(posts[0]?.timestamp || 0);
                const oldest = Number(posts.at(-1)?.timestamp || 0);
                if (!posts.length || (oldest && oldest < scanStartTimestamp)) {
                  archiveStopPage = Math.min(archiveStopPage, pageNumber);
                } else {
                  const estimatedPages = Math.floor((oldest - scanStartTimestamp) / Math.max(1, newest - oldest));
                  archiveScheduleThrough = Math.max(archiveScheduleThrough,
                    pageNumber + Math.min(requestConcurrency, Math.max(1, estimatedPages)));
                }
              }
              return { pageNumber, posts };
            })()
              .then(
                (result) => pageBuffer.set(pageNumber, result),
                (error) => pageBuffer.set(pageNumber, { pageNumber, error }),
              )
              .finally(() => inFlightPages.delete(pageNumber));
            inFlightPages.set(pageNumber, tracked);
          };

          const fillListRequestSlots = () => {
            while (
              !stopScheduling &&
              inFlightPages.size < getConcurrency() &&
              nextPageToFetch < pageLimit &&
              nextPageToFetch <= archiveScheduleThrough && nextPageToFetch <= archiveStopPage &&
              inFlightPages.size + pageBuffer.size < maxBufferedPages
            ) {
              const pageNumber = nextPageToFetch;
              nextPageToFetch += 1;
              launchListPage(pageNumber);
            }
          };

          const takeListPageInOrder = async (pageNumber) => {
            fillListRequestSlots();
            while (!pageBuffer.has(pageNumber)) {
              if (!inFlightPages.size) {
                throw new Error(`No list request can provide page ${pageNumber}.`);
              }
              await Promise.race([...inFlightPages.values()]);
              fillListRequestSlots();
            }
            const result = pageBuffer.get(pageNumber);
            pageBuffer.delete(pageNumber);
            if (result.error) throw result.error;
            return result;
          };

          return {
            take: takeListPageInOrder,
            stop() { stopScheduling = true; },
            settle: () => Promise.all([...inFlightPages.values()]),
            overfetch: (pageNumber) => Math.max(0, nextPageToFetch - (pageNumber + 1)),
          };
        };
        const listPipeline = createListPipeline({getConcurrency: () => scheduler.concurrency,
          requestJson, pacingSleep, sendToSink});

        // Share post workers across a bounded window of list pages. Duplicate
        // PIDs are serialized so two snapshots cannot race their comment cursor.
        const createPostQueue = ({getConcurrency, signal, onComplete = () => {}}) => {
          const postQueue = [];
          const postWorkByPid = new Map();
          let activePostWorkers = 0;
          const drainPostQueue = () => {
            while (postQueue.length && activePostWorkers < getConcurrency()) {
              const {worker, resolve, reject} = postQueue.shift();
              activePostWorkers += 1;
              Promise.resolve().then(() => {
                signal.throwIfAborted();
                return worker();
              }).then(resolve, reject).finally(() => {
                activePostWorkers -= 1;
                drainPostQueue();
              });
            }
          };
          const schedulePost = (pid, worker) => {
            const previous = postWorkByPid.get(pid) || Promise.resolve();
            const work = previous.then(() => new Promise((resolve, reject) => {
              postQueue.push({worker, resolve, reject});
              drainPostQueue();
            })).then(async result => {
              // Release the worker after text work, but retain the PID barrier
              // and page completion barrier until its image stage finishes.
              if (result?.completion) await result.completion;
            }).finally(() => onComplete(pid));
            postWorkByPid.set(pid, work);
            const forget = () => { if (postWorkByPid.get(pid) === work) postWorkByPid.delete(pid); };
            work.then(forget, forget);
            return work;
          };

          return {schedule: schedulePost, settle: () => Promise.allSettled([...postWorkByPid.values()])};
        };
        const posts = createPostQueue({getConcurrency: () => scheduler.concurrency, signal: controller.signal,
          onComplete: postFinished});
        const schedulePost = posts.schedule;
        // Bound the entire download/upload lifetime, including slow local disks.
        const imageTransfers = createPostQueue({getConcurrency: () => 2, signal: controller.signal});
        const mediaPosts = createPostQueue({getConcurrency: () => requestConcurrency, signal: controller.signal});

        const createPostArchiver = ({requestJson, pacingSleep, sendToSink, schedulePost, getConcurrency}) => {
          let nextMediaPlan = 0;
          const processListPage = async (posts, cachedPage, cachedResumes, cachedRevision) => {
            const pendingRows = [];
            const pendingUnavailableByPid = new Map();
            const rowsByPid = new Map();
            const detailsFetchedPids = new Set();
            const preparedRows = new Map();
            const deferredFavorites = new Set();
            for (const post of posts) {
              const row = {
                pid: String(post.pid),
                timestamp: Number(post.timestamp),
                reply: Number(post.reply),
                favorites: nonNegativeInteger(post.likenum),
                type: post.type || 'text',
                text: post.text || '',
                ...(cachedPage && Number.isInteger(post.observed_at) ? {observed_at: post.observed_at} : {}),
                ...(extractImages && Object.hasOwn(post, 'media_ids') ? {media_ids: mediaIds(post.media_ids)} : {}),
              };
              rowsByPid.set(row.pid, row);
              pendingRows.push(row);
              if (cachedResumes?.[row.pid]) preparedRows.set(row.pid, JSON.stringify(row));
            }

            const fetchAndApplyDetail = async (post, favoritesFallback) => {
              await pacingSleep();
              const detailJson = await requestJson(
                `/chapi/api/v3/hole/one?pid=${encodeURIComponent(post.pid)}&comment_stream=1`,
                `detail #${post.pid}`, false, archive,
              );
              if (detailJson.unavailable) {
                post.unavailable = true;
                if (post.favorites === null) pendingUnavailableByPid.set(post.pid, {pid: post.pid, reason: 'post_not_found'});
                return;
              }
              const hole = detailJson?.data?.hole || {};
              if (extractImages) post.media_ids = mediaIds(hole.media_ids);
              post.text = hole.text || post.text;
              post.type = hole.type || post.type;
              post.reply = Number(hole.reply ?? post.reply);
              post.favorites = nonNegativeInteger(hole.likenum, favoritesFallback);
              delete post.observed_at;
              detailsFetchedPids.add(post.pid);
              if (minFavorites !== null && post.favorites === null) {
                pendingUnavailableByPid.set(post.pid, {
                  pid: post.pid,
                  reason: 'detail_missing',
                });
              }
            };

            const missingFavorites = [...rowsByPid.values()].filter(
              (post) => !cachedPage && minFavorites !== null && post.favorites === null
                && post.timestamp >= reportStartTimestamp && post.timestamp < endTimestamp,
            ).filter(post => {
              if (matchMode === 'all' && minComments !== null && post.reply <= minComments) {
                deferredFavorites.add(post.pid);
                return false;
              }
              return true;
            });
            await mapLimit(missingFavorites, getConcurrency(), async (post) => {
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
            await mapLimit(missingText, getConcurrency(), async (post) => {
              await fetchAndApplyDetail(post, post.favorites);
            });
            pageMatches = pageMatches.filter(matchesThresholds);
            if (archive) {
              const states = new Map();
              const needsPrepare = pageMatches.filter(post => {
                if (preparedRows.get(post.pid) === JSON.stringify(post)) {
                  states.set(post.pid, {resume: cachedResumes[post.pid], revision: cachedRevision});
                  return false;
                }
                return true;
              });
              const revision = archiveRevision;
              const prepared = needsPrepare.length ? await sendToSink('archive_prepare', {posts: needsPrepare}) : {resumes: {}};
              for (const post of needsPrepare) states.set(post.pid, {resume: prepared.resumes?.[post.pid], revision});
              // One resume lookup per post batch, then bounded comment chunks.
              const workOrder = [...pageMatches].sort((a, b) => Math.min(b.reply, 1000) - Math.min(a.reply, 1000));
              await Promise.all(workOrder.map((post) => schedulePost(post.pid, async () => {
                const state = states.get(post.pid);
                const resume = resumeStillValid(post.pid, state.revision) ? state.resume :
                  (await sendToSink('archive_prepare', {posts: [post]})).resumes?.[post.pid];
                if (!resume) throw new Error(`Missing archive resume state for #${post.pid}`);
                if (resume.unavailable) return;
                if (post.unavailable) {
                  await sendToSink('archive_post_unavailable', {post});
                  return;
                }
                if (extractImages && (!resume.post_known || Object.hasOwn(post, 'media_ids'))) {
                  if (!Object.hasOwn(post, 'media_ids')) {
                    await pacingSleep();
                    const detail = await requestJson(`/chapi/api/v3/hole/one?pid=${encodeURIComponent(post.pid)}&comment_stream=1`, `detail image metadata #${post.pid}`, false, true);
                    if (detail.unavailable) {
                      await sendToSink('archive_post_unavailable', {post});
                      return;
                    }
                    if (!detail?.data?.hole) throw new Error(`Missing image metadata for #${post.pid}`);
                    post.media_ids = mediaIds(detail.data.hole.media_ids);
                  }
                  await sendToSink('archive_post_media', {post});
                }
                if (!resume.complete) {
                const seen = new Set();
                const collected = new Set(resume.saved_comment_ids || []);
                const maxComments = 1000;
                let pendingComments = [];
                let batchPages = 0;
                for (let commentPage = resume.next_page || 1; ; commentPage += 1) {
                  const alreadyCapped = collected.size >= maxComments;
                  if (post.reply !== 0 && !alreadyCapped) await pacingSleep();
                  const data = post.reply === 0 || alreadyCapped ? {data: {list: []}} : await requestJson(
                    `/chapi/api/v3/comment/list?pid=${encodeURIComponent(post.pid)}&page=${commentPage}&limit=${commentPageSize}&sort=0&comment_stream=1`,
                    `detail comments #${post.pid} page ${commentPage}`, false, true,
                  );
                  if (data.unavailable) {
                    await sendToSink('archive_post_unavailable', {post});
                    return;
                  }
                  const comments = data?.data?.list;
                  if (!Array.isArray(comments)) throw new Error(`Invalid comment list for #${post.pid}`);
                  const rows = comments.map((comment) => {
                    if (comment.cid === undefined || comment.cid === null || String(comment.cid) === '') {
                      throw new Error(`Missing comment ID for #${post.pid}`);
                    }
                    return {
                      cid: String(comment.cid), text: comment.text || '',
                      timestamp: Number(comment.timestamp || 0),
                      name_tag: String(comment.name_tag || ''),
                      quote_cid: comment.quote?.cid == null ? null : String(comment.quote.cid),
                      ...(extractImages ? {media_ids: mediaIds(comment.media_ids)} : {}),
                    };
                  });
                  if (rows.length && rows.every((row) => seen.has(row.cid))) {
                    throw new Error(`Comment pagination made no progress for #${post.pid}`);
                  }
                  for (const row of rows) seen.add(row.cid);
                  for (const row of rows) {
                    if (!collected.has(row.cid) && collected.size >= maxComments) break;
                    collected.add(row.cid);
                    pendingComments.push(row);
                  }
                  const complete = !comments.length || collected.size >= maxComments;
                  batchPages += 1;
                  if (batchPages >= commentBatchPages || complete) {
                    await sendToSink('archive_comments', { post,

                      comment_page: commentPage, comment_page_size: commentPageSize, comments: pendingComments,
                      complete,
                    });
                    pendingComments = [];
                    batchPages = 0;
                  }
                  if (complete) break;
                }
                }
                if (downloadImages) {
                  const completion = mediaPosts.schedule(post.pid, async () => {
                    while (true) {
                      const planned = await sendToSink('archive_media_plan', {
                        plan_id: String(++nextMediaPlan),  post});
                      if (!planned.images?.length) break;
                      await Promise.all(planned.images.map(item => imageTransfers.schedule(item.media_key, async () => {
                        await pacingSleep();
                        const file = await requestJson(item.url, `image ${item.media_key}`, true);
                        const {blob, ...metadata} = file;
                        await sendToSink('archive_media_file', {
                          post: {pid: post.pid, timestamp: post.timestamp, reply: post.reply, favorites: post.favorites},
                          media_key: item.media_key, ...metadata}, blob);
                      })));
                    }
                  });
                  completion.catch(() => {}); // The PID/page barrier below owns errors.
                  return {completion};
                }
              })));
            }
            return {rows: pendingRows, matches: archive ? [] : pageMatches.map(post => post.pid),
              unavailable: [...pendingUnavailableByPid.values()], deferred: [...deferredFavorites]};
          };

          return {processPage: processListPage};
        };
        const postArchiver = createPostArchiver({requestJson, pacingSleep, sendToSink, schedulePost,
          getConcurrency: () => scheduler.concurrency});

        const processingPages = new Map();
        const pipelineWaiters = [];
        const wakePipeline = () => { while (pipelineWaiters.length) pipelineWaiters.shift()(); };
        const waitForPipeline = () => new Promise(resolve => pipelineWaiters.push(resolve));
        controller.signal.addEventListener('abort', wakePipeline, {once: true});
        const recentFingerprints = [];
        let producerDone = false;
        let pageFailure = false;
        let producerPageNumber = startPage;
        const producer = (async () => {
          for (; producerPageNumber < pageLimit; producerPageNumber += 1) {
            const pageNumber = producerPageNumber;
            while (processingPages.size >= (archive ? requestConcurrency : 1)) {
              controller.signal.throwIfAborted();
              await waitForPipeline();
            }
            controller.signal.throwIfAborted();
            if (pageFailure) break;
            const cachedPage = archive && pageNumber <= archiveCachedPages;
            const {posts, sourceCount, sourceOldest, cachedResumes, revision} = await listPipeline.take(pageNumber);
            controller.signal.throwIfAborted();
            // Exact PID-set repeats/cycles are abnormal; partial overlap is normal
            // on a changing feed. Keep only a bounded recent-page window.
            if (!cachedPage && posts.length) {
              const fingerprint = JSON.stringify([...new Set(posts.map(post => String(post.pid)))].sort());
              if (recentFingerprints.includes(fingerprint)) {
                throw new Error(`List pagination made no progress on page ${pageNumber}`);
              }
              recentFingerprints.push(fingerprint);
              if (recentFingerprints.length > 32) recentFingerprints.shift();
            }
            const oldest = Number(sourceOldest ?? posts.at(-1)?.timestamp ?? 0);
            const exhausted = (sourceCount ?? posts.length) === 0;
            const reached = exhausted || (!cachedPage && oldest > 0 && oldest < scanStartTimestamp)
              || (archiveCacheOnly && pageNumber === archiveCachedPages);
            const terminal = reached || pageNumber + 1 === pageLimit;
            if (terminal) listPipeline.stop();
            const work = postArchiver.processPage(posts, cachedPage, cachedResumes, revision).then(
              result => ({...result, cachedPage, oldest, scanned: sourceCount ?? posts.length,
                reachedStart: reached, feedExhausted: exhausted, terminal}),
              error => { pageFailure = true; listPipeline.stop(); wakePipeline(); return {error}; });
            processingPages.set(pageNumber, work);
            wakePipeline();
            if (terminal) break;
          }
        })().catch(error => {
          // Deliver discovery errors after earlier pages have committed.
          processingPages.set(producerPageNumber, Promise.resolve({error}));
          listPipeline.stop();
        }).finally(() => { producerDone = true; wakePipeline(); });
        settleWork = async () => {
          wakePipeline();
          await producer;
          await Promise.all([...processingPages.values()]);
          await posts.settle();
          await mediaPosts.settle();
          await imageTransfers.settle();
          await listPipeline.settle();
        };

        while (true) {
            const pageNumber = startPage + pages;
            while (!processingPages.has(pageNumber)) {
              controller.signal.throwIfAborted();
              if (producerDone) throw new Error(`No processed page ${pageNumber}.`);
              await waitForPipeline();
            }
            const result = await processingPages.get(pageNumber);
            if (result.error) throw result.error;
            const {cachedPage, oldest, terminal} = result;
            pages += 1;
            scanned += result.scanned;
            chunkScanned += result.scanned;
            pendingRows.push(...result.rows);
            for (const pid of result.matches) pendingMatchedPids.add(pid);
            for (const row of result.unavailable) pendingUnavailableByPid.set(row.pid, row);
            const deferred = new Set(result.deferred);
            for (const row of result.rows) {
              if (deferred.has(row.pid)) pendingDeferredPids.add(row.pid);
              else pendingDeferredPids.delete(row.pid);
            }
            reachedStart = result.reachedStart;
            feedExhausted = result.feedExhausted;
            if (terminal) {
              listPipeline.stop();
              await listPipeline.settle();
              telemetry.add('overfetch_pages', listPipeline.overfetch(pageNumber));
            }
            const checkpoint =
              (pagesBefore + pages) % checkpointPages === 0 || terminal;
            const chunkFull = pages % cacheChunkPages === 0;

            // Cached candidate batches and network rows have different payload shapes.
            // Never let a multi-page cache chunk straddle that boundary.
            const cacheBoundary = cachedPage && pageNumber === archiveCachedPages;
            if (chunkFull || terminal || checkpoint || cacheBoundary) {
              const snapshot = telemetry.snapshot();
              await sendToSink('list_chunk', {
                archive_cached: cachedPage,
                start_page: chunkStartPage,
                end_page: pageNumber,
                pages: pageNumber - chunkStartPage + 1,
                scanned: cachedPage ? 0 : chunkScanned,
                oldest,
                reached_start: reachedStart,
                feed_exhausted: feedExhausted,
                checkpoint,
                terminal,
                rows: cachedPage ? [] : pendingRows,
                matched_pids: [...pendingMatchedPids],
                favorite_unavailable: cachedPage ? [] : [...pendingUnavailableByPid.values()],
                favorite_deferred_pids: cachedPage ? [] : [...pendingDeferredPids],
                telemetry: telemetry.delta(snapshot),
              });
              chunkStartPage = pageNumber + 1;
              chunkScanned = 0;
              pendingRows = [];
              pendingMatchedPids = new Set();
              pendingUnavailableByPid = new Map();
              pendingDeferredPids = new Set();
              telemetry.acknowledge(snapshot);
            }
            processingPages.delete(pageNumber);
            wakePipeline();
            if (terminal) break;
        }

        // Account for requests completed during callbacks and the final data
        // callback itself. This receipt changes no page/checkpoint positions.
        await sendToSink('telemetry_final', {
          telemetry: telemetry.delta(telemetry.snapshot())});

        return null;
        } finally {
          finished = true;
          controller.abort();
          controlController.abort();
          await settleWork();
          if (controlRequest) await controlRequest;
        }
      },
      {
        archive,
        archiveRun,
        archiveCachedPages,
        archiveCacheOnly,
        commentBatchPages,
        commentPageSize,
        extractImages,
        downloadImages,
        controlUrl,
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
