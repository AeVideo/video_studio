"""Сборка фото-коллажа для сцен, где не нашлось подходящего видео, но
нашлось 1-3 уверенных (score >= PHOTO_SCORE_THRESHOLD) фото. Собирается
Pillow-ом (текстурная подложка + фото под углом с тенью, как приколотые
к доске карточки), дальше ffmpeg просто оживляет готовую картинку через
zoompan (см. generate_collage_clip в assemble_video.py)."""
import random
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageEnhance


def _make_backdrop(w, h, tone=(60, 55, 50)):
    bg = Image.new("RGB", (w, h), tone)
    noise = Image.effect_noise((w, h), 28).convert("L")
    noise = ImageOps.colorize(noise, black=(0, 0, 0), white=(255, 255, 255))
    bg = Image.blend(bg, noise, 0.10)
    vign = Image.new("L", (w, h), 0)
    vd = ImageDraw.Draw(vign)
    vd.ellipse([-w * 0.3, -h * 0.5, w * 1.3, h * 1.1], fill=90)
    vign = vign.filter(ImageFilter.GaussianBlur(w // 6))
    light = Image.new("RGB", (w, h), tuple(min(255, c + 35) for c in tone))
    bg = Image.composite(light, bg, vign)
    return bg


def _polaroid(img_path, target_w, border=14, bottom_border=46, max_rotate=6):
    img = Image.open(img_path).convert("RGB")
    ratio = target_w / img.width
    img = img.resize((target_w, max(1, int(img.height * ratio))))
    img = ImageEnhance.Color(img).enhance(0.75)
    img = ImageEnhance.Contrast(img).enhance(0.92)
    card = Image.new("RGB", (target_w + border * 2, img.height + border * 2 + bottom_border), (245, 240, 228))
    card.paste(img, (border, border))
    angle = random.uniform(-max_rotate, max_rotate)
    card = card.rotate(angle, expand=True, fillcolor=(0, 0, 0, 0), resample=Image.BICUBIC)
    return card.convert("RGBA")


def _paste_with_shadow(base, card, xy):
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shad_layer = Image.new("RGBA", card.size, (0, 0, 0, 150))
    shad_mask = card.split()[-1] if card.mode == "RGBA" else None
    shadow.paste(shad_layer, (xy[0] + 10, xy[1] + 14), shad_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    base.alpha_composite(shadow)
    base.alpha_composite(card, xy)


def build_photo_collage(image_paths, out_path, canvas_w=1280, canvas_h=720):
    bg = _make_backdrop(canvas_w, canvas_h).convert("RGBA")
    n = len(image_paths)
    if n == 1:
        layout = [(0.5, 0.5, 0.55)]
    elif n == 2:
        layout = [(0.34, 0.46, 0.42), (0.68, 0.56, 0.40)]
    else:
        layout = [(0.26, 0.40, 0.36), (0.62, 0.30, 0.34), (0.55, 0.68, 0.38)]

    margin = 18
    for path, (cx, cy, wf) in zip(image_paths, layout):
        card = _polaroid(path, target_w=int(canvas_w * wf))
        max_h = canvas_h - margin * 2
        if card.height > max_h:
            card = _polaroid(path, target_w=int(canvas_w * wf * (max_h / card.height) * 0.97))
        x = int(canvas_w * cx - card.width / 2)
        y = int(canvas_h * cy - card.height / 2)
        x = max(margin - card.width // 4, min(x, canvas_w - card.width - margin + card.width // 4))
        y = max(margin, min(y, canvas_h - card.height - margin))
        _paste_with_shadow(bg, card, (x, y))

    out = bg.convert("RGB")
    out.save(out_path, quality=92)
    return out_path


def apply_archival_grade(img_path, out_path):
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    img = ImageEnhance.Color(img).enhance(0.85)
    sepia = Image.new("RGB", (w, h), (60, 50, 35))
    img = Image.blend(img, sepia, 0.06)
    noise = Image.effect_noise((w, h), 18).convert("L")
    noise_rgb = ImageOps.colorize(noise, black=(0, 0, 0), white=(255, 255, 255))
    img = Image.blend(img, noise_rgb, 0.05)
    vign = Image.new("L", (w, h), 255)
    vd = ImageDraw.Draw(vign)
    vd.ellipse([-w * 0.25, -h * 0.25, w * 1.25, h * 1.25], fill=0)
    vign = vign.filter(ImageFilter.GaussianBlur(w // 8))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    img = Image.composite(black, img, vign.point(lambda p: int(p * 0.55)))
    img.save(out_path, quality=92)
    return out_path
