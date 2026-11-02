"""Как выглядит талисман Джарвиса. Только рисование, без окон: так его можно проверять и показывать отдельно.

Это мини-реактор: тёмный стеклянный диск, светящийся треугольный сердечник и кольца с бегущими сегментами, то же, что и в большом
окне. Лица нет: у Джарвиса его и не должно быть, настроение передают цвет, яркость и движение колец. Всё тяжёлое (свечение, стекло)
считается один раз, а в каждом кадре только собирается.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

LOGICAL_SIZE = 124  # сторона холста в логических пикселях при масштабе 1
_RADIUS = 27
_CENTER = (62, 66)
PALETTE = {
    "idle": (84, 190, 240),
    "listening": (102, 220, 255),
    "hearing": (160, 240, 255),
    "thinking": (255, 190, 92),
    "speaking": (120, 226, 255),
    "muted": (118, 132, 146),
    "alert": (255, 190, 92),
    "error": (255, 112, 100),
    "happy": (120, 240, 205),
}
_GLASS_TOP = (20, 40, 60)
_GLASS_BOTTOM = (4, 10, 18)
_CORE_HOT = (236, 250, 255)
_SEGMENTS = 12


@dataclass
class Pose:
    """Всё, что меняется от кадра к кадру. Значения считает Mascot, здесь только рисунок."""

    color: tuple[float, float, float] = PALETTE["idle"]
    glow: float = 0.5  # 0..1
    bob: float = 0.0  # смещение вверх-вниз в логических пикселях
    stretch: float = 0.0  # >0 вытянут вверх, <0 приплюснут
    lean: float = 0.0  # наклон колец, в логических пикселях
    gaze: tuple[float, float] = (0.0, 0.0)  # -1..1: сердечник чуть смещается в сторону курсора
    eye_open: float = 1.0  # 0 моргание, 1 обычная яркость сердечника, >1 ярче
    eye_shape: str = "oval"  # oval, happy, sad, closed, focus: режим сердечника
    mouth: float = 0.0  # 0 тишина, 1 голос на полную: сердечник вспыхивает в такт речи
    smile: float = 0.35  # не используется в рисунке, оставлено характеру
    ring_angle: float = 0.0  # радианы
    ring_bright: float = 0.6
    antenna: float = 0.0  # раскачка наружного кольца, -1..1
    tip: float = 0.6  # яркость меток на кольце
    orbit: float = -1.0  # точки «обрабатываю»: фаза 0..1, -1 не показывать
    waves: float = -1.0  # волны «слышу»: фаза 0..1
    sleep_z: float = -1.0  # режим дремоты: фаза 0..1
    sparkle: float = -1.0  # вспышка успеха: фаза 0..1
    muted_mark: bool = False


def _smoothstep(edge0: float, edge1: float, values: np.ndarray) -> np.ndarray:
    t = np.clip((values - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


class MascotArt:
    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale
        self.size = max(32, round(LOGICAL_SIZE * scale))
        self._ss = 2
        self._k = self.size / LOGICAL_SIZE
        self._glow_mask = self._make_glow_mask()
        self._glass, self._rim_mask = self._make_glass()
        self._glow_cache: dict[tuple[int, int, int, int], Image.Image] = {}
        self._rim_cache: dict[tuple[int, int, int], Image.Image] = {}

    # ---------- Заготовки ----------

    def _make_glow_mask(self) -> Image.Image:
        size = self.size
        y, x = np.mgrid[0:size, 0:size].astype(np.float32)
        cx, cy = _CENTER[0] * self._k, _CENTER[1] * self._k
        distance = np.hypot(x - cx, y - cy) / (self.size * 0.46)
        # Плавно к нулю у края холста: вокруг талисмана нет прямоугольника и лишней зоны для кликов.
        mask = np.clip(1.0 - distance, 0.0, 1.0) ** 2.6
        return Image.fromarray((mask * 255).astype(np.uint8), "L")

    def _make_glass(self) -> tuple[Image.Image, Image.Image]:
        """Стеклянный диск вдвое крупнее, чем нужно: при лёгком сжатии и растяжении он уменьшается без лесенки."""
        side = round(_RADIUS * 2.0 * self._k * 2.0)
        y, x = np.mgrid[0:side, 0:side].astype(np.float32)
        center = (side - 1) / 2
        # Запас 8 %: иначе низ диска упирается в край заготовки и выглядит срезанным.
        u, v = (x - center) / (side / 2) * 1.08, (y - center) / (side / 2) * 1.08
        distance = np.hypot(u, v)
        alpha = 1.0 - _smoothstep(0.955, 1.0, distance)
        t = np.clip((v + 1.0) / 2.0, 0.0, 1.0)[..., None]
        top, bottom = np.array(_GLASS_TOP, np.float32), np.array(_GLASS_BOTTOM, np.float32)
        rgb = top * (1 - t) + bottom * t
        # Узкий блик сверху и отражение снизу: так диск читается как стекло прибора, а не как плоский круг.
        highlight = np.exp(-((u / 0.62) ** 2 + ((v + 0.74) / 0.11) ** 2))[..., None]
        bounce = np.exp(-((u / 0.62) ** 2 + ((v - 0.86) / 0.14) ** 2))[..., None]
        rgb = rgb + highlight * 46 + bounce * 12
        glass = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
        rim = (_smoothstep(0.8, 0.985, distance) * alpha) ** 1.7
        return Image.fromarray(glass, "RGBA"), Image.fromarray((rim * 235).astype(np.uint8), "L")

    def _glow_image(self, color: tuple[int, int, int], level: int) -> Image.Image:
        key = (*color, level)
        cached = self._glow_cache.get(key)
        if cached is not None:
            return cached
        strength = 0.18 + 0.52 * (level / 12)
        mask = self._glow_mask.point(lambda value: int(value * strength))
        image = Image.new("RGBA", (self.size, self.size), (*color, 0))
        image.putalpha(mask)
        if len(self._glow_cache) > 90:
            self._glow_cache.clear()
        self._glow_cache[key] = image
        return image

    def _rim_image(self, color: tuple[int, int, int]) -> Image.Image:
        cached = self._rim_cache.get(color)
        if cached is not None:
            return cached
        image = Image.new("RGBA", self._glass.size, (*color, 0))
        image.putalpha(self._rim_mask)
        if len(self._rim_cache) > 40:
            self._rim_cache.clear()
        self._rim_cache[color] = image
        return image

    # ---------- Кадр ----------

    def _p(self, x: float, y: float) -> tuple[float, float]:
        """Логические координаты в координаты рисунка с запасом для сглаживания."""
        k = self._k * self._ss
        return x * k, y * k

    def _w(self, width: float) -> int:
        return max(1, round(width * self._k * self._ss))

    def render(self, pose: Pose) -> Image.Image:
        size = self.size
        red, green, blue = pose.color
        color = (int(red) // 4 * 4, int(green) // 4 * 4, int(blue) // 4 * 4)
        frame = self._glow_image(color, max(0, min(12, round(pose.glow * 12)))).copy()
        cx, cy = _CENTER[0] + pose.lean * 0.4, _CENTER[1] + pose.bob
        self._body(frame, pose, color, cx, cy)
        self._layer(frame, lambda draw: self._front(draw, pose, color, cx, cy))
        assert frame.size == (size, size)
        return frame

    def _layer(self, frame: Image.Image, paint) -> None:  # noqa: ANN001
        layer = Image.new("RGBA", (self.size * self._ss, self.size * self._ss), (0, 0, 0, 0))
        paint(ImageDraw.Draw(layer, "RGBA"))
        frame.alpha_composite(layer.reduce(self._ss))

    def _body(self, frame: Image.Image, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        width = round(_RADIUS * 2.16 * (1 - pose.stretch * 0.5) * self._k)
        height = round(_RADIUS * 2.16 * (1 + pose.stretch * 0.5) * self._k)
        if width < 2 or height < 2:
            return
        glass = self._glass.copy()
        glass.alpha_composite(self._rim_image(color))
        body = glass.resize((width, height), Image.Resampling.BILINEAR)
        frame.alpha_composite(body, (round(cx * self._k - width / 2), round(cy * self._k - height / 2)))

    def _front(self, draw: ImageDraw.ImageDraw, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        self._outer_ring(draw, pose, color, cx, cy)
        self._coils(draw, pose, color, cx, cy)
        self._core(draw, pose, color, cx, cy)
        if pose.orbit >= 0:
            self._orbit_dots(draw, pose, color, cx, cy)
        if pose.waves >= 0:
            self._sound_waves(draw, pose, color, cx, cy)
        if pose.sparkle >= 0:
            self._flash(draw, pose, cx, cy)
        if pose.muted_mark:
            self._mic_off(draw, cx, cy)

    def _arc(self, draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, start: float, sweep: float,
             fill: tuple[int, ...], width: float) -> None:
        x, y = self._p(cx, cy)
        r = radius * self._k * self._ss
        begin = math.degrees(start)
        draw.arc((x - r, y - r, x + r, y + r), begin, begin + math.degrees(sweep), fill=fill, width=self._w(width))

    def _outer_ring(self, draw: ImageDraw.ImageDraw, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        """Наружное кольцо за краем диска: тонкая шкала и три бегущие дуги, как у большого реактора."""
        wobble = pose.antenna * 1.4
        radius = _RADIUS + 8.5 + wobble
        self._arc(draw, cx, cy, radius, 0.0, math.tau, (*color, 54), 0.8)
        for arc in range(3):
            start = pose.ring_angle + arc * math.tau / 3
            pieces = 8
            for piece in range(pieces):
                fade = (piece + 1) / pieces
                alpha = int(255 * pose.ring_bright * fade)
                self._arc(draw, cx, cy, radius, start + piece / pieces * 1.05, 1.05 / pieces + 0.01, (*color, alpha), 1.9)
        # Встречная дуга ближе к диску: кольца расходятся, и реактор выглядит живым даже в покое.
        for arc in range(2):
            start = -pose.ring_angle * 0.7 + arc * math.pi
            self._arc(draw, cx, cy, _RADIUS + 3.6, start, 0.9, (*color, int(120 + 90 * pose.ring_bright)), 1.0)

    def _coils(self, draw: ImageDraw.ImageDraw, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        """Двенадцать катушек дугового реактора: их подсветка бежит по кругу, а голос зажигает все разом."""
        outer, inner = _RADIUS - 3.2, _RADIUS - 9.6
        lit = 0.25 + 0.75 * min(1.0, pose.mouth * 1.4)
        step = math.tau / _SEGMENTS
        for index in range(_SEGMENTS):
            angle = index * step + pose.ring_angle * 0.25
            chase = 0.5 + 0.5 * math.cos(angle - pose.ring_angle * 1.5)
            alpha = int(255 * min(1.0, (0.22 + 0.5 * chase * chase) * pose.tip + lit * 0.5))
            half = step * 0.34
            corners = [(math.cos(angle - half) * outer, math.sin(angle - half) * outer),
                       (math.cos(angle + half) * outer, math.sin(angle + half) * outer),
                       (math.cos(angle + half * 0.8) * inner, math.sin(angle + half * 0.8) * inner),
                       (math.cos(angle - half * 0.8) * inner, math.sin(angle - half * 0.8) * inner)]
            draw.polygon([self._p(cx + x, cy + y) for x, y in corners], fill=(*color, alpha))

    def _core(self, draw: ImageDraw.ImageDraw, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        """Сердечник: треугольник и горячий центр. Форма повторяет большой реактор, яркость идёт за голосом и настроением."""
        gx, gy = pose.gaze
        x, y = cx + gx * 1.5, cy + gy * 1.2
        # Радость и успех: сердечник чуть расправляется. Без лица это единственный способ показать настроение формой.
        size = 8.0 + 2.6 * pose.mouth + (1.6 if pose.eye_shape == "happy" else 0.0)
        # Моргание здесь короткое подрагивание света, а не тьма: реактор не гаснет.
        brightness = (0.6 + 0.4 * min(1.25, pose.eye_open)) * (0.6 if pose.eye_shape == "closed" else 1.0)
        spin = -pose.ring_angle * 0.12 - math.pi / 2
        points = [(x + math.cos(spin + i * math.tau / 3) * size, y + math.sin(spin + i * math.tau / 3) * size) for i in range(3)]
        outline = [self._p(*point) for point in points]
        draw.polygon(outline, fill=(*color, int(60 * brightness)))
        draw.line([*outline, outline[0]], fill=(*self._hot(color), int(min(255, 220 * brightness))), width=self._w(1.2), joint="curve")
        radius = 2.6 + 3.6 * pose.mouth
        self._dot(draw, (x, y), radius + 2.8, (*color, int(min(255, 90 * brightness))))
        self._dot(draw, (x, y), radius, (*_CORE_HOT, int(min(255, 245 * brightness))))
        if pose.eye_shape == "sad":
            # Сбой: разрыв на кольце сердечника. Без лица это читается как предупреждение прибора.
            self._arc(draw, x, y, size + 2.4, math.radians(200), math.radians(140), (255, 112, 100, 235), 1.3)

    @staticmethod
    def _hot(color: tuple[int, int, int]) -> tuple[int, int, int]:
        return tuple(min(255, int(c * 0.35 + 255 * 0.65)) for c in color)  # type: ignore[return-value]

    def _dot(self, draw: ImageDraw.ImageDraw, center: tuple[float, float], radius: float, fill: tuple[int, ...]) -> None:
        x, y = self._p(*center)
        r = radius * self._k * self._ss
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def _orbit_dots(self, draw: ImageDraw.ImageDraw, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        for index in range(3):
            phase = (pose.orbit + index / 3) * math.tau
            x = cx + math.cos(phase) * (_RADIUS + 14)
            y = cy + math.sin(phase) * (_RADIUS + 14)
            depth = 0.55 + 0.45 * math.sin(phase)
            self._dot(draw, (x, y), 1.3 + depth * 0.9, (*color, int(130 + 125 * depth)))

    def _sound_waves(self, draw: ImageDraw.ImageDraw, pose: Pose, color: tuple[int, int, int], cx: float, cy: float) -> None:
        for index in range(2):
            phase = (pose.waves + index / 2) % 1.0
            radius = _RADIUS + 10 + phase * 13
            alpha = int(190 * (1 - phase))
            for side in (-1, 1):
                start = math.radians(-32 if side == 1 else 148)
                self._arc(draw, cx, cy, radius, start, math.radians(64), (*color, alpha), 1.4)

    def _flash(self, draw: ImageDraw.ImageDraw, pose: Pose, cx: float, cy: float) -> None:
        """Успех: от диска расходится кольцо. Конфетти здесь было бы не к лицу."""
        phase = pose.sparkle
        self._arc(draw, cx, cy, _RADIUS + 4 + phase * 19, 0.0, math.tau, (*_CORE_HOT, int(210 * (1 - phase))), 1.2)

    def _mic_off(self, draw: ImageDraw.ImageDraw, cx: float, cy: float) -> None:
        """Перечёркнутый микрофон: сразу понятно, почему Джарвис не отзывается."""
        x, y = cx + 25, cy - 23
        self._dot(draw, (x, y), 8.2, (14, 22, 32, 245))
        px, py = self._p(x, y - 1.2)
        rx, ry = 2.2 * self._k * self._ss, 3.4 * self._k * self._ss
        draw.rounded_rectangle((px - rx, py - ry, px + rx, py + ry), radius=rx, fill=(255, 150, 140, 255))
        draw.line([self._p(x - 4.6, y + 4.6), self._p(x + 4.6, y - 4.6)], fill=(255, 112, 100, 255), width=self._w(1.4))


def premultiplied_bgra(frame: Image.Image) -> bytes:
    """Формат, которого требует UpdateLayeredWindow: BGRA с заранее умноженной прозрачностью."""
    rgba = np.asarray(frame, dtype=np.uint8)
    alpha = rgba[..., 3:4].astype(np.uint16)
    rgb = ((rgba[..., :3].astype(np.uint16) * alpha + 127) // 255).astype(np.uint8)
    return np.concatenate([rgb[..., ::-1], rgba[..., 3:4]], axis=2).tobytes()


def _font(size: int):  # noqa: ANN202
    from PIL import ImageFont

    for name in ("bahnschrift.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, font, width: int, max_lines: int) -> list[str]:  # noqa: ANN001
    lines: list[str] = []
    current = ""
    words = " ".join(text.split()).split(" ")
    for word in words:
        probe = f"{current} {word}".strip()
        if font.getlength(probe) <= width:
            current = probe
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    used = sum(len(line) + 1 for line in lines)
    if used < len(" ".join(words)) and lines:
        while lines[-1] and font.getlength(lines[-1] + "…") > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return lines


def render_bubble(text: str, color: tuple[int, int, int], scale: float, tail_right: bool) -> Image.Image:
    """Подпись рядом с талисманом в стиле панели: прямые углы, тонкая рамка и метка в сторону реактора."""
    k = scale
    font = _font(max(11, round(13.5 * k)))
    max_width = round(232 * k)
    lines = _wrap(text, font, max_width, 4)
    line_height = round(19 * k)
    pad_x, pad_y = round(13 * k), round(9 * k)
    tail = round(8 * k)
    text_width = max((font.getlength(line) for line in lines), default=10)
    width = int(text_width) + pad_x * 2 + tail + 2
    height = line_height * max(1, len(lines)) + pad_y * 2 + 2
    ss = 3
    panel = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel, "RGBA")
    left = 1 if tail_right else tail + 1
    right = width - tail - 1 if tail_right else width - 1
    box = (left * ss, 1 * ss, right * ss, (height - 1) * ss)
    draw.rectangle(box, fill=(4, 14, 24, 240), outline=(*color, 120), width=max(2, ss))
    # Уголки ярче рамки: тот же рисунок, что и у панелей большого окна.
    corner = round(7 * k) * ss
    x0, y0, x1, y1 = box
    for x, y, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        draw.line([(x, y), (x + dx * corner, y)], fill=(*color, 255), width=max(2, ss))
        draw.line([(x, y), (x, y + dy * corner)], fill=(*color, 255), width=max(2, ss))
    middle = height / 2
    if tail_right:
        points = [((right - 1) * ss, (middle - tail * 0.7) * ss), ((width - 1) * ss, middle * ss), ((right - 1) * ss, (middle + tail * 0.7) * ss)]
    else:
        points = [((left + 1) * ss, (middle - tail * 0.7) * ss), (1 * ss, middle * ss), ((left + 1) * ss, (middle + tail * 0.7) * ss)]
    draw.polygon(points, fill=(4, 14, 24, 240))
    draw.line([points[0], points[1], points[2]], fill=(*color, 160), width=max(2, ss))
    image = panel.reduce(ss)
    text_draw = ImageDraw.Draw(image, "RGBA")
    y = pad_y
    for line in lines:
        text_draw.text((left + pad_x - 1, y), line, font=font, fill=(226, 245, 255, 255))
        y += line_height
    return image
