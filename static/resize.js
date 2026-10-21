// Уменьшает фото в браузере перед отправкой (до 2000 px, JPEG), чтобы несколько
// снимков с телефона укладывались в лимит загрузки. Без JS форма работает как обычно.
(function () {
  var MAX = 2000;
  function shrink(file) {
    return new Promise(function (resolve) {
      if (!/^image\/(jpeg|png|webp)$/.test(file.type) || !window.createImageBitmap) return resolve(file);
      createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
        var k = Math.min(1, MAX / Math.max(bmp.width, bmp.height));
        if (k === 1 && file.size < 1.5e6) return resolve(file);
        var c = document.createElement("canvas");
        c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
        c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
        c.toBlob(function (b) {
          resolve(b ? new File([b], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" }) : file);
        }, "image/jpeg", 0.88);
      }).catch(function () { resolve(file); });
    });
  }
  document.querySelectorAll("form.js-resize").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.fetch || !window.FormData) return;
      e.preventDefault();
      var btn = form.querySelector("button.primary");
      if (btn) { btn.disabled = true; btn.textContent = "Загрузка…"; }
      var data = new FormData(form), files = [];
      form.querySelectorAll("input[type=file]").forEach(function (inp) {
        data.delete(inp.name);
        Array.prototype.forEach.call(inp.files, function (f) { files.push([inp.name, f]); });
      });
      Promise.all(files.map(function (p) { return shrink(p[1]).then(function (f) { data.append(p[0], f); }); }))
        .then(function () { return fetch(form.action, { method: "POST", body: data, credentials: "same-origin" }); })
        .then(function (r) {
          if (r.ok && r.redirected) { location.href = r.url; return; }
          alert(r.status === 413 ? "Слишком большие файлы — выберите меньше фото за раз." : "Ошибка загрузки (" + r.status + ").");
          if (btn) { btn.disabled = false; btn.textContent = "Записать"; }
        })
        .catch(function () { form.submit(); });
    });
  });
})();
