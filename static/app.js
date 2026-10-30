// Подключается в <head> без defer (снимает no-js до отрисовки); DOM-часть — после DOMContentLoaded.
// Общие обработчики вместо inline-скриптов (CSP: script-src 'self' без 'unsafe-inline').
// Разметка: data-confirm="…" (форма или кнопка), data-autosubmit, data-print, data-select-all,
// img[data-drop-broken], .nav-toggle, .pw-toggle, <div id="spool-left" data-left="…"> на странице катушки.
document.documentElement.classList.remove("no-js");
(function () {
  // подтверждение: своё окно <dialog id="confirm-dialog"> — только для отправки формы и где браузер это умеет;
  // иначе (старый браузер, кнопка без формы, не кнопка отправки) — системный confirm(), как раньше.
  // Текст «Да»: data-confirm-ok, красная кнопка — data-confirm-danger. Отправляем той же кнопкой (submitter),
  // иначе потеряются её name/value (action=delete и т.п.).
  var dlg = null, confirmed = null;
  function dialogReady() {  // окно есть и разметка цела — иначе системный confirm(), чтобы кнопки не «молчали»
    dlg = dlg || document.getElementById("confirm-dialog");
    return !!(dlg && dlg.showModal && HTMLFormElement.prototype.requestSubmit && dlg.querySelector(".dlg-text") &&
              dlg.querySelector("[value=ok]") && dlg.querySelector("[value=cancel]"));
  }
  function ask(src, form, submitter) {
    dlg.querySelector(".dlg-text").textContent = src.dataset.confirm;
    var ok = dlg.querySelector("[value=ok]");
    ok.textContent = src.dataset.confirmOk || "Да";
    ok.className = "btn " + (src.hasAttribute("data-confirm-danger") ? "danger solid" : "primary");
    dlg.onclose = function () {
      if (dlg.returnValue !== "ok") return;
      confirmed = form;  // обработчик submit срабатывает внутри requestSubmit — сразу и снимаем
      try { form.requestSubmit(submitter || undefined); } finally { confirmed = null; }
    };
    dlg.returnValue = "";
    dlg.showModal();
    dlg.querySelector("[value=cancel]").focus();
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.dataset || !form.dataset.confirm || confirmed === form) return;
    if (dialogReady()) { e.preventDefault(); ask(form, form, e.submitter); }
    else if (!confirm(form.dataset.confirm)) e.preventDefault();
  });

  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("button[data-confirm], input[data-confirm]");
    if (el) {
      var submits = el.form && (el.type === "submit" || el.type === "image");
      if (submits && dialogReady()) { e.preventDefault(); e.stopImmediatePropagation(); ask(el, el.form, el); }
      else if (!confirm(el.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); }
      return;
    }
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var src = document.querySelector(copy.dataset.copy);
      if (!src) return;
      var label = copy.dataset.label || (copy.dataset.label = copy.textContent);
      var show = function (text) {
        copy.textContent = text;
        clearTimeout(copy._t);
        copy._t = setTimeout(function () { copy.textContent = label; }, 2500);
      };
      var selectIt = function () { getSelection().selectAllChildren(src); show("Выделено — скопируйте вручную"); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(src.textContent.trim()).then(function () { show("Скопировано ✓"); }, selectIt);
      } else {  // нет буфера обмена (не https) — старый способ, пока жест нажатия ещё действует
        getSelection().selectAllChildren(src);
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
        show(ok ? "Скопировано ✓" : "Выделено — скопируйте вручную");
      }
      return;
    }
    if (e.target.closest("[data-print]")) { window.print(); return; }
    var sel = e.target.closest("[data-select-all]");
    if (sel) { sel.select(); return; }
    var nav = e.target.closest(".nav-toggle");
    if (nav) { setNav(!document.body.classList.contains("nav-open")); return; }
    if (e.target.closest(".sheet-backdrop")) { setNav(false); return; }
    // быстрые значения поля: <button data-set="имя поля" data-value="…">
    var set = e.target.closest("[data-set]");
    if (set && set.form) {
      var f = set.form.elements[set.dataset.set];
      if (f) { f.value = set.dataset.value; f.dispatchEvent(new Event("input", { bubbles: true })); }
      return;
    }
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
        if (box) box.style.display = over ? "flex" : "";  // по умолчанию скрыта в CSS
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
