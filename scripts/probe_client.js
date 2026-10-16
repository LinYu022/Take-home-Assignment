/* Isolated feasibility instrument; no production UI or local ViewState decoder. */
(() => {
  "use strict";
  const MiB = 1024 * 1024;
  const surface = document.querySelector("#surface"), context = surface.getContext("2d", {alpha: false});
  const status = document.querySelector("#status");
  let serial = 0, epoch = 0, active = 0, queue = [], entries = new Map();
  let pngBytes = 0, decoded = new Set(), current = null, run = null, decodingCount = 0;
  let generation, config, timeline, metrics, canvasToken = null;
  const raf = () => new Promise(resolve => requestAnimationFrame(resolve));
  const post = async (path, body = {}) => {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  };
  function resource() {
    metrics.browser_peak = {
      in_flight: Math.max(metrics.browser_peak.in_flight, active),
      waiting: Math.max(metrics.browser_peak.waiting, queue.length),
      compressed_frames: Math.max(metrics.browser_peak.compressed_frames, [...entries.values()].filter(x => x.blob).length),
      compressed_bytes: Math.max(metrics.browser_peak.compressed_bytes, pngBytes),
      decoded_images: Math.max(metrics.browser_peak.decoded_images, decoded.size),
      decoded_pixels: Math.max(metrics.browser_peak.decoded_pixels, decoded.size * 1024 * 768 * 4)
    };
    if (active > 2 || queue.length > 4 || entries.size > 8 || pngBytes > 12 * MiB || decoded.size > 2 || decoded.size * 1024 * 768 * 4 > 16 * MiB)
      throw new Error("browser hard resource bound exceeded");
  }
  function evict(frame) {
    const entry = entries.get(frame);
    if (!entry) return;
    // A canvas contains copied pixels, so it does not pin compressed input.
    // A pending bitmap decode still owns its source blob until completion.
    if ([...decoded].some(image => image !== canvasToken && image.frame === frame)) return;
    if (entry.blob) { pngBytes -= entry.blob.size; entry.blob = null; }
    if (entry.controller) entry.controller.abort();
    queue = queue.filter(x => x !== entry);
    entries.delete(frame);
  }
  function release(image) {
    if (!image) return;
    if (image.bitmap) image.bitmap.close();
    decoded.delete(image.token);
  }
  async function stop() {
    epoch++;
    if (run) { run.stopped = true; run = null; }
    queue = [];
    if (canvasToken) decoded.delete(canvasToken);
    canvasToken = null; current = null; context.clearRect(0, 0, 1024, 768);
    // Pending bitmap decode owns its reservation until it actually completes.
    while (decodingCount) await new Promise(resolve => setTimeout(resolve, 1));
    for (const token of decoded) if (token.bitmap) token.bitmap.close();
    decoded.clear();
    for (const f of [...entries.keys()]) evict(f);
    // Fetch cancellation is not treated as proof that the server task stopped.
    const response = await post("/generation");
    generation = response.generation;
    // settle aborted fetch finally handlers before reusing counters
    while (active || decodingCount) await new Promise(resolve => setTimeout(resolve, 1));
    return response.stats;
  }
  async function prepare(options) {
    await stop(); config = options;
    if (![1, 2].includes(options.concurrency) || ![4, 6, 8].includes(options.prefetch)) throw new Error("unsupported bounds");
    const start = performance.now();
    const response = await post("/prepare", {concurrency: options.concurrency, level: options.level});
    generation = response.generation;
    timeline = [];
    if (response.length > 10000) throw new Error("probe timeline memory limit (10000 rows)");
    for (let next = 0; next !== null;) {
      const r = await fetch(`/timeline?start=${next}&limit=512`);
      if (!r.ok) throw new Error("timeline request failed");
      const page = await r.json(); timeline.push(...page.rows); next = page.next_cursor;
    }
    metrics = {requests: [], presentations: [], browser_peak: {in_flight: 0, waiting: 0, compressed_frames: 0,
      compressed_bytes: 0, decoded_images: 0, decoded_pixels: 0}, preparation_ms: performance.now() - start};
    return response;
  }
  function pump() {
    while (queue.length && active < config.concurrency) {
      const entry = queue.shift();
      if (entry.epoch !== epoch || !entries.has(entry.frame)) continue;
      active++; resource();
      entry.controller = new AbortController();
      const started = performance.now();
      fetch(`/frame?frame=${entry.frame}&generation=${generation}&request_id=${entry.id}`, {signal: entry.controller.signal})
        .then(async response => {
          const headersAt = performance.now();
          if (!response.ok) throw new Error(`frame ${entry.frame}: HTTP ${response.status}`);
          const blob = await response.blob(), done = performance.now();
          if (entry.epoch !== epoch || entries.get(entry.frame) !== entry) return;
          if (pngBytes + blob.size > 12 * MiB) throw new Error("compressed PNG budget exceeded");
          entry.blob = blob; pngBytes += blob.size;
          entry.manifest = JSON.parse(response.headers.get("X-Render-Manifest"));
          entry.measurement = {request_id: entry.id, frame: entry.frame, ttfb_ms: headersAt - started,
            download_ms: done - headersAt, http_ms: done - started, bytes: blob.size,
            ...JSON.parse(response.headers.get("X-Probe-Timing")), server_resources: JSON.parse(response.headers.get("X-Probe-Resources"))};
          metrics.requests.push(entry.measurement); resource(); entry.resolve(entry);
        })
        .catch(error => {
          if (entry.epoch === epoch && entries.get(entry.frame) === entry) { entry.error = error.message; entry.reject(error); }
        })
        .finally(() => { active--; pump(); });
    }
  }
  function request(frame) {
    if (entries.has(frame)) return entries.get(frame);
    if (entries.size >= config.prefetch || queue.length >= 4 && active >= config.concurrency) return null;
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    promise.catch(() => {});
    const entry = {frame, epoch, id: `r${++serial}`, promise, resolve, reject};
    entries.set(frame, entry); queue.push(entry); pump(); resource(); return entry;
  }
  async function decode(entry, myEpoch) {
    await entry.promise;
    if (epoch !== myEpoch) throw new Error("stale before browser decode");
    if (decoded.size >= 2) throw new Error("decoded image slots exhausted");
    const token = {frame: entry.frame};
    decoded.add(token); decodingCount++; resource();
    const start = performance.now();
    let bitmap;
    try { bitmap = await createImageBitmap(entry.blob); token.bitmap = bitmap; }
    catch (error) { decoded.delete(token); throw error; }
    finally { decodingCount--; }
    if (epoch !== myEpoch) { release({bitmap, token}); throw new Error("stale after browser decode"); }
    if (bitmap.width !== 1024 || bitmap.height !== 768 || entry.manifest.frame !== entry.frame || entry.manifest.view.cursor.frame !== entry.frame)
      throw new Error("frame/PNG/ViewState identity mismatch");
    entry.measurement.browser_decode_ms = performance.now() - start;
    return {bitmap, token, entry};
  }
  function swap(image) {
    // One displayed canvas plus at most one decode/ready ImageBitmap. Drawing
    // consumes no third application-owned decoded image; release the bitmap now.
    context.drawImage(image.bitmap, 0, 0); release(image); image.bitmap = null;
    if (canvasToken) decoded.delete(canvasToken);
    canvasToken = {frame: image.entry.frame}; decoded.add(canvasToken); resource();
    const old = current; current = image;
    status.textContent = `Frame ${image.entry.frame} | timestamp_ns ${image.entry.manifest.timestamp_ns}`;
    return old;
  }
  function confirmed(image, swappedAt) {
    const measurement = image.entry.measurement;
    measurement.present_opportunity_ms = performance.now() - swappedAt;
    metrics.presentations.push({frame: image.entry.frame, pts_ns: image.entry.manifest.pts_ns,
      view_frame: image.entry.manifest.view.cursor.frame, sha256: image.entry.manifest.sha256,
      at_ms: performance.now()});
  }
  async function show(frame) {
    const myEpoch = epoch, started = performance.now();
    const image = await decode(request(frame), myEpoch);
    await raf(); if (epoch !== myEpoch) throw new Error("stale before presentation");
    const old = swap(image), swappedAt = performance.now();
    await raf(); if (epoch !== myEpoch) throw new Error("stale before confirmation");
    confirmed(image, swappedAt); release(old);
    return {frame, elapsed_ms: performance.now() - started, measurement: image.entry.measurement,
      sha256: image.entry.manifest.sha256, pts: image.entry.manifest.pts, manifest_frame: image.entry.manifest.frame};
  }
  function sequence(start, rate) {
    const firstPTS = timeline[start].pts_ns, frames = [start];
    let last = start;
    // Schedule at most 30 different source frames per second of wall time.
    for (let tick = 1; last < timeline.length - 1; tick++) {
      const target = firstPTS + tick * rate / 30 * 1e9;
      let frame = last;
      while (frame + 1 < timeline.length && timeline[frame + 1].pts_ns <= target) frame++;
      if (frame !== last) frames.push(frame);
      last = frame;
    }
    return frames;
  }
  async function play(options) {
    await prepare(options);
    const frames = sequence(options.start || 0, options.rate), myEpoch = epoch;
    let index = 0;
    const fill = () => {
      for (const f of [...entries.keys()]) if (f < frames[index]) evict(f);
      for (let i = index; i < Math.min(frames.length, index + config.prefetch); i++) { if (!request(frames[i])) break; }
    };
    fill(); await show(frames[0]);
    // Initial prebuffer is explicitly outside active playback, but reported.
    const prebufferStart = performance.now();
    for (let i = 0; i < Math.min(config.prefetch, frames.length); i++) { fill(); const e = request(frames[i]); if (e) await e.promise; }
    metrics.prebuffer_ms = performance.now() - prebufferStart;
    let ready = null, decoding = false, fatal = null, pendingConfirmation = null;
    const started = performance.now();
    let anchorWall = started, anchorPTS = timeline[frames[0]].pts_ns, bufferStart = null, bufferMs = 0, longestBuffer = 0;
    let nextIndex = 1;
    run = {stopped: false}; const running = run;
    function nextDecode() {
      if (decoding || ready || nextIndex >= frames.length || decoded.size >= 2) return;
      const entry = request(frames[nextIndex]);
      if (!entry || !entry.blob) return;
      decoding = true;
      decode(entry, myEpoch).then(image => { ready = image; }).catch(error => { fatal = error.message; }).finally(() => { decoding = false; });
    }
    nextDecode();
    await new Promise((resolve, reject) => {
      function tick(now) {
        if (myEpoch !== epoch || running.stopped) { reject(new Error("playback invalidated")); return; }
        if (fatal) { reject(new Error(fatal)); return; }
        if (pendingConfirmation) {
          confirmed(pendingConfirmation.image, pendingConfirmation.at); pendingConfirmation = null;
        }
        if (now - started >= options.seconds * 1000 || nextIndex >= frames.length) {
          if (bufferStart !== null) { const duration = now - bufferStart; bufferMs += duration; longestBuffer = Math.max(longestBuffer, duration); }
          resolve(); return;
        }
        const next = frames[nextIndex], duePTS = timeline[next].pts_ns;
        const targetPTS = anchorPTS + (now - anchorWall) * options.rate * 1e6;
        if (targetPTS >= duePTS || bufferStart !== null) {
          if (ready && ready.entry.frame === next) {
            if (bufferStart !== null) {
              const duration = now - bufferStart; bufferMs += duration; longestBuffer = Math.max(longestBuffer, duration);
              // The due frame is now available: present it and restart time at
              // that new position. Waiting its interval a second time would
              // introduce unmeasured slow motion after every buffer.
              anchorPTS = timeline[ready.entry.frame].pts_ns; anchorWall = now; bufferStart = null;
            }
            pendingConfirmation = {image: ready, old: swap(ready), at: performance.now()}; ready = null;
            index = nextIndex; nextIndex++; fill();
          } else if (bufferStart === null) {
            bufferStart = now;
          }
        }
        nextDecode(); requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
    const ended = performance.now(), wallMs = ended - started;
    const ptsDeltaSeconds = (timeline[current.entry.frame].pts_ns - timeline[frames[0]].pts_ns) / 1e9;
    const presented = metrics.presentations.slice(1);
    const result = {...metrics, configuration: options, elapsed_ms: wallMs, buffer_ms: bufferMs,
      buffer_ratio: bufferMs / wallMs, longest_buffer_ms: longestBuffer, distinct_presentations: presented.length,
      effective_fps: presented.length / (wallMs / 1000), source_seconds: ptsDeltaSeconds,
      active_speed: ptsDeltaSeconds / ((wallMs - bufferMs) / 1000), overall_speed: ptsDeltaSeconds / (wallMs / 1000),
      skipped_source_frames: current.entry.frame - frames[0] - presented.length};
    if (ready) release(ready);
    result.server_final = await stop();
    return result;
  }
  async function seeks(options) {
    await prepare(options);
    const results = [];
    for (const frame of options.frames) {
      if (current) { release(current); current = null; }
      for (const f of [...entries.keys()]) evict(f);
      const shown = await show(frame);
      const digest = await crypto.subtle.digest("SHA-256", await current.entry.blob.arrayBuffer());
      shown.browser_png_sha256 = [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, "0")).join("");
      if (shown.browser_png_sha256 !== shown.sha256) throw new Error("PNG payload SHA-256 mismatch");
      results.push(shown);
    }
    const result = {...metrics, results};
    result.server_final = options.hold ? null : await stop(); return result;
  }
  window.probe = {play, seeks, stop};
})();
