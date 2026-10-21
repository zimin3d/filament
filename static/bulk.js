// Массовая правка на главной: режим выбора карточек, панель действий, этикетки для отмеченных.
(function () {
  var body = document.body, form = document.getElementById("bulk"), count = document.getElementById("pick-count");
  var picks = function () { return document.querySelectorAll("input.pick"); };
  var update = function () { count.textContent = document.querySelectorAll("input.pick:checked").length; };
  var kinds = JSON.parse(form.dataset.kinds);
  var act = document.getElementById("bulk-action"), val = document.getElementById("bulk-value"),
      wrap = document.getElementById("bulk-value-wrap");
  var syncKind = function () {
    var k = kinds[act.value];
    wrap.style.display = k ? "" : "none";
    val.required = !!k && k !== "text";
    val.type = k === "date" ? "date" : "text";
    val.inputMode = k === "num" ? "decimal" : "text";
  };
  document.getElementById("pick-toggle").onclick = function () { body.classList.toggle("picking"); };
  document.getElementById("pick-cancel").onclick = function () {
    body.classList.remove("picking"); picks().forEach(function (c) { c.checked = false; }); update();
  };
  document.getElementById("pick-all").onclick = function (e) {
    e.preventDefault(); picks().forEach(function (c) { c.checked = true; }); update();
  };
  // в режиме выбора клик по карточке отмечает её, а не открывает
  document.querySelectorAll(".card-wrap").forEach(function (w) {
    w.querySelector("a.card").addEventListener("click", function (e) {
      if (!body.classList.contains("picking")) return;
      e.preventDefault(); var c = w.querySelector("input.pick"); c.checked = !c.checked; update();
    });
  });
  document.addEventListener("change", function (e) { if (e.target.classList.contains("pick")) update(); });
  act.onchange = syncKind; syncKind();
  document.getElementById("pick-labels").onclick = function () {
    var ids = [].map.call(document.querySelectorAll("input.pick:checked"), function (c) { return "id=" + c.value; });
    if (!ids.length) { alert("Отметьте катушки."); return; }
    location.href = form.dataset.labels + "?" + ids.join("&");
  };
  form.addEventListener("submit", function (e) {
    if (!document.querySelectorAll("input.pick:checked").length) { e.preventDefault(); alert("Отметьте катушки."); }
  });
})();
