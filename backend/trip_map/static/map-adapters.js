"use strict";

// Map backends behind one small interface, so app.js doesn't care which one is used:
//   clear()                          remove all pins and lines
//   addPin(lat, lon, html, title, onClick)
//   addLine([[lat, lon], ...], { color, dashed, geodesic }, onClick)   geodesic: curve over the globe (flights), not for road geometry
//   fit(points)                      fit the view to [[lat, lon], ...]
//   focusOn(lat, lon, minZoom)
// create() uses cfg.provider: MapLibre GL vector map (default), Google Maps or Leaflet + OSM;
// any failure falls back to Leaflet.

const MapAdapters = (() => {
  const LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet";
  const MAPLIBRE = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl";

  const loadScript = (src) => new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.async = true; s.onload = resolve; s.onerror = () => reject(new Error("failed to load " + src));
    document.head.append(s);
  });

  const loadCss = (href) => {
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href;
    document.head.append(l);
  };

  // pin markup: the content is only a number and a fixed class, never plan data
  // the visible pin is 34px; the clickable box around it is 44px (touch target)
  const pinHtml = (n, cls) => `<div class="pin-hit"><div class="pin ${cls}" style="width:34px;height:34px"><span>${n}</span></div></div>`;

  // Points along the great circle a->b, longitudes unwrapped so a line over the antimeridian doesn't jump.
  function greatCircle([lat1, lon1], [lat2, lon2], steps = 64) {
    const rad = Math.PI / 180, deg = 180 / Math.PI;
    const v = (lat, lon) => [Math.cos(lat * rad) * Math.cos(lon * rad), Math.cos(lat * rad) * Math.sin(lon * rad), Math.sin(lat * rad)];
    const p = v(lat1, lon1), q = v(lat2, lon2);
    const dot = Math.min(1, Math.max(-1, p[0] * q[0] + p[1] * q[1] + p[2] * q[2]));
    const w = Math.acos(dot);
    if (w < 1e-6) return [[lat1, lon1], [lat2, lon2]];
    const out = [];
    let prevLon = lon1;
    for (let i = 0; i <= steps; i++) {
      const f = i / steps, A = Math.sin((1 - f) * w) / Math.sin(w), B = Math.sin(f * w) / Math.sin(w);
      const x = A * p[0] + B * q[0], y = A * p[1] + B * q[1], z = A * p[2] + B * q[2];
      let lon = Math.atan2(y, x) * deg;
      while (lon - prevLon > 180) lon -= 360;
      while (lon - prevLon < -180) lon += 360;
      prevLon = lon;
      out.push([Math.atan2(z, Math.hypot(x, y)) * deg, lon]);
    }
    return out;
  }

  // A flight with transfers is a chain of legs (SVO -> VVO -> DPS): every leg is its own arc, and the longitudes stay
  // continuous from one leg to the next (no jump across the antimeridian). Two points keep the single-arc behaviour.
  function geodesicPath(points) {
    const out = [];
    for (let i = 0; i + 1 < points.length; i++) {
      const arc = greatCircle(points[i], points[i + 1]);
      if (out.length) {
        const shift = Math.round((out[out.length - 1][1] - arc[0][1]) / 360) * 360;
        if (shift) arc.forEach((pt) => { pt[1] += shift; });
        arc.shift(); // the joint is already in the path
      }
      out.push(...arc);
    }
    return out.length ? out : points;
  }

  // ---- MapLibre GL (vector tiles) ----
  async function createMapLibre(el, cfg) {
    loadCss(MAPLIBRE + ".min.css");
    await loadScript(MAPLIBRE + ".min.js");
    const map = new maplibregl.Map({
      container: el, style: cfg.style_url, center: cfg.center || [45, 55], zoom: cfg.zoom || 3, attributionControl: { compact: true },
    });
    // Wait for the style, not for "load": "load" fires only when every tile of the first view is in, which for a
    // world view takes seconds, and the route would appear only after that. Pins and lines need just the style.
    await new Promise((resolve, reject) => {
      if (map.isStyleLoaded()) return resolve();
      map.once("style.load", resolve);
      map.once("error", (e) => reject(e.error || new Error("map style failed to load")));
    });
    // the container is a flex child that changes size (window, tabs, side panels): keep the canvas in step
    if (window.ResizeObserver) new ResizeObserver(() => map.resize()).observe(el);
    // Label language: prefer name:<lang>, then the Latin transliteration, then the local name.
    // Only symbol layers whose text comes from names are touched (road refs etc. stay as they are).
    const lang = cfg.language.split("-")[0];
    const labelExpr = ["coalesce", ["get", `name:${lang}`], ["get", "name:latin"], ["get", "name"]];
    for (const layer of map.getStyle().layers) {
      const tf = layer.layout && layer.layout["text-field"];
      if (layer.type === "symbol" && tf && JSON.stringify(tf).includes("name")) map.setLayoutProperty(layer.id, "text-field", labelExpr);
    }
    let markers = [], lines = [], seq = 0;
    return {
      name: "maplibre",
      clear() {
        markers.forEach((m) => m.remove()); markers = [];
        lines.forEach((id) => { map.removeLayer(id); map.removeSource(id); }); lines = [];
        if (window.__ekvMapDebug) window.__ekvMapDebug.lines = [];
      },
      addPin(lat, lon, html, title, onClick) {
        const node = document.createElement("div");
        node.innerHTML = html; node.title = title; node.style.cursor = "pointer";
        node.addEventListener("click", onClick);
        markers.push(new maplibregl.Marker({ element: node, anchor: "bottom" }).setLngLat([lon, lat]).addTo(map));
      },
      addLine(points, { color, dashed, geodesic }, onClick) {
        const id = `line-${seq++}`;
        const path = geodesic && points.length >= 2 ? geodesicPath(points) : points;
        // read-only trace of what was drawn (used by the visual test run: window.__ekvMapDebug)
        (window.__ekvMapDebug = window.__ekvMapDebug || { lines: [] }).lines.push({ id, geodesic: !!geodesic, dashed: !!dashed, color, points, drawn: path.length });
        map.addSource(id, { type: "geojson", data: { type: "Feature", geometry: { type: "LineString", coordinates: path.map(([la, lo]) => [lo, la]) } } });
        map.addLayer({
          id, type: "line", source: id, layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": color, "line-width": 4, "line-opacity": 0.95, ...(dashed ? { "line-dasharray": [2, 2] } : {}) },
        });
        map.on("click", id, onClick);
        map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
        lines.push(id);
      },
      fit(points) {
        map.resize(); // container is a flex child; make sure the canvas has its real size before fitting
        if (!points.length) map.jumpTo({ center: [20, 30], zoom: 2 });
        else if (points.length === 1) map.jumpTo({ center: [points[0][1], points[0][0]], zoom: 13 });
        else {
          const b = new maplibregl.LngLatBounds();
          points.forEach(([la, lo]) => b.extend([lo, la]));
          map.fitBounds(b, { padding: 50, maxZoom: 14, duration: 0 });
        }
      },
      focusOn(lat, lon, minZoom) { map.jumpTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), minZoom) }); },
    };
  }

  // ---- Google Maps ----
  async function createGoogle(el, cfg, onAuthFail) {
    // Google reports a rejected key only through this global hook, after the map was created
    window.gm_authFailure = onAuthFail;
    await new Promise((resolve, reject) => {
      window.__gmapsReady = resolve;
      const q = new URLSearchParams({
        key: cfg.google_maps_key, language: cfg.language, v: "weekly",
        loading: "async", libraries: "marker", callback: "__gmapsReady",
      });
      const s = document.createElement("script");
      s.src = "https://maps.googleapis.com/maps/api/js?" + q;
      s.async = true; s.onerror = () => reject(new Error("failed to load Google Maps"));
      document.head.append(s);
    });
    const map = new google.maps.Map(el, {
      center: { lat: 30, lng: 20 }, zoom: 2, mapId: cfg.map_id,
      streetViewControl: false, mapTypeControl: false, fullscreenControl: false,
    });
    let items = [];
    return {
      name: "google",
      clear() {
        items.forEach((i) => (i.setMap ? i.setMap(null) : (i.map = null)));
        items = [];
      },
      addPin(lat, lon, html, title, onClick) {
        const content = document.createElement("div");
        content.innerHTML = html;
        const m = new google.maps.marker.AdvancedMarkerElement({ map, position: { lat, lng: lon }, content, title });
        m.addEventListener("gmp-click", onClick);
        items.push(m);
      },
      addLine(points, { color, dashed, geodesic }, onClick) {
        const line = new google.maps.Polyline({
          map, path: points.map(([lat, lng]) => ({ lat, lng })), geodesic: !!geodesic,
          strokeColor: color, strokeOpacity: dashed ? 0 : 0.8, strokeWeight: 3,
          icons: dashed ? [{ icon: { path: "M 0,-1 0,1", strokeOpacity: 0.9, scale: 3 }, offset: "0", repeat: "14px" }] : [],
        });
        line.addListener("click", onClick);
        items.push(line);
      },
      fit(points) {
        if (!points.length) { map.setCenter({ lat: 30, lng: 20 }); map.setZoom(2); return; }
        if (points.length === 1) { map.setCenter({ lat: points[0][0], lng: points[0][1] }); map.setZoom(13); return; }
        const bounds = new google.maps.LatLngBounds();
        points.forEach(([lat, lon]) => bounds.extend({ lat, lng: lon }));
        map.fitBounds(bounds, 50);
        google.maps.event.addListenerOnce(map, "idle", () => { if (map.getZoom() > 14) map.setZoom(14); });
      },
      focusOn(lat, lon, minZoom) {
        map.setCenter({ lat, lng: lon });
        if ((map.getZoom() || 0) < minZoom) map.setZoom(minZoom);
      },
    };
  }

  // ---- Leaflet + OpenStreetMap (fallback) ----
  // `onBase(state)` is told when the base layer is really there ("ok") or the tiles keep failing ("failed").
  async function createLeaflet(el, onBase) {
    loadCss(LEAFLET + ".min.css");
    await loadScript(LEAFLET + ".min.js");
    const map = L.map(el);
    const layer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "© участники OpenStreetMap",
    }).addTo(map);
    if (onBase) {
      let loaded = 0, failed = 0, told = false;
      const tell = (st) => { if (!told) { told = true; onBase(st); } };
      layer.on("tileload", () => { loaded += 1; tell("ok"); });
      layer.on("tileerror", () => { failed += 1; });
      setTimeout(() => { if (!loaded && failed) tell("failed"); }, 6000);
    }
    let items = [];
    return {
      name: "leaflet",
      clear() { items.forEach((i) => i.remove()); items = []; if (window.__ekvMapDebug) window.__ekvMapDebug.lines = []; },
      addPin(lat, lon, html, title, onClick) {
        const icon = L.divIcon({ className: "", iconSize: [44, 44], iconAnchor: [22, 44], html });
        items.push(L.marker([lat, lon], { icon, title }).addTo(map).on("click", onClick));
      },
      addLine(points, { color, dashed, geodesic }, onClick) {
        // the same arcs as on the vector map: without this a flight is a ruler line across the globe
        const path = geodesic && points.length >= 2 ? geodesicPath(points) : points;
        (window.__ekvMapDebug = window.__ekvMapDebug || { lines: [] }).lines.push({ id: `line-${items.length}`, geodesic: !!geodesic, dashed: !!dashed, color, points, drawn: path.length });
        items.push(L.polyline(path, { color, weight: 4, opacity: 0.95, dashArray: dashed ? "6 8" : null })
          .addTo(map).on("click", onClick));
      },
      fit(points) {
        map.invalidateSize(); // container is a flex child; make sure Leaflet sees its real size before fitting
        if (!points.length) map.setView([30, 20], 2);
        else if (points.length === 1) map.setView(points[0], 13);
        else map.fitBounds(points, { padding: [50, 50], maxZoom: 14 });
      },
      focusOn(lat, lon, minZoom) { map.setView([lat, lon], Math.max(map.getZoom(), minZoom)); },
    };
  }

  // Returns { api, note } where note explains why the fallback is used (or null).
  // onNote(text) may be called later: the fallback reports that its tiles never arrived.
  async function create(el, cfg, { onAuthFail, onNote } = {}) {
    const fallback = async (note) => {
      el.replaceChildren(); // drop whatever a half-initialised map left behind
      const onBase = note && onNote ? (st) => onNote(st === "ok" ? note : "Подложка карты не загрузилась (нет доступа к серверам карт): маршрут показан без неё.") : null;
      return { api: await createLeaflet(el, onBase), note };
    };
    if (cfg.provider === "leaflet") return fallback(null);
    if (cfg.provider === "google") {
      if (!cfg.google_maps_key) return fallback("Ключ Google Maps не задан, показана карта OpenStreetMap (подписи на местном языке).");
      try {
        return { api: await createGoogle(el, cfg, onAuthFail), note: null };
      } catch (e) {
        console.warn(e);
        return fallback("Google Maps не загрузился, показана карта OpenStreetMap (подписи на местном языке).");
      }
    }
    try {
      return { api: await createMapLibre(el, cfg), note: null };
    } catch (e) {
      console.warn(e);
      return fallback("Векторная карта не загрузилась (нужны WebGL и доступ к tiles.openfreemap.org), показана карта OpenStreetMap (подписи на местном языке).");
    }
  }

  // Points to fit the view to: the pins and every drawn line (an arc bulges away from its two ends, and a Pacific flight
  // must not be fitted across the Atlantic). Pins are moved to the copy of the world nearest to the route.
  function routePoints(pins, lines) {
    const pts = [];
    for (const { coords, geodesic } of lines) pts.push(...(geodesic && coords.length >= 2 ? geodesicPath(coords) : coords));
    if (!pts.length) return pins.slice();
    const ref = pts.reduce((a, p) => a + p[1], 0) / pts.length;
    return [...pts, ...pins.map(([la, lo]) => [la, lo + Math.round((ref - lo) / 360) * 360])];
  }

  return { create, pinHtml, geodesicPath, routePoints };
})();
