// 3D-просмотр STL на three.js (r128, лежит в static/vendor — CSP пускает только свои скрипты).
// <div class="stl-view" data-url="…" data-color="#rrggbb"> — модель с сервера;
// <input type="file" class="stl-local" data-target="id"> — файл с устройства, на сервер не уходит.
(function () {
  if (!window.THREE) return;

  function make(container, color) {
    var w = container.clientWidth, h = container.clientHeight;
    var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h);
    container.innerHTML = "";
    container.appendChild(renderer.domElement);

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(40, w / h, 0.1, 100000);
    camera.up.set(0, 0, 1);  // в STL «вверх» — это Z
    scene.add(new THREE.HemisphereLight(0xffffff, 0x555555, 0.9));
    var sun = new THREE.DirectionalLight(0xffffff, 0.7);
    scene.add(sun);
    var controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    var state = { renderer: renderer, scene: scene, camera: camera, controls: controls, sun: sun,
                  color: color || "#4f8cff", mesh: null, grid: null };
    (function loop() {
      if (!container.isConnected) return;
      controls.update();
      sun.position.copy(camera.position);
      renderer.render(scene, camera);
      requestAnimationFrame(loop);
    })();
    new ResizeObserver(function () {
      var w2 = container.clientWidth, h2 = container.clientHeight;
      if (!w2 || !h2) return;
      renderer.setSize(w2, h2);
      camera.aspect = w2 / h2;
      camera.updateProjectionMatrix();
    }).observe(container);
    return state;
  }

  function show(state, buffer, info) {
    var geometry = new THREE.STLLoader().parse(buffer);
    geometry.computeBoundingBox();
    var box = geometry.boundingBox, size = new THREE.Vector3(), center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);
    geometry.translate(-center.x, -center.y, -box.min.z);  // на «стол», по центру
    if (!geometry.attributes.normal) geometry.computeVertexNormals();
    if (state.mesh) state.scene.remove(state.mesh);
    if (state.grid) state.scene.remove(state.grid);
    state.mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial(
      { color: state.color, metalness: 0.1, roughness: 0.6, flatShading: false }));
    state.scene.add(state.mesh);

    var span = Math.max(size.x, size.y, 10);
    var step = span > 200 ? 50 : span > 60 ? 10 : 5;
    var gridSize = Math.ceil(span * 1.4 / step) * step;
    state.grid = new THREE.GridHelper(gridSize, gridSize / step, 0x888888, 0xcccccc);
    state.grid.rotation.x = Math.PI / 2;
    state.scene.add(state.grid);

    var r = Math.max(size.x, size.y, size.z);
    state.camera.position.set(r * 1.4, -r * 1.6, r * 1.2);
    state.controls.target.set(0, 0, size.z / 2);
    state.controls.update();
    state.camera.near = r / 100;
    state.camera.far = r * 100;
    state.camera.updateProjectionMatrix();

    if (info) {
      var tri = geometry.attributes.position.count / 3;
      info.textContent = size.x.toFixed(1) + " × " + size.y.toFixed(1) + " × " + size.z.toFixed(1) + " мм · " +
        Math.round(tri).toLocaleString("ru-RU") + " треуг.";
    }
  }

  function bindTools(container, state) {
    var bar = container.parentNode.querySelector(".stl-tools");
    if (!bar) return;
    bar.addEventListener("click", function (e) {
      var act = e.target.dataset.act;
      if (!act || !state.mesh) return;
      if (act === "wire") state.mesh.material.wireframe = !state.mesh.material.wireframe;
      if (act === "reset") {
        var box = new THREE.Box3().setFromObject(state.mesh), size = new THREE.Vector3();
        box.getSize(size);
        var r = Math.max(size.x, size.y, size.z);
        state.camera.position.set(r * 1.4, -r * 1.6, r * 1.2);
        state.controls.target.set(0, 0, size.z / 2);
      }
      if (act === "top") { state.camera.position.set(0, -0.001, state.camera.position.length()); state.controls.target.set(0, 0, 0); }
    });
  }

  document.querySelectorAll(".stl-view[data-url]").forEach(function (el) {
    var info = el.parentNode.querySelector(".stl-info");
    var state = make(el, el.dataset.color);
    bindTools(el, state);
    if (info) info.textContent = "загрузка…";
    fetch(el.dataset.url, { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
      .then(function (buf) { show(state, buf, info); })
      .catch(function (e) { if (info) info.textContent = "не удалось открыть модель (" + e.message + ")"; });
  });

  document.querySelectorAll("input.stl-local").forEach(function (inp) {
    var el = document.getElementById(inp.dataset.target), state = null;
    var info = el.parentNode.querySelector(".stl-info");
    inp.addEventListener("change", function () {
      var file = inp.files[0];
      if (!file) return;
      if (!state) { state = make(el, "#4f8cff"); bindTools(el, state); }
      info.textContent = "читаю " + file.name + "…";
      file.arrayBuffer().then(function (buf) { show(state, buf, info); info.textContent = file.name + " · " + info.textContent; })
        .catch(function () { info.textContent = "это не STL?"; });
    });
  });
})();
