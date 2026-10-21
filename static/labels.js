// Этикетки: размер ленты, печать всех / одной, PNG для Niimbot/Phomemo через «Поделиться».
(function () {
  var root = document.documentElement, body = document.body;
  var sel = document.getElementById("size"), cw = document.getElementById("cw"), ch = document.getElementById("ch");
  var custom = document.getElementById("custom"), color = document.getElementById("color");
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function apply() {
    var w, h;
    if (sel.value === "custom") { custom.hidden = false; w = +cw.value || 58; h = +ch.value || 40; }
    else { custom.hidden = true; var p = sel.value.split("x"); w = +p[0]; h = +p[1]; }
    root.style.setProperty("--w", w);
    root.style.setProperty("--h", h);
    document.getElementById("page-size").textContent = "@page { size: " + w + "mm " + h + "mm; margin: 0; }";
    body.classList.toggle("small-label", h < 25 || w < 35);
    body.classList.toggle("color-on", color.checked);
    store("label-size", sel.value); store("label-cw", cw.value); store("label-ch", ch.value);
    store("label-color", color.checked ? "1" : "");
  }
  var saved = load("label-size");
  if (saved && [].some.call(sel.options, function (o) { return o.value === saved; })) sel.value = saved;
  if (load("label-cw")) cw.value = load("label-cw");
  if (load("label-ch")) ch.value = load("label-ch");
  color.checked = load("label-color") === "1";
  [sel, cw, ch, color].forEach(function (el) { el.addEventListener("change", apply); el.addEventListener("input", apply); });
  apply();

  document.getElementById("print-all").onclick = function () { body.classList.remove("only"); window.print(); };
  document.querySelectorAll(".one").forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll(".slot.chosen").forEach(function (s) { s.classList.remove("chosen"); });
      b.closest(".slot").classList.add("chosen");
      body.classList.add("only");
      window.print();
    };
  });
  window.addEventListener("afterprint", function () { body.classList.remove("only"); });

  // PNG под ленту → «Поделиться» (на телефоне сразу выбрать приложение Niimbot/Phomemo), иначе скачать
  document.querySelectorAll(".png").forEach(function (b) {
    b.onclick = function () {
      var w = getComputedStyle(root).getPropertyValue("--w").trim(), h = getComputedStyle(root).getPropertyValue("--h").trim();
      var url = b.dataset.url + "?w=" + w + "&h=" + h;
      b.disabled = true;
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.blob(); }).then(function (blob) {
        var file = new File([blob], b.dataset.name, { type: "image/png" });
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          return navigator.share({ files: [file], title: "Этикетка" }).catch(function () {});
        }
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = b.dataset.name; document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      }).finally(function () { b.disabled = false; });
    };
  });
  if (body.dataset.autoprint) window.addEventListener("load", function () { setTimeout(window.print, 300); });
})();
