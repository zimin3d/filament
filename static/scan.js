// Сканер QR с этикеток: камера телефона → переход на катушку с открытой формой взвешивания.
// Встроенный BarcodeDetector (Chrome/Android), иначе jsQR (Safari/iPhone и др.).
(function () {
  var video = document.getElementById("scan-video"), status = document.getElementById("scan-status");
  var base = document.body.dataset.base;  // корень приложения, например /filament/
  var canvas = document.createElement("canvas"), ctx = canvas.getContext("2d", { willReadFrequently: true });
  var detector = null, stream = null, done = false;

  function target(text) {
    // наши ссылки: …/s/12 или …/spool/12 — на этом же сайте
    try {
      var u = new URL(text, location.href);
      if (u.origin !== location.origin) return null;
      var m = u.pathname.match(/\/(?:s|spool)\/(\d+)\/?$/);
      return m ? base + "spool/" + m[1] + "?scan=1#weigh" : null;
    } catch (e) { return null; }
  }

  function found(text) {
    var url = target(text);
    if (!url) { status.textContent = "Это не этикетка катушки: " + text.slice(0, 60); return; }
    done = true;
    status.textContent = "Открываю…";
    if (navigator.vibrate) navigator.vibrate(60);
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    location.href = url;
  }

  function tick() {
    if (done) return;
    if (video.readyState >= 2) {
      if (detector) {
        detector.detect(video).then(function (codes) {
          if (codes.length) found(codes[0].rawValue);
        }).catch(function () {});
      } else if (window.jsQR) {
        var w = video.videoWidth, h = video.videoHeight, k = Math.min(1, 640 / Math.max(w, h));
        canvas.width = Math.round(w * k);
        canvas.height = Math.round(h * k);
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        var img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        var code = jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
        if (code && code.data) found(code.data);
      }
    }
    setTimeout(function () { requestAnimationFrame(tick); }, 120);
  }

  function start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = "Браузер не даёт доступ к камере (нужен HTTPS и современный браузер).";
      return;
    }
    status.textContent = "Разрешите доступ к камере…";
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (s) {
        stream = s;
        video.srcObject = s;
        video.setAttribute("playsinline", "");
        return video.play();
      })
      .then(function () {
        status.textContent = "Наведите камеру на QR-код этикетки";
        var useNative = "BarcodeDetector" in window && BarcodeDetector.getSupportedFormats;
        (useNative ? BarcodeDetector.getSupportedFormats() : Promise.resolve([]))
          .then(function (f) { if (f.indexOf("qr_code") >= 0) detector = new BarcodeDetector({ formats: ["qr_code"] }); })
          .finally(tick);
      })
      .catch(function (e) {
        status.textContent = e && e.name === "NotAllowedError"
          ? "Доступ к камере запрещён — разрешите его в настройках сайта."
          : "Камера недоступна: " + (e && e.message || e);
      });
  }

  document.getElementById("scan-manual").addEventListener("submit", function (e) {
    e.preventDefault();
    var n = this.number.value.trim().replace(/^[№#]/, "");
    if (n) location.href = base + "?q=" + encodeURIComponent("№" + n);
  });
  start();
})();
