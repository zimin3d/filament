"""Превью STL картинкой (без зависимостей, кроме Pillow): изометрический вид, плоское затенение,
алгоритм художника. Для больших моделей берётся каждый k-й треугольник — для превью достаточно."""
import math
import re
import struct

from PIL import Image, ImageDraw

MAX_TRIANGLES = 60_000     # больше — берём каждый k-й: для картинки 400 px достаточно
MAX_FACES = 200_000        # предел после дробления крупных треугольников
LIGHT = (0.35, -0.45, 0.82)


def triangles(data):
    """Треугольники ((x,y,z)×3) из бинарного или текстового STL."""
    if len(data) >= 84:
        n = int.from_bytes(data[80:84], "little")
        if len(data) == 84 + 50 * n:
            step = max(1, math.ceil(n / MAX_TRIANGLES))
            for i, t in enumerate(struct.iter_unpack("<12fH", data[84:84 + 50 * n])):
                if i % step == 0:
                    yield (t[3:6], t[6:9], t[9:12])
            return
    nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    step = max(1, math.ceil(len(nums) / 3 / MAX_TRIANGLES))
    for i in range(0, len(nums) - 2, 3):
        if (i // 3) % step == 0:
            yield tuple(tuple(float(v) for v in nums[i + k]) for k in range(3))


def render(data, color="#4f8cff", size=400):
    tris = list(triangles(data))
    if not tris:
        raise ValueError("пустая модель")
    # вид спереди-справа-сверху: поворот вокруг Z на 45°, камера поднята на 35° (верх модели — к зрителю)
    cz, sz = math.cos(math.radians(-45)), math.sin(math.radians(-45))
    cx, sx = math.cos(math.radians(35)), math.sin(math.radians(35))

    def view(p):
        x, y, z = p
        x, y = x * cz - y * sz, x * sz + y * cz
        y, z = y * cx - z * sx, y * sx + z * cx
        return x, y, z

    lx, ly, lz = LIGHT
    ln = math.sqrt(lx * lx + ly * ly + lz * lz)
    lx, ly, lz = lx / ln, ly / ln, lz / ln
    faces = []
    for a, b, c in tris:
        a, b, c = view(a), view(b), view(c)
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        if ny > 0:  # зритель смотрит вдоль +y: видимая грань должна «смотреть» на него (обход в STL бывает любой)
            nx, ny, nz = -nx, -ny, -nz
        shade = max(0.0, (nx * lx + ny * ly + nz * lz) / nl)
        faces.append(((a[1] + b[1] + c[1]) / 3, (a, b, c), shade))
    # крупные треугольники дробим: сортировка «художника» по центру точна только для мелких граней
    xs0 = [p[0] for _, t, _ in faces for p in t]
    zs0 = [p[2] for _, t, _ in faces for p in t]
    limit = max(max(xs0) - min(xs0), max(zs0) - min(zs0)) / 24
    small = []
    stack = faces
    while stack:
        depth, (a, b, c), shade = stack.pop()
        longest = max(math.dist(a, b), math.dist(b, c), math.dist(c, a))
        if longest > limit and len(small) + len(stack) < MAX_FACES:
            ab, bc, ca = (tuple((p + q) / 2 for p, q in zip(u, v)) for u, v in ((a, b), (b, c), (c, a)))
            for t in ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)):
                stack.append(((t[0][1] + t[1][1] + t[2][1]) / 3, t, shade))
        else:
            small.append((depth, (a, b, c), shade))
    faces = small
    xs = [p[0] for _, t, _ in faces for p in t]
    zs = [p[2] for _, t, _ in faces for p in t]
    span = max(max(xs) - min(xs), max(zs) - min(zs)) or 1
    big = size * 2  # рисуем вдвое крупнее и уменьшаем — сглаживание краёв
    scale = big * 0.86 / span
    ox, oz = (max(xs) + min(xs)) / 2, (max(zs) + min(zs)) / 2
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    faces.sort(key=lambda f: f[0], reverse=True)  # дальние (больший y — от зрителя) первыми
    for _, tri, shade in faces:
        k = 0.35 + 0.65 * shade
        pts = [(big / 2 + (p[0] - ox) * scale, big / 2 - (p[2] - oz) * scale) for p in tri]
        fill = (int(r * k), int(g * k), int(b * k), 255)
        draw.polygon(pts, fill=fill, outline=fill)
    return img.resize((size, size), Image.LANCZOS)
