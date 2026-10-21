// Читает из файла слайсера только нужное (начало/конец G-code или slice_info.config
// из .3mf), отправляет на сервер для разбора и подставляет граммы, время и название.
(function () {
  var CHUNK = 512 * 1024;
  function text(blob) { return blob.text ? blob.text() : new Response(blob).text(); }

  function gcodePart(file) {
    if (file.size <= 2 * CHUNK) return text(file);
    return Promise.all([text(file.slice(0, CHUNK)), text(file.slice(file.size - CHUNK))])
      .then(function (p) { return p[0] + "\n" + p[1]; });
  }

  // минимальное чтение zip: ищем запись в центральном каталоге и распаковываем её
  function zipEntry(file, wanted) {
    var tailSize = Math.min(file.size, 70000);
    return file.slice(file.size - tailSize).arrayBuffer().then(function (buf) {
      var v = new DataView(buf), eocd = -1;
      for (var i = buf.byteLength - 22; i >= 0; i--) if (v.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
      if (eocd < 0) throw new Error("не zip");
      var cdSize = v.getUint32(eocd + 12, true), cdOff = v.getUint32(eocd + 16, true);
      return file.slice(cdOff, cdOff + cdSize).arrayBuffer();
    }).then(function (cd) {
      var v = new DataView(cd), p = 0, dec = new TextDecoder();
      while (p + 46 <= cd.byteLength && v.getUint32(p, true) === 0x02014b50) {
        var method = v.getUint16(p + 10, true), csize = v.getUint32(p + 20, true);
        var nlen = v.getUint16(p + 28, true), elen = v.getUint16(p + 30, true), clen = v.getUint16(p + 32, true);
        var off = v.getUint32(p + 42, true), name = dec.decode(new Uint8Array(cd, p + 46, nlen));
        if (wanted.test(name)) return { method: method, csize: csize, off: off, name: name };
        p += 46 + nlen + elen + clen;
      }
      return null;
    }).then(function (e) {
      if (!e) return null;
      return file.slice(e.off, e.off + 30).arrayBuffer().then(function (h) {
        var v = new DataView(h), start = e.off + 30 + v.getUint16(26, true) + v.getUint16(28, true);
        var data = file.slice(start, start + e.csize);
        if (e.method === 0) return text(data);
        if (e.method !== 8 || !window.DecompressionStream) throw new Error("сжатие не поддерживается");
        return new Response(data.stream().pipeThrough(new DecompressionStream("deflate-raw"))).text();
      });
    });
  }

  function fmt(min) { var h = Math.floor(min / 60), m = min % 60; return h ? h + ":" + (m < 10 ? "0" : "") + m : String(m); }

  document.querySelectorAll("input.slice-file").forEach(function (inp) {
    var form = inp.form, status = inp.parentNode.querySelector(".slice-status");
    inp.addEventListener("change", function () {
      var file = inp.files[0];
      if (!file) return;
      status.textContent = "читаю файл…";
      var is3mf = /\.3mf$/i.test(file.name);
      var job = is3mf ? zipEntry(file, /^Metadata\/slice_info\.config$/) : gcodePart(file);
      job.then(function (t) {
        if (t === null) throw new Error("в .3mf нет данных нарезки — сохраните «нарезанный» проект (gcode.3mf)");
        var fd = new FormData();
        fd.append("csrf", form.querySelector("input[name=csrf]").value);
        fd.append("kind", is3mf ? "3mf" : "gcode");
        fd.append("text", t);
        if (form.material) fd.append("material", form.material.value);
        return fetch(inp.dataset.url, { method: "POST", body: fd, credentials: "same-origin" });
      }).then(function (r) { if (!r.ok) throw new Error("сервер: " + r.status); return r.json(); })
        .then(function (d) {
          if (d.grams && form.grams) form.grams.value = String(d.grams).replace(".", ",");
          inp.dispatchEvent(new CustomEvent("slice-parsed", { detail: d, bubbles: true }));
          if (d.minutes && form.duration) form.duration.value = fmt(d.minutes);
          if (form.caption && !form.caption.value) form.caption.value = file.name.replace(/(\.gcode)?\.(3mf|gcode|gco|bgcode)$/i, "").replace(/[_]+/g, " ");
          var msg = [];
          if (d.grams) msg.push(d.grams + " г"); if (d.minutes) msg.push(fmt(d.minutes));
          if (d.per_filament && d.per_filament.length) msg.push("по катушкам: " + d.per_filament.join(" + ") + " г — граммы всех цветов; поправьте, если печать многоцветная");
          status.textContent = msg.length ? "✓ " + msg.join(", ") : "в файле не нашлось граммов и времени";
        })
        .catch(function (e) { status.textContent = "не удалось прочитать: " + e.message; });
    });
  });
})();
