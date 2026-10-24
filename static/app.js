// Подключается в <head> без defer (снимает no-js до отрисовки); DOM-часть — после DOMContentLoaded.
// Общие обработчики вместо inline-скриптов (CSP: script-src 'self' без 'unsafe-inline').
// Разметка: data-confirm="…" (форма или кнопка), data-autosubmit, data-print, data-select-all,
// img[data-drop-broken], .nav-toggle, .pw-toggle, <div id="spool-left" data-left="…"> на странице катушки.
document.documentElement.classList.remove("no-js");
(function () {
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("button[data-confirm], input[data-confirm]");
    if (el && !confirm(el.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    if (e.target.closest("[data-print]")) { window.print(); return; }
    var sel = e.target.closest("[data-select-all]");
    if (sel) { sel.select(); return; }
    var nav = e.target.closest(".nav-toggle");
    if (nav) { setNav(!document.body.classList.contains("nav-open")); return; }
    if (e.target.closest(".sheet-backdrop")) { setNav(false); return; }
    var pw = e.target.closest(".pw-toggle");
    if (pw) {
      var i = document.getElementById(pw.dataset.target || "pw"), s = i.type === "password";
      i.type = s ? "text" : "password";
      pw.setAttribute("aria-pressed", s);
      pw.setAttribute("aria-label", s ? "Скрыть пароль" : "Показать пароль");
      pw.textContent = s ? "🙈" : "👁";
      i.focus();
    }
  });

  // лист «Ещё» на телефоне: фокус в лист при открытии, обратно на кнопку при закрытии; Esc закрывает
  function setNav(open) {
    var btn = document.querySelector(".nav-toggle");
    document.body.classList.toggle("nav-open", open);
    if (btn) btn.setAttribute("aria-expanded", open);
    if (open) { var first = document.querySelector("#mainnav .nav-i"); if (first) first.focus({ preventScroll: true }); }
    else if (btn) btn.focus({ preventScroll: true });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.body.classList.contains("nav-open")) setNav(false);
  });
  // «назад» возвращает страницу из bfcache как была — с открытым листом; закрываем без перевода фокуса
  window.addEventListener("pageshow", function () {
    document.body.classList.remove("nav-open");
    var btn = document.querySelector(".nav-toggle");
    if (btn) btn.setAttribute("aria-expanded", false);
  });

  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("[data-autosubmit]")) e.target.form.submit();
  });

  // картинка-превью, которой нет (модель без превью), просто убирается
  document.addEventListener("error", function (e) {
    if (e.target.tagName === "IMG" && e.target.hasAttribute("data-drop-broken")) e.target.remove();
  }, true);

  document.addEventListener("DOMContentLoaded", ready);
  function ready() {
    // страница катушки: списание больше остатка требует подтверждения; со сканера — сразу к весам
    var left = document.getElementById("spool-left");
    if (left) {
      var limit = parseFloat(left.dataset.left);
      document.addEventListener("input", function (e) {
        if (e.target.name !== "grams") return;
        var g = parseFloat(e.target.value.replace(",", ".").replace(/\s/g, ""));
        var over = g > limit + 0.5, box = e.target.form.querySelector(".over-confirm");
        if (box) box.style.display = over ? "" : "none";
        e.target.setCustomValidity("");
        e.target.title = over ? "Больше остатка по учёту (" + Math.round(limit) + " г)" : "";
        e.target.classList.toggle("over", over);
      });
      if (/[?&]scan=1/.test(location.search)) {
        window.addEventListener("load", function () { var g = document.querySelector("#weigh input[name=gross]"); if (g) g.focus(); });
      }
    }

    // подвкладки раздела: активная — в видимой части строки
    var cur = document.querySelector(".subnav [aria-current]");
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ inline: "center", block: "nearest" });

    var sw = document.body && document.body.dataset.sw;
    if (sw && "serviceWorker" in navigator) navigator.serviceWorker.register(sw);
  }
})();
