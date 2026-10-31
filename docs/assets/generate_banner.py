#!/usr/bin/env python3
# Copyright 2026 Hunonic
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sinh banner Hunonic cho README.

    python3 docs/assets/generate_banner.py

Bố cục (theo banner thương hiệu): nền gần đen, logo Hunonic bên trái kèm quầng sáng
xanh, chữ "HUNONIC" mảnh và giãn rộng ở bên phải. Mọi thứ suy ra từ logo trong repo
(``docs/assets/logo/hunonic-logo.png``), nên muốn đổi kích thước hay chữ thì sửa tham
số dòng lệnh rồi chạy lại.

- **Quầng sáng**: lấy chính alpha của logo làm mặt nạ, phóng to + làm mờ ⇒ quầng bám
  theo hình dạng logo chứ không phải một vòng tròn chung chung.
- **Chữ**: mặt mảnh (UltraLight) + giãn chữ, canh giữa theo **hộp mực thật** của chuỗi
  (``getbbox``) chứ không theo dòng chữ, nên không bị lệch quang học.
- **Font**: dò theo danh sách đường dẫn của macOS rồi Linux; không tải font từ mạng và
  không cần commit file font nào.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))

#: (đường dẫn, chỉ số mặt chữ trong file .ttc) — mặt mảnh nhất có sẵn, thử lần lượt.
FONT_CANDIDATES: list[tuple[str, int]] = [
    ("/System/Library/Fonts/HelveticaNeue.ttc", 5),  # Helvetica Neue UltraLight
    ("/System/Library/Fonts/HelveticaNeue.ttc", 12),  # Helvetica Neue Thin
    ("/System/Library/Fonts/Avenir Next.ttc", 10),  # Avenir Next Ultra Light
    ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
]

BG = (9, 11, 13)  # nền gần đen
GLOW = (40, 175, 105)  # xanh lá Hunonic, nhạt hơn để làm quầng
INK = (240, 245, 242)  # màu chữ


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """Font mảnh đầu tiên tìm được; không có thì dùng font mặc định của PIL."""
    for path, index in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size)


def glow_layer(size: tuple[int, int], shape: Image.Image, box: tuple[int, int],
               spread: int, blur: int, strength: float) -> Image.Image:
    """Quầng sáng RGBA của ``shape`` (alpha làm mặt nạ), phóng to ``spread`` rồi mờ."""
    mask = Image.new("L", size, 0)
    x, y = box
    grown = shape.split()[3].point(lambda v: int(v * strength))
    grown = grown.resize((shape.width + spread * 2, shape.height + spread * 2), Image.LANCZOS)
    mask.paste(grown, (x - spread, y - spread))
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(Image.new("RGBA", size, GLOW + (255,)), (0, 0), mask)
    return layer


def draw_wordmark(img: Image.Image, text: str, left: int, right: int,
                  size: int, tracking: int) -> None:
    """Vẽ ``text`` mảnh + giãn chữ, canh giữa ngang trong [left, right] và giữa dọc."""
    font = find_font(size)
    widths = [font.getlength(c) for c in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = left + (right - left - total) / 2
    bbox = font.getbbox(text)
    y = img.height / 2 - (bbox[1] + bbox[3]) / 2
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for char, width in zip(text, widths):
        draw.text((x, y), char, font=font, fill=INK + (255,))
        x += width + tracking
    img.alpha_composite(layer)


def build(width: int, height: int, text: str, logo_path: str, out_path: str) -> None:
    img = Image.new("RGBA", (width, height), BG + (255,))

    logo = Image.open(logo_path).convert("RGBA")
    logo_size = int(height * 0.68)
    logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
    box = (int(width * 0.14 - logo_size / 2), (height - logo_size) // 2)

    # Quầng rộng (ánh sáng quanh logo) → quầng sát (viền sáng) → chính logo.
    img.alpha_composite(glow_layer(img.size, logo, box, spread=110, blur=48, strength=0.55))
    img.alpha_composite(glow_layer(img.size, logo, box, spread=34, blur=16, strength=0.85))
    img.alpha_composite(logo, box)

    draw_wordmark(img, text,
                  left=box[0] + logo_size + int(width * 0.03),
                  right=width - int(width * 0.05),
                  size=int(height * 0.255), tracking=int(height * 0.123))

    img.convert("RGB").save(out_path, optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sinh banner Hunonic cho README")
    parser.add_argument("--width", type=int, default=1800)
    parser.add_argument("--height", type=int, default=440)
    parser.add_argument("--text", default="HUNONIC")
    parser.add_argument("--logo", default=os.path.join(ASSETS_DIR, "logo", "hunonic-logo.png"))
    parser.add_argument("--out", default=os.path.join(ASSETS_DIR, "hunonic-banner.png"))
    args = parser.parse_args()

    build(args.width, args.height, args.text, args.logo, args.out)
    print(f"Đã ghi {args.out} ({args.width}×{args.height})")


if __name__ == "__main__":
    main()
