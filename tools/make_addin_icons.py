"""Значки add-in (PNG) и значок программы (ICO) из логотипа в docs/assets. Запуск: python tools/make_addin_icons.py"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "anonymizer" / "addin_web"
LOGO = Image.open(ROOT / "docs" / "assets" / "logo.png").convert("RGBA")
MARK = Image.open(ROOT / "docs" / "assets" / "logo-mark.png").convert("RGBA")   # только капюшон: читается в 16 пикселей


def pick(size: int) -> Image.Image:
    return (MARK if size <= 80 else LOGO).resize((size, size), Image.LANCZOS)


for size in (16, 32, 64, 80, 128):
    pick(size).save(OUT / f"icon-{size}.png", optimize=True)

sizes = [16, 24, 32, 48, 64, 128, 256]
pick(256).save(ROOT / "packaging" / "app.ico", format="ICO", sizes=[(s, s) for s in sizes],
               append_images=[pick(s) for s in sizes if s != 256])
print("ok")
