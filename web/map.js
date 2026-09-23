// Карта и движения камеры сохранены из макета дизайнера. Значения D передаёт движок.
const ratingExpression = [
  "case", ["==", ["get", "modeled"], false], "#87918e",
  [
  "interpolate",
  ["linear"],
  ["get", "rating"],
  0,
  "#d66052",
  50,
  "#d9a441",
  100,
  "#49a273",
  ],
];
function ratingColor(v) {
  if (!Number.isFinite(v)) return "#87918e";
  const a = v < 50 ? [214, 96, 82] : [217, 164, 65],
    b = v < 50 ? [217, 164, 65] : [73, 162, 115],
    t = v < 50 ? v / 50 : (v - 50) / 50;
  return (
    "rgb(" +
    a
      .map((x, i) => Math.round(x + (b[i] - x) * Math.max(0, Math.min(1, t))))
      .join(",") +
    ")"
  );
}
// Тематические слои показывают ровно значения движка, а не уличный прогноз пробок.
function selectedMapMetric() {
  return state.mapMetric && catalog?.indicators?.[state.mapMetric] ? state.mapMetric : "D";
}
function mapMetricName() {
  return selectedMapMetric() === "D" ? "Оценка района, D" : catalog.indicators[selectedMapMetric()].name;
}
function mapMetricValue(id) {
  const row = viewDistrict(id), metric = selectedMapMetric();
  const value = metric === "D" ? districtScore(row) : indicatorValue(row?.indicators?.[metric]);
  return Number.isFinite(value) ? value : null;
}
function geographicName(id) {
  return district(id)?.name || geojson?.features.find(f => f.properties.id === id)?.properties.name || id;
}
function syncMapMetric() {
  const select = $("map-metric-select");
  if (!select || !catalog) return;
  const keys = Object.keys(catalog.indicators);
  if (select.options.length !== keys.length + 1) {
    select.innerHTML = '<option value="D">Оценка района, D</option>' + keys.map(key =>
      `<option value="${esc(key)}">${esc(catalog.indicators[key].name)}</option>`).join("");
  }
  select.value = selectedMapMetric();
  select.onchange = () => {
    state.mapMetric = select.value;
    updateMap();
    if (typeof persistSession === "function") persistSession();
  };
  $("map-metric-title").textContent = mapMetricName();
  $("map-metric-note").textContent = ["T1", "T2", "B2"].includes(selectedMapMetric())
    ? "Районный показатель модели, не текущие пробки на улицах. Чем выше, тем лучше."
    : "Чем выше, тем лучше. Серый район — нет показателей в датасете задания.";
}
function mapFeatures() {
  return {
    ...geojson,
    features: geojson.features
      .map((f) => ({
        ...f,
        id: f.properties.id,
        properties: {
          ...f.properties,
          rating: mapMetricValue(f.properties.id),
          modeled: Number.isFinite(mapMetricValue(f.properties.id)),
        },
      })),
  };
}
function cameraPadding() {
  return innerWidth < 761
    ? { top: 110, right: 45, bottom: innerHeight * 0.47, left: 30 }
    : {
        top: 150,
        right: 110,
        bottom: 175,
        left: innerWidth >= 1600 ? 440 : innerWidth < 1101 ? 390 : 420,
      };
}
function flyOverview(initial = false) {
  if (!mapReady) return;
  map.fitBounds(cityBounds(), {
    padding: cameraPadding(),
    pitch: state.threeD ? 40 : 0,
    bearing: state.threeD ? -15 : 0,
    duration: initial ? 0 : 1400 * motion(),
    maxZoom: 12.5,
  });
}
function featureBounds(feature) {
  const points =
    feature.geometry.type === "MultiPolygon"
      ? feature.geometry.coordinates.flat(2)
      : feature.geometry.coordinates.flat();
  return points.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds());
}
function flyDistrict(id, close = false) {
  if (!mapReady) return;
  const f = geojson.features.find((f) => f.properties.id === id);
  if (!f) return;
  popup?.remove();
  if (close) {
    state.threeD = true;
    $("toggle-3d").classList.add("active");
    $("toggle-3d").setAttribute("aria-pressed", "true");
    map.flyTo({
      center:
        Number.isFinite(f.properties.center_lon) &&
        Number.isFinite(f.properties.center_lat)
          ? [f.properties.center_lon, f.properties.center_lat]
          : featureBounds(f).getCenter(),
      zoom: 15.5,
      pitch: 56,
      bearing: -18,
      padding: cameraPadding(),
      duration: 1800 * motion(),
    });
  } else {
    map.fitBounds(featureBounds(f), {
      padding: cameraPadding(),
      pitch: state.threeD ? 45 : 22,
      bearing: state.threeD ? -12 : 0,
      duration: 1450 * motion(),
      maxZoom: 13.7,
    });
  }
}
function selectDistrict(id) {
  if (!district(id) && !geojson?.features.some(f => f.properties.id === id)) return;
  state.district = id;
  state.tab = "overview";
  render();
  $("panel-scroll").scrollTop = 0;
  flyDistrict(id);
  $("map-hint").classList.add("hidden");
}
function updateMap() {
  syncMapMetric();
  if (!mapReady) return;
  popup?.remove();
  hovered = null;
  const source = map.getSource("districts");
  if (!source) return;
  source.setData(mapFeatures());
  geojson.features.forEach((f) =>
    map.setFeatureState(
      { source: "districts", id: f.properties.id },
      { selected: f.properties.id === state.district, hover: false },
    ),
  );
  markers.forEach(({ el, id }) => {
    el.classList.toggle("selected", id === state.district);
    el.querySelector(".marker-score").textContent = fmt(mapMetricValue(id));
    el.classList.toggle("unmodeled", !district(id));
    const alert = el.querySelector(".marker-alert");
    if (alert) alert.remove();
    if (selectedMapMetric() === "D" && id === weakestId()) {
      const dot = document.createElement("i");
      dot.className = "marker-alert";
      el.appendChild(dot);
    }
    el.setAttribute(
      "aria-label",
      geographicName(id) + ", " + (district(id) ? mapMetricName() + " " + fmt(mapMetricValue(id)) : "нет показателей в учебной модели"),
    );
  });
}
function showMapError(text) {
  if (!mapReady) renderMapFallback(text);
  $("map-status").textContent = text;
  $("map-status").classList.remove("hidden");
}
async function loadMap() {
  if (!geojson?.features?.length) {
    showMapError("Границы районов недоступны. Выберите район на карточке.");
    return;
  }
  if (!window.maplibregl) {
    showMapError(
      "Карта недоступна без интернета. Районы и расчёты работают в панели слева.",
    );
    return;
  }
  try {
    map = new maplibregl.Map({
      container: "map",
      style: "https://tiles.openfreemap.org/styles/liberty",
      center: cityBounds().getCenter(),
      zoom: 10.7,
      pitch: 0,
      bearing: 0,
      maxPitch: 65,
      minZoom: 8,
      maxZoom: 18,
      attributionControl: false,
      renderWorldCopies: false,
      canvasContextAttributes: { antialias: true },
      fadeDuration: 250,
    });
    map.addControl(
      new maplibregl.AttributionControl({ compact: true }),
      "bottom-right",
    );
    map.on("error", (e) => {
      if (!mapReady)
        showMapError(
          "Карта загружается медленно. Можно выбирать районы слева.",
        );
      console.warn("Map:", e.error?.message || "Ошибка ресурса");
    });
    map.on("load", () => {
      try {
        const layers = map.getStyle().layers,
          label = layers.find(
            (l) => l.type === "symbol" && l.layout?.["text-field"],
          )?.id;
        for (const l of layers) {
          if (l.type === "fill-extrusion")
            map.setLayoutProperty(l.id, "visibility", "none");
          if (
            l.type === "symbol" &&
            l.layout?.["text-field"] &&
            /name/.test(JSON.stringify(l.layout["text-field"]))
          ) {
            try {
              map.setLayoutProperty(l.id, "text-field", [
                "coalesce",
                ["get", "name:ru"],
                ["get", "name"],
              ]);
            } catch (e) {
              /* Existing labels remain usable. */
            }
          }
        }
        map.addSource("districts", {
          type: "geojson",
          data: mapFeatures(),
          promoteId: "id",
        });
        map.addLayer(
          {
            id: "district-fill",
            type: "fill",
            source: "districts",
            paint: {
              "fill-color": ratingExpression,
              "fill-opacity": [
                "interpolate",
                ["linear"],
                ["zoom"],
                10,
                [
                  "case",
                  ["boolean", ["feature-state", "selected"], false],
                  0.26,
                  ["boolean", ["feature-state", "hover"], false],
                  0.24,
                  0.14,
                ],
                15,
                0.055,
              ],
              "fill-color-transition": { duration: 750 },
            },
          },
          label,
        );
        map.addLayer(
          {
            id: "district-outline",
            type: "line",
            source: "districts",
            paint: {
              "line-color": ratingExpression,
              "line-width": 1.3,
              "line-opacity": 0.55,
            },
          },
          label,
        );
        map.addLayer(
          {
            id: "district-selected",
            type: "line",
            source: "districts",
            paint: {
              "line-color": "#176b4a",
              "line-width": [
                "case",
                ["boolean", ["feature-state", "selected"], false],
                3,
                0,
              ],
              "line-opacity": 0.85,
            },
          },
          label,
        );
        const sourceId =
          Object.keys(map.getStyle().sources).find(
            (k) => map.getStyle().sources[k].type === "vector",
          ) || "akim-buildings";
        if (!map.getSource(sourceId))
          map.addSource(sourceId, {
            type: "vector",
            url: "https://tiles.openfreemap.org/planet",
          });
        map.addLayer(
          {
            id: "akim-3d",
            type: "fill-extrusion",
            source: sourceId,
            "source-layer": "building",
            minzoom: 14,
            filter: ["!=", ["get", "hide_3d"], true],
            paint: {
              "fill-extrusion-color": [
                "interpolate",
                ["linear"],
                ["to-number", ["get", "render_height"], 4],
                0,
                "#e1e6d9",
                60,
                "#b7c9a2",
                180,
                "#87a489",
              ],
              "fill-extrusion-height": [
                "interpolate",
                ["linear"],
                ["zoom"],
                14,
                0,
                15.4,
                ["to-number", ["get", "render_height"], 4],
              ],
              "fill-extrusion-base": [
                "interpolate",
                ["linear"],
                ["zoom"],
                14,
                0,
                15.4,
                ["to-number", ["get", "render_min_height"], 0],
              ],
              "fill-extrusion-opacity": 0.9,
            },
          },
          label,
        );
        map.setLight({
          anchor: "viewport",
          color: "#fff9e8",
          intensity: 0.38,
          position: [1.3, 190, 45],
        });
        popup = new maplibregl.Popup({
          closeButton: false,
          closeOnClick: false,
          offset: 16,
          maxWidth: "290px",
        });
        geojson.features.forEach((f) => {
          const id = f.properties.id,
            el = document.createElement("button");
          el.className = "district-marker";
          el.innerHTML =
            "<span>" +
            esc(geographicName(id)) +
            '</span><span class="marker-score">' +
            fmt(mapMetricValue(id)) +
            "</span>" +
            (id === baseline.min_district.id
              ? '<i class="marker-alert"></i>'
              : "");
          el.onclick = (e) => {
            e.stopPropagation();
            selectDistrict(id);
          };
          new maplibregl.Marker({ element: el })
            .setLngLat(
              Number.isFinite(f.properties.center_lon) &&
                Number.isFinite(f.properties.center_lat)
                ? [f.properties.center_lon, f.properties.center_lat]
                : featureBounds(f).getCenter(),
            )
            .addTo(map);
          markers.push({ el, id });
        });
        map.on("mousemove", "district-fill", (e) => {
          map.getCanvas().style.cursor = "pointer";
          const f = e.features[0],
            id = f.properties.id;
          if (hovered !== id) {
            if (hovered)
              map.setFeatureState(
                { source: "districts", id: hovered },
                { hover: false },
              );
            hovered = id;
            map.setFeatureState({ source: "districts", id }, { hover: true });
            const d = viewDistrict(id);
            popup.setHTML(!d
              ? '<div class="popup-heading"><b>' + esc(geographicName(id)) + '</b></div><p class="popup-note">Район есть на административной карте. В датасете задания для него нет показателей: Score не рассчитывается.</p>'
              : (
              '<div class="popup-heading"><b>' +
                esc(d.name) +
                "</b><b>" +
                fmt(mapMetricValue(id)) +
                "</b></div>" +
                '<div class="popup-note">' + esc(mapMetricName()) + ' · ' + (state.after ? 'после проектов' : 'до проектов') + '</div>' +
                weakIndicators(d)
                  .map(
                    ([k, i]) =>
                      '<div class="popup-item"><span>' +
                      esc(i.name) +
                      "</span><b>" +
                      fmt(indicatorValue(i)) +
                      "</b></div>",
                  )
                  .join("") +
                '<div class="popup-note">Нажмите, чтобы открыть район</div>'),
            );
          }
          popup.setLngLat(e.lngLat).addTo(map);
        });
        map.on("mouseleave", "district-fill", () => {
          map.getCanvas().style.cursor = "";
          if (hovered)
            map.setFeatureState(
              { source: "districts", id: hovered },
              { hover: false },
            );
          hovered = null;
          popup.remove();
        });
        map.on("click", "district-fill", (e) =>
          selectDistrict(e.features[0].properties.id),
        );
        map.on("dragstart", () => popup.remove());
        mapReady = true;
        $("map-fallback").classList.add("hidden");
        $("map-status").classList.add("hidden");
        updateMap();
        flyOverview(true);
      } catch (e) {
        showMapError(
          "Не удалось настроить слои карты. Выбор районов доступен слева.",
        );
        console.error(e);
      }
    });
    setTimeout(() => {
      if (!mapReady)
        showMapError(
          "Карта ждёт подключения к OpenFreeMap. Районы и проекты доступны слева.",
        );
    }, 18000);
  } catch (e) {
    showMapError(
      "WebGL недоступен. Выбор районов и расчёт остаются в панели слева.",
    );
    console.warn(e);
  }
}

// Обзор строится по переданным геометриям; координаты города не зашиты в интерфейс.
function cityBounds() {
  const bounds = new maplibregl.LngLatBounds();
  for (const feature of geojson.features) bounds.extend(featureBounds(feature));
  return bounds;
}
