/* Interactive trips map: pan, zoom, hover labels. Draws the points the server already put on
   the page (data-points / data-lines). Tiles come from this server's own /tiles/ route only. */
(function () {
  "use strict";
  var box = document.querySelector(".evmap");
  if (!box || typeof L === "undefined") { return; }  // no script support or no library: the static map stays
  var points, lines;
  try {
    points = JSON.parse(box.getAttribute("data-points") || "[]");
    lines = JSON.parse(box.getAttribute("data-lines") || "[]");
  } catch (err) { return; }
  var all = [];
  points.forEach(function (p) { all.push([p[0], p[1]]); });
  lines.forEach(function (l) { all.push(l[0]); all.push(l[1]); });
  if (!all.length) { return; }

  box.textContent = "";
  box.style.height = "420px";
  box.style.maxWidth = "640px";
  box.style.border = "1px solid currentColor";
  box.style.borderRadius = "6px";
  var map = L.map(box, { minZoom: 2, maxZoom: 17, attributionControl: true, worldCopyJump: false });
  map.attributionControl.setPrefix(false);
  L.tileLayer("/tiles/{z}/{x}/{y}.png", {
    minZoom: 2, maxZoom: 17,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" ' +
                 'rel="noreferrer noopener">OpenStreetMap contributors</a>'
  }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);

  function label(text) {  // text only, never markup
    var el = document.createElement("span");
    el.textContent = String(text);
    return el;
  }
  lines.forEach(function (l) {
    L.polyline([l[0], l[1]], { color: "#b3261e", weight: 2.5, dashArray: "7 5" }).addTo(map);
  });
  points.forEach(function (p) {
    var kind = p[3];
    var style = { color: "#fff", weight: 2.5, fillColor: "#1b4fd8", fillOpacity: 1, radius: 5 };
    if (kind === "start") { style = { color: "#1b4fd8", weight: 3.5, fillColor: "#fff", fillOpacity: 1, radius: 8 }; }
    if (kind === "end") { style.radius = 8; }
    L.circleMarker([p[0], p[1]], style).bindTooltip(label(p[2])).addTo(map);
  });
  if (all.length === 1) { map.setView(all[0], 15); }
  else { map.fitBounds(L.latLngBounds(all), { padding: [30, 30], maxZoom: 16 }); }
})();
