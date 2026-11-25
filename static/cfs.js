// Подбор катушек для CFS: «любой цвет», доп. строки, заполнение из файла слайсера.
(function () {
  var form = document.getElementById("cfs-form"), rows = form.querySelectorAll(".cfs-row");
  // нетронутый серый цвет не отправляем — это «любой цвет»
  form.querySelectorAll("input[type=color]").forEach(function (c) {
    c.addEventListener("input", function () { c.dataset.empty = "0"; });
  });
  function disableEmptyColors() {
    form.querySelectorAll("input[type=color]").forEach(function (c) { if (c.dataset.empty === "1") c.disabled = true; });
  }
  form.addEventListener("submit", disableEmptyColors);
  window.addEventListener("pageshow", function () {
    form.querySelectorAll("input[type=color]").forEach(function (c) { c.disabled = false; });
  });
  var more = document.getElementById("cfs-more");
  function syncMore() { if (more) more.hidden = ![].some.call(rows, function (r) { return r.hidden; }); }
  if (more) more.onclick = function () {
    var next = [].find.call(rows, function (r) { return r.hidden; });
    if (next) next.hidden = false;
    syncMore();
  };
  syncMore();
  form.addEventListener("slice-parsed", function (e) {
    var fil = (e.detail.filaments || []).slice(0, rows.length);
    if (!fil.length) return;
    rows.forEach(function (r, i) {
      var f = fil[i], c = r.querySelector("input[type=color]"), m = r.querySelector("select"), g = r.querySelector("input[name^=g]");
      r.hidden = !f && i >= 4;
      c.value = f && f.color ? f.color : "#888888"; c.dataset.empty = f && f.color ? "0" : "1";
      m.value = ""; if (f && f.type) [].forEach.call(m.options, function (o) { if (f.type.indexOf(o.value) >= 0 && o.value.length > m.value.length) m.value = o.value; });
      g.value = f && f.grams ? String(f.grams).replace(".", ",") : "";
    });
    disableEmptyColors();
    form.submit();
  });
})();
