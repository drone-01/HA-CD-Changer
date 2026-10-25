/* Disc Library – phone-first app shell. Plain JS, no build step.
   All API paths are relative so they work behind Home Assistant ingress. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const view = $("#view");
  const sheet = $("#sheet");
  const ICONS = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true" style="fill:currentColor;stroke:none"><path d="M7 4l13 8-13 8z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>',
    prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg>',
    next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg>',
    stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6h12v12H6z"/></svg>',
    heart: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/></svg>',
    shuffle: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h3c4 0 6 10 10 10h3M4 17h3c1.5 0 2.6-1.4 3.6-3.2M14 10.2C15 8.4 16 7 17 7h3"/><path d="M18 4l3 3-3 3M18 14l3 3-3 3"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/></svg>',
    upload: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5M5 20h14"/></svg>',
    down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>',
  };
  const ART_COLORS = ["#5A4E3B", "#4A4A3B", "#3B4A5A", "#5A3B4D", "#3B5A55", "#5A3B3B", "#3F3B5A", "#4D5A3B"];

  const state = {
    q: "", filter: "all", sort: "artist", albums: [], genres: [], changers: [],
    vibe: { mood: "Brooding", energy: "High", time_budget: "about_hour", familiarity: "rediscover" },
    vibePicks: null, statsRange: "year", now: { playing: false },
  };

  // --- helpers -----------------------------------------------------------------
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, opts = {}) {
    const init = { ...opts, headers: { ...(opts.headers || {}) } };
    if (opts.json !== undefined) {
      init.method = init.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    }
    const r = await fetch(`api/${path}`, init);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }

  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
  }

  function art(album, size = "") {
    const id = album.id ?? album.album_id ?? 0;
    const bg = ART_COLORS[id % ART_COLORS.length];
    const inner = album.art_url
      ? `<img src="${esc(album.art_url)}" alt="" loading="lazy">`
      : `<span class="initials" aria-hidden="true">${esc(initials(album.title))}</span>`;
    return `<div class="art ${size}" style="background:${bg}">${inner}</div>`;
  }
  const initials = (t = "") => t.replace(/[^\p{L}\p{N} ]/gu, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const where = (a) => a.location_type === "changer" ? `C${a.changer_id}·${a.slot}` : "";
  const changerName = (id) => (state.changers.find((c) => c.id === id) || {}).name || `Changer ${id}`;

  function describeLocation(a) {
    switch (a.location_type) {
      case "changer": return `${changerName(a.changer_id)} · slot ${a.slot}`;
      case "shelf": return `On the shelf${a.location_note ? ` (${a.location_note})` : ""}`;
      case "player": return `In another player${a.location_note ? ` (${a.location_note})` : ""}`;
      case "loaned": return `Loaned out${a.location_note ? ` (${a.location_note})` : ""}`;
      default: return "Not placed yet";
    }
  }

  // --- routing -----------------------------------------------------------------
  const routes = { library: renderLibrary, vibe: renderVibe, playing: renderPlaying, stats: renderStats };
  function route() {
    const name = (location.hash || "#library").slice(1);
    const fn = routes[name] || renderLibrary;
    document.querySelectorAll(".tabs a").forEach((a) => {
      if (a.dataset.tab === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    fn();
    drawMini();
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);

  // --- Library -----------------------------------------------------------------
  async function loadAlbums() {
    const params = new URLSearchParams({ sort: state.sort });
    if (state.q) params.set("q", state.q);
    if (state.filter === "favorites") params.set("favorites", "true");
    else if (state.filter !== "all") params.set("genre", state.filter);
    state.albums = await api(`albums?${params}`);
  }

  async function renderLibrary() {
    view.innerHTML = `
      <div class="row">
        <h1 class="grow">Library</h1>
        <button type="button" class="icon-btn round" id="random" aria-label="Play a random album">${ICONS.shuffle}</button>
        <a class="btn primary" href="#vibe" style="border-radius:22px">Vibe Sesh</a>
      </div>
      <div class="search">${ICONS.search}
        <label class="sr-only" for="q">Search the library</label>
        <input id="q" type="search" placeholder="Artist, album or track" value="${esc(state.q)}" autocomplete="off">
      </div>
      <div class="chips" role="group" aria-label="Filter" id="chips"></div>
      <div id="enrich"></div>
      <div id="continue"></div>
      <div class="row"><h2 class="grow" id="count">Albums</h2>
        <label class="sr-only" for="sort">Sort</label>
        <select id="sort" class="btn ghost" style="min-height:36px;padding:0 10px;font-size:14px">
          <option value="artist">Artist A–Z</option><option value="title">Title</option>
          <option value="year">Newest</option><option value="added">Recently added</option><option value="played">Last played</option>
        </select>
      </div>
      <div class="grid" id="grid"></div>
      <label class="btn ghost" id="reimport-btn" style="align-self:center;margin-top:8px">${ICONS.upload}Re-import catalog
        <input type="file" id="reimport" accept=".xlsx,.xlsm" class="sr-only"></label>`;
    $("#sort").value = state.sort;
    $("#reimport").addEventListener("change", importCatalog);

    let t;
    $("#q").addEventListener("input", (e) => {
      clearTimeout(t);
      t = setTimeout(async () => { state.q = e.target.value.trim(); await loadAlbums(); drawGrid(); }, 200);
    });
    $("#sort").addEventListener("change", async (e) => { state.sort = e.target.value; await loadAlbums(); drawGrid(); });
    $("#random").addEventListener("click", async () => {
      try { await api("random", { method: "POST" }); await refreshNow(); location.hash = "#playing"; }
      catch (err) { toast(err.message); }
    });

    try {
      [state.genres, state.changers] = await Promise.all([api("genres"), api("changers")]);
      await loadAlbums();
    } catch (err) { toast(err.message); }
    drawChips();
    drawContinue();
    drawGrid();
    drawEnrich();
  }

  // --- metadata enrichment banner ------------------------------------------------
  let enrichTimer;
  async function drawEnrich() {
    clearTimeout(enrichTimer);
    const box = $("#enrich");
    if (!box) return;
    let e;
    try { e = await api("enrich"); } catch { return; }
    const { job, counts } = e;
    const pending = counts.pending || 0;
    const missed = (counts.not_found || 0) + (counts.error || 0);
    if (job.running) {
      const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
      box.innerHTML = `<div class="card" style="display:flex;flex-direction:column;gap:8px">
        <div class="row"><strong class="grow" style="font-size:14px">Fetching covers and album details</strong>
          <span class="quiet" style="font-size:13px">${job.done} of ${job.total}</span></div>
        <div class="bar"><span style="width:${pct}%"></span></div>
        <div class="quiet ellipsis" style="font-size:12px">${esc(job.current || "")}</div></div>`;
      enrichTimer = setTimeout(async () => {
        if (!$("#enrich")) return;
        state.genres = await api("genres").catch(() => state.genres);
        await loadAlbums(); drawGrid(); drawChips(); drawEnrich();
      }, 4000);
      return;
    }
    if (job.finished_at && job.total && !pending) {
      state.genres = await api("genres").catch(() => state.genres);
      drawChips();
    }
    if (pending) {
      box.innerHTML = `<button type="button" class="card link" id="enrich-go">
        <div class="grow"><div style="font-weight:600">Fetch covers and album details</div>
        <div class="quiet" style="font-size:13px">${pending} album${pending === 1 ? "" : "s"} still need art, track lists and genres</div></div>
        <span style="color:var(--accent);font-weight:700">Start</span></button>`;
      $("#enrich-go").onclick = async () => { await api("enrich/start", { method: "POST" }); drawEnrich(); };
    } else if (missed) {
      box.innerHTML = `<div class="quiet" style="font-size:13px;text-align:center">${missed} album${missed === 1 ? "" : "s"} couldn't be matched automatically.
        Open one and use <strong>Fix match</strong>, or <button type="button" class="btn ghost" id="enrich-retry" style="min-height:32px;font-size:13px;padding:0 10px">try again</button></div>`;
      $("#enrich-retry").onclick = async () => { await api("enrich/start?retry=true", { method: "POST" }); drawEnrich(); };
    } else {
      box.innerHTML = "";
    }
  }

  function drawChips() {
    const items = [["all", "All"], ["favorites", "Favorites"], ...state.genres.map((g) => [g.name, g.name])];
    $("#chips").innerHTML = items.map(([v, label]) =>
      `<button type="button" class="chip" data-v="${esc(v)}" aria-pressed="${state.filter === v}">${v === "favorites" ? ICONS.heart.replace("<svg", '<svg style="width:14px;height:14px"') : ""}${esc(label)}</button>`
    ).join("");
    $("#chips").onclick = async (e) => {
      const b = e.target.closest(".chip");
      if (!b) return;
      state.filter = b.dataset.v;
      drawChips();
      await loadAlbums();
      drawGrid();
    };
  }

  async function drawContinue() {
    const box = $("#continue");
    if (!box) return;
    const rows = await api("continue").catch(() => []);
    if (!rows.length) { box.innerHTML = ""; return; }
    const r = rows[0];
    const pct = r.track_count ? Math.round(((r.last_track - 1) / r.track_count) * 100) : 0;
    box.innerHTML = `
      <button type="button" class="card link" id="resume-card">
        ${art({ id: r.album_id, title: r.title }, "md")}
        <div class="grow">
          <h2 style="font-size:11px" class="ellipsis">Pick up where you left off</h2>
          <div style="font-weight:600;margin-top:2px" class="ellipsis">${esc(r.title)}</div>
          <div class="quiet" style="font-size:13px">${esc(r.artist)} · track ${r.last_track}${r.track_count ? ` of ${r.track_count}` : ""}</div>
          ${r.track_count ? `<div class="bar" style="margin-top:6px"><span style="width:${pct}%"></span></div>` : ""}
        </div>
        <span style="color:var(--accent);font-weight:700;font-size:14px">Resume</span>
      </button>`;
    $("#resume-card").onclick = () => resume(r.album_id);
  }

  function drawGrid() {
    const grid = $("#grid");
    if (!grid) return;
    $("#count").textContent = `${state.albums.length} album${state.albums.length === 1 ? "" : "s"}`;
    if (!state.albums.length) {
      if (!state.q && state.filter === "all" && $("#reimport-btn")) $("#reimport-btn").hidden = true;
      grid.outerHTML = state.q || state.filter !== "all"
        ? `<div class="empty" id="grid"><strong>No matches</strong>Try another search or filter.</div>`
        : `<div class="empty" id="grid"><strong>Your library is empty</strong>Import CD_Collection.xlsx to load the catalog.
             <div style="margin-top:16px"><label class="btn primary">${ICONS.upload}Import catalog
             <input type="file" id="import" accept=".xlsx,.xlsm" class="sr-only"></label></div></div>`;
      const inp = $("#import");
      if (inp) inp.addEventListener("change", importCatalog);
      return;
    }
    grid.innerHTML = state.albums.map((a) => `
      <button type="button" class="tile" data-id="${a.id}" aria-label="${esc(a.title)} by ${esc(a.artist)}">
        <div style="position:relative">${art(a)}${where(a) ? `<span class="where" aria-hidden="true">${where(a)}</span>` : ""}</div>
        <span class="t ellipsis">${esc(a.title)}</span>
        <span class="a ellipsis">${esc(a.artist)}</span>
      </button>`).join("");
    grid.onclick = (e) => {
      const tile = e.target.closest(".tile");
      if (tile) openAlbum(Number(tile.dataset.id));
    };
  }

  async function importCatalog(e) {
    const file = e.target.files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      const r = await api("import", { method: "POST", body });
      toast(`Imported ${r.rows} rows: ${r.added} new, ${r.updated} updated${r.added ? ". Fetching covers now." : ""}`);
      renderLibrary();
    } catch (err) { toast(err.message); }
  }

  // --- Album sheet ---------------------------------------------------------------
  async function openAlbum(id) {
    let a;
    try { a = await api(`albums/${id}`); } catch (err) { return toast(err.message); }
    if (!state.changers.length) state.changers = await api("changers").catch(() => []);
    const playable = a.location_type === "changer";
    const yr = a.original_year && a.year && a.original_year !== a.year ? `${a.original_year} (this pressing ${a.year})` : (a.year || a.original_year);
    const meta = [yr, a.label, a.format, a.catalog_no].filter(Boolean).map(esc).join(" · ");
    const mins = a.runtime_sec ? `${Math.round(a.runtime_sec / 60)} min` : "";
    const facts = [a.subgenre || a.genre, mins, a.tracks.length ? `${a.tracks.length} tracks` : ""].filter(Boolean).map(esc).join(" · ");
    const fmtLen = (sec) => sec ? `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}` : "";
    const statusText = {
      matched: `Matched on MusicBrainz (${a.enrich_note || "artist and title"})`,
      manual: "Matched by hand",
      not_found: `Not matched yet: ${a.enrich_note || "no confident match"}`,
      error: `Lookup failed: ${a.enrich_note || ""}`,
      skipped: `Skipped: ${a.enrich_note || ""}`,
      pending: "Waiting for cover and details",
    }[a.enrich_status] || "";
    const mbSearch = `https://musicbrainz.org/search?type=release&method=indexed&query=${encodeURIComponent(`release:"${a.title}" AND artist:"${a.artist}"`)}`;
    sheet.innerHTML = `
      <div class="sheet-body">
        <div class="grabber" aria-hidden="true"></div>
        <div class="row" style="align-items:flex-start;gap:14px">
          ${art(a, "md xl")}
          <div class="grow">
            <h1 id="sheet-title" style="font-size:22px">${esc(a.title)}</h1>
            <div class="quiet">${esc(a.artist)}</div>
            <div class="quiet" style="font-size:13px;margin-top:4px">${meta}</div>
          </div>
          <button type="button" class="icon-btn" id="close" aria-label="Close">${ICONS.down}</button>
        </div>
        ${facts ? `<div style="font-size:14px">${facts}</div>` : ""}
        ${a.edition_notes ? `<div class="quiet" style="font-size:14px">${esc(a.edition_notes)}</div>` : ""}
        <div class="card" style="font-size:14px"><span class="quiet">Location:</span> ${esc(describeLocation(a))}
          ${a.full_listens ? `<span class="quiet"> · ${a.full_listens} full listen${a.full_listens === 1 ? "" : "s"}</span>` : ""}</div>
        <div class="row">
          <button type="button" class="btn primary grow" id="play" ${playable ? "" : "disabled"}>${ICONS.play}Play album</button>
          ${a.resume ? `<button type="button" class="btn grow" id="resume">Resume at track ${a.resume.last_track}</button>` : ""}
          <button type="button" class="icon-btn round" id="fav" aria-pressed="${!!a.favorite}" aria-label="Favorite">
            ${ICONS.heart.replace("<svg", `<svg style="${a.favorite ? "fill:var(--accent);stroke:var(--accent)" : ""}"`)}</button>
        </div>
        ${a.tracks.length ? `<ol class="tracklist">${a.tracks.map((t) => `<li><span class="n">${t.number}</span><span class="grow ellipsis">${esc(t.title || "")}</span><span class="quiet" style="font-size:13px">${fmtLen(t.duration_sec)}</span></li>`).join("")}</ol>` : ""}
        <details class="card" id="fix" ${["not_found", "error"].includes(a.enrich_status) ? "open" : ""}>
          <summary style="cursor:pointer;font-size:14px;min-height:28px">${esc(statusText)}</summary>
          <form id="fix-form" style="display:flex;flex-direction:column;gap:10px;margin-top:10px">
            <div class="quiet" style="font-size:13px">Wrong cover or tracks? Find the right pressing on
              <a href="${mbSearch}" target="_blank" rel="noopener">MusicBrainz</a>, copy its link, and paste it here.</div>
            <div class="field"><label for="fix-mbid">MusicBrainz release link</label>
              <input id="fix-mbid" type="url" inputmode="url" placeholder="https://musicbrainz.org/release/…"></div>
            <div class="form-error" id="fix-err" role="alert"></div>
            <div class="row">
              <button type="button" class="btn ghost grow" id="fix-again">Look up again</button>
              <button type="submit" class="btn grow">Use this release</button>
            </div>
          </form>
        </details>
        <form id="loc" class="card" style="display:flex;flex-direction:column;gap:10px">
          <h2>Where is this disc?</h2>
          <div class="field"><label for="loc-type">Location</label>
            <select id="loc-type">
              <option value="changer">In a changer</option><option value="shelf">On the shelf</option>
              <option value="player">In another player</option><option value="loaned">Loaned out</option>
              <option value="unassigned">Not placed</option>
            </select></div>
          <div class="row" id="loc-changer-fields">
            <div class="field grow"><label for="loc-changer">Changer</label>
              <select id="loc-changer">${state.changers.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.used}/${c.capacity})</option>`).join("")}</select></div>
            <div class="field" style="width:110px"><label for="loc-slot">Slot</label>
              <input id="loc-slot" type="number" inputmode="numeric" min="1" max="400"></div>
          </div>
          <div class="field" id="loc-note-field"><label for="loc-note">Note (optional)</label><input id="loc-note" type="text"></div>
          <div class="form-error" id="loc-err" role="alert"></div>
          <div class="row">
            <button type="button" class="btn ghost grow" id="next-free">Next free slot</button>
            <button type="submit" class="btn grow">Save location</button>
          </div>
        </form>
      </div>`;
    const disc = a.discs[0];
    $("#loc-type").value = a.location_type || "unassigned";
    if (a.changer_id) $("#loc-changer").value = a.changer_id;
    if (a.slot) $("#loc-slot").value = a.slot;
    $("#loc-note").value = a.location_note || "";
    const syncFields = () => {
      const isChanger = $("#loc-type").value === "changer";
      $("#loc-changer-fields").hidden = !isChanger;
      $("#next-free").hidden = !isChanger;
      $("#loc-note-field").hidden = isChanger;
    };
    syncFields();
    $("#loc-type").onchange = syncFields;
    $("#next-free").onclick = async () => {
      try {
        const s = await api(`slots/free?changer_id=${$("#loc-changer").value}`);
        $("#loc-slot").value = s.slot;
      } catch (err) { $("#loc-err").textContent = err.message; }
    };
    $("#loc").onsubmit = async (e) => {
      e.preventDefault();
      const type = $("#loc-type").value;
      const body = { location_type: type };
      if (type === "changer") { body.changer_id = Number($("#loc-changer").value); body.slot = Number($("#loc-slot").value); }
      else body.note = $("#loc-note").value || null;
      try {
        await api(`discs/${disc.id}/location`, { json: body });
        toast("Location saved");
        state.changers = await api("changers").catch(() => state.changers);
        openAlbum(id);
        if ((location.hash || "#library") === "#library") { await loadAlbums(); drawGrid(); }
      } catch (err) { $("#loc-err").textContent = err.message; }
    };
    const refetch = async (mbid) => {
      $("#fix-err").textContent = "Looking it up…";
      try {
        const r = await api(`albums/${a.id}/enrich`, { json: { mbid: mbid || null } });
        if (["error", "not_found"].includes(r.result.status)) { $("#fix-err").textContent = r.result.note; return; }
        toast(r.result.cover ? "Cover and details updated" : "Details updated; no cover art found");
        openAlbum(a.id);
        if ((location.hash || "#library") === "#library") { await loadAlbums(); drawGrid(); }
      } catch (err) { $("#fix-err").textContent = err.message; }
    };
    $("#fix-form").onsubmit = (e) => { e.preventDefault(); refetch($("#fix-mbid").value.trim()); };
    $("#fix-again").onclick = () => refetch(null);
    $("#close").onclick = () => sheet.close();
    $("#play").onclick = () => play(a.id);
    if (a.resume) $("#resume").onclick = () => resume(a.id);
    $("#fav").onclick = async () => {
      await api(`albums/${a.id}/favorite`, { json: { favorite: !a.favorite } });
      openAlbum(a.id);
    };
    if (!sheet.open) sheet.showModal();
  }
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });

  async function play(albumId, fromTrack = 1) {
    try {
      await api("play", { json: { album_id: albumId, from_track: fromTrack } });
      if (sheet.open) sheet.close();
      await refreshNow();
      location.hash = "#playing";
    } catch (err) { toast(err.message); }
  }

  async function resume(albumId) {
    try {
      await api(`albums/${albumId}/resume`, { method: "POST" });
      if (sheet.open) sheet.close();
      await refreshNow();
      location.hash = "#playing";
    } catch (err) { toast(err.message); }
  }

  // --- Vibe Sesh -----------------------------------------------------------------
  const VIBE_Q = [
    ["mood", "Mood", [["Brooding", "Brooding"], ["Upbeat", "Upbeat"], ["Mellow", "Mellow"], ["Nostalgic", "Nostalgic"]]],
    ["energy", "Energy", [["Low", "Low"], ["Medium", "Medium"], ["High", "High"]]],
    ["time_budget", "Time", [["under_45", "Under 45 min"], ["about_hour", "About an hour"], ["all_evening", "All evening"]]],
    ["familiarity", "Familiar or new", [["favorite", "Old favorite"], ["rediscover", "Rediscover"]]],
  ];

  function renderVibe() {
    if (state.vibePicks) return renderPicks();
    view.innerHTML = `
      <div><h1>Vibe Sesh</h1><p class="sub">Four taps, three albums.</p></div>
      ${VIBE_Q.map(([key, label, opts]) => `
        <div role="group" aria-label="${esc(label)}" style="display:flex;flex-direction:column;gap:10px">
          <h2>${esc(label)}</h2>
          <div class="pills">${opts.map(([v, t]) =>
            `<button type="button" class="chip" data-k="${key}" data-v="${v}" aria-pressed="${state.vibe[key] === v}">${esc(t)}</button>`).join("")}</div>
        </div>`).join("")}
      <button type="button" class="btn primary block" id="find" style="min-height:52px;font-size:17px">Find my albums</button>`;
    view.onclick = (e) => {
      const b = e.target.closest(".chip");
      if (!b) return;
      state.vibe[b.dataset.k] = b.dataset.v;
      renderVibe();
    };
    $("#find").onclick = async (e) => {
      e.stopPropagation();
      try {
        state.vibePicks = await api("vibe", { json: { ...state.vibe, seed: Date.now() % 100000 } });
        renderPicks();
      } catch (err) { toast(err.message); }
    };
  }

  function renderPicks() {
    view.onclick = null;
    const labels = VIBE_Q.map(([k, , opts]) => (opts.find(([v]) => v === state.vibe[k]) || [, ""])[1].toLowerCase()).join(" · ");
    const picks = state.vibePicks;
    view.innerHTML = `
      <button type="button" class="btn ghost" id="change" style="align-self:flex-start;border:0;padding:0;color:var(--accent)">‹ Change answers</button>
      <div><h1 style="font-size:28px">Three picks for tonight</h1><p class="sub" style="font-size:14px">${esc(labels)}</p></div>
      ${picks.length ? picks.map((p) => `
        <div class="card" style="display:flex;flex-direction:column;gap:12px">
          <div class="row" style="gap:14px">
            ${art(p, "md")}
            <div class="grow">
              <div style="font-size:17px;font-weight:700">${esc(p.title)}</div>
              <div class="quiet" style="font-size:14px">${esc(p.artist)}${p.year ? ` · ${p.year}` : ""}</div>
              <div style="font-size:14px;margin-top:4px">${esc(p.reason)}</div>
            </div>
          </div>
          <button type="button" class="btn primary" data-play="${p.id}">${ICONS.play}Play album</button>
        </div>`).join("")
      : `<div class="empty"><strong>Nothing to pick from yet</strong>Put some albums in a changer first.</div>`}
      <button type="button" class="btn" id="again">Shuffle picks</button>`;
    $("#change").onclick = () => { state.vibePicks = null; renderVibe(); };
    $("#again").onclick = async () => {
      state.vibePicks = await api("vibe", { json: { ...state.vibe, seed: Date.now() % 100000 } });
      renderPicks();
    };
    view.querySelectorAll("[data-play]").forEach((b) => (b.onclick = () => play(Number(b.dataset.play))));
  }

  // --- Now playing ---------------------------------------------------------------
  async function refreshNow() {
    try { state.now = await api("now-playing"); } catch { state.now = { playing: false }; }
    drawMini();
    if ((location.hash || "#library") === "#playing" && !sheet.open) drawPlaying();
  }

  function drawMini() {
    const n = state.now;
    const mini = $("#mini");
    const onPlaying = (location.hash || "#library") === "#playing";
    mini.hidden = !n.playing || onPlaying;
    if (!n.playing) return;
    $("#mini-art").outerHTML = art(n.album, "sm").replace('class="art sm"', 'class="art sm" id="mini-art"');
    $("#mini-title").textContent = n.album.title;
    $("#mini-sub").textContent = `${n.album.artist} · track ${n.track}${n.track_count ? ` of ${n.track_count}` : ""}`;
    const paused = n.status.state === "paused";
    const btn = $("#mini-toggle");
    btn.innerHTML = paused ? ICONS.play : ICONS.pause;
    btn.setAttribute("aria-label", paused ? "Play" : "Pause");
    btn.onclick = (e) => { e.preventDefault(); transport(paused ? "resume" : "pause"); };
  }

  function renderPlaying() { drawPlaying(); }

  function drawPlaying() {
    const n = state.now;
    if (!n.playing) {
      view.innerHTML = `<h1>Now playing</h1>
        <div class="empty"><strong>Nothing's playing</strong>Pick an album from the Library or try a Vibe Sesh.
        <div style="margin-top:16px" class="row" style="justify-content:center">
          <a class="btn" href="#library">Library</a> <a class="btn primary" href="#vibe">Vibe Sesh</a></div></div>`;
      return;
    }
    const a = n.album;
    const paused = n.status.state === "paused";
    const pct = n.track_count ? Math.round(((n.track - 1) / n.track_count) * 100) : 0;
    view.innerHTML = `
      <div class="row">
        <a href="#library" class="icon-btn" aria-label="Back to library" style="color:var(--ink)">${ICONS.down}</a>
        <div class="grow" style="text-align:center"><h2 style="font-size:12px">${esc(n.changer.name)} · slot ${n.changer.slot}</h2></div>
        <span class="status-chip"><span class="dot"></span>${paused ? "Paused" : "Playing"}</span>
      </div>
      ${art(a, "lg")}
      <div class="row" style="align-items:flex-start">
        <div class="grow">
          <h1 style="font-size:24px">${esc(a.title)}</h1>
          <div class="quiet" style="font-size:16px;margin-top:2px">${esc(a.artist)}${a.year ? ` · ${a.year}` : ""}</div>
          ${n.tracks.length && n.tracks[n.track - 1] ? `<div style="margin-top:8px;color:var(--accent);font-weight:700">${n.track} · ${esc(n.tracks[n.track - 1].title || "")}</div>` : ""}
        </div>
        <button type="button" class="icon-btn" id="np-fav" aria-label="Favorite" aria-pressed="${!!a.favorite}">
          ${ICONS.heart.replace("<svg", `<svg style="${a.favorite ? "fill:var(--accent);stroke:var(--accent)" : ""}"`)}</button>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="bar thick"><span style="width:${pct}%"></span></div>
        <div class="row quiet" style="font-size:12px;justify-content:space-between"><span>Album progress</span>
          <span>Track ${n.track}${n.track_count ? ` of ${n.track_count}` : ""}</span></div>
      </div>
      <div class="row" style="justify-content:center;gap:24px">
        <button type="button" class="icon-btn mid" data-t="previous" aria-label="Previous track">${ICONS.prev}</button>
        <button type="button" class="icon-btn big" data-t="${paused ? "resume" : "pause"}" aria-label="${paused ? "Play" : "Pause"}">${paused ? ICONS.play : ICONS.pause}</button>
        <button type="button" class="icon-btn mid" data-t="next" aria-label="Next track">${ICONS.next}</button>
      </div>
      <div class="row">
        <button type="button" class="btn grow" id="np-info">Album info</button>
        <button type="button" class="btn grow" data-t="stop">${ICONS.stop.replace("<svg", '<svg style="fill:currentColor;stroke:none;width:16px;height:16px"')}Stop</button>
      </div>
      ${n.tracks.length ? `<h2>Tracks</h2><ol class="tracklist">${n.tracks.map((t) =>
        `<li class="${t.number === n.track ? "now" : t.number < n.track ? "done" : ""}"><span class="n">${t.number}</span><span class="grow ellipsis">${esc(t.title || "")}</span></li>`).join("")}</ol>` : ""}`;
    view.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => transport(b.dataset.t)));
    $("#np-info").onclick = () => openAlbum(a.id);
    $("#np-fav").onclick = async () => { await api(`albums/${a.id}/favorite`, { json: { favorite: !a.favorite } }); refreshNow(); };
  }

  async function transport(action) {
    try { state.now = await api(`transport/${action}`, { method: "POST" }); }
    catch (err) { toast(err.message); }
    await refreshNow();
  }

  // --- Stats ---------------------------------------------------------------------
  async function renderStats() {
    view.innerHTML = `
      <h1>Stats</h1>
      <div class="seg" role="group" aria-label="Time range">
        ${[["month", "Month"], ["year", "Year"], ["all", "All time"]].map(([v, t]) =>
          `<button type="button" data-r="${v}" aria-pressed="${state.statsRange === v}">${t}</button>`).join("")}
      </div>
      <div id="stats-body"><div class="empty">Loading…</div></div>`;
    view.querySelectorAll("[data-r]").forEach((b) => (b.onclick = () => { state.statsRange = b.dataset.r; renderStats(); }));
    let s, hist;
    try { [s, hist] = await Promise.all([api(`stats?range=${state.statsRange}`), api("history?limit=20")]); }
    catch (err) { return toast(err.message); }
    const maxG = Math.max(1, ...s.by_genre.map((g) => g.full_listens));
    $("#stats-body").outerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="quiet" style="font-size:13px">Full listens</div><div class="v">${s.full_listens}</div></div>
        <div class="kpi"><div class="quiet" style="font-size:13px">Hours listened</div><div class="v">${s.hours}</div></div>
        <div class="kpi"><div class="quiet" style="font-size:13px">Albums played</div><div class="v">${s.albums_played}<small> of ${s.albums_owned}</small></div></div>
        <div class="kpi"><div class="quiet" style="font-size:13px">Streak</div><div class="v">${s.streak_days}<small> day${s.streak_days === 1 ? "" : "s"}</small></div></div>
      </div>
      <h2>History</h2>
      <div>${hist.length ? hist.map((h) => {
        const partial = !h.completed && h.ended_at && h.end_reason !== "finished";
        const status = !h.ended_at ? "Playing now" : h.completed ? "Full listen"
          : h.end_reason === "interrupted" ? `Interrupted at track ${h.last_track}` : `Stopped at track ${h.last_track}`;
        return `<div class="list-row">
          ${art({ id: h.album_id, title: h.title }, "sm")}
          <div class="grow"><div class="ellipsis" style="font-weight:600">${esc(h.title)}</div>
            <div class="quiet" style="font-size:12px">${esc(h.artist)} · ${esc((h.started_at || "").slice(0, 10))} · ${status}${h.user_name ? ` · ${esc(h.user_name)}` : ""}</div></div>
          ${partial ? `<button type="button" class="btn" style="min-height:36px;border-color:var(--accent);color:var(--accent)" data-resume="${h.album_id}">Resume</button>` : ""}
        </div>`;
      }).join("") : `<div class="empty"><strong>No listens yet</strong>Your history fills in as you play albums.</div>`}</div>
      ${s.by_genre.length ? `<h2>Full listens by genre</h2><div style="display:flex;flex-direction:column;gap:10px">${s.by_genre.map((g) => `
        <div class="genre-row"><span class="ellipsis">${esc(g.genre)}</span><div class="bar"><span style="width:${(g.full_listens / maxG) * 100}%"></span></div><span class="quiet" style="text-align:right">${g.full_listens}</span></div>`).join("")}</div>` : ""}
      ${s.most_played.length ? `<h2>Most played</h2><div>${s.most_played.map((m, i) => `
        <div class="list-row" style="font-size:14px"><span class="quiet" style="width:18px">${i + 1}</span>
          <span class="grow ellipsis">${esc(m.title)} <span class="quiet">· ${esc(m.artist)}</span></span><span class="quiet">${m.plays} play${m.plays === 1 ? "" : "s"}</span></div>`).join("")}</div>` : ""}`;
    view.querySelectorAll("[data-resume]").forEach((b) => (b.onclick = () => resume(Number(b.dataset.resume))));
  }

  // --- start -----------------------------------------------------------------
  route();
  refreshNow();
  setInterval(refreshNow, 3000);
})();
