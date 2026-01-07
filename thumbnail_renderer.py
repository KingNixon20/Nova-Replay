from PIL import Image, ImageDraw, ImageFilter, ImageFont
import os


def _resize_and_crop(im, target_w, target_h):
    # Resize while preserving aspect ratio, then center-crop to target
    src_w, src_h = im.size
    src_ratio = src_w / src_h
    target_ratio = target_w / target_h
    if src_ratio > target_ratio:
        # source is wider -> scale by height
        new_h = target_h
        new_w = int(target_h * src_ratio)
    else:
        new_w = target_w
        new_h = int(target_w / src_ratio)
    im = im.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return im.crop((left, top, left + target_w, top + target_h))


def render_decorated_thumbnail(src_path, out_path_base, size=(160, 90), radius=14):
    """
    Create two PNG files: normal and hover variants.
    - src_path: source frame or video frame image
    - out_path_base: basename (without extension); function will write <base>.png and <base>_hover.png
    - size: (w,h) target size (16:9 recommended)
    Returns (normal_path, hover_path)
    """
    if not os.path.exists(src_path):
        return None, None

    try:
        im = Image.open(src_path).convert('RGBA')
    except Exception:
        return None, None

    target_w, target_h = size
    frame = _resize_and_crop(im, target_w, target_h)

    # create rounded mask
    mask = Image.new('L', (target_w, target_h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, target_w, target_h], radius=radius, fill=255)

    # create shadow
    shadow_pad = 12
    shadow = Image.new('RGBA', (target_w + shadow_pad * 2, target_h + shadow_pad * 2), (0, 0, 0, 0))
    shadow_mask = Image.new('L', (target_w, target_h), 0)
    sd = ImageDraw.Draw(shadow_mask)
    sd.rounded_rectangle([0, 0, target_w, target_h], radius=radius, fill=180)
    shadow.paste((0, 0, 0, 200), (shadow_pad, shadow_pad), shadow_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))

    # base image with shadow
    base = Image.new('RGBA', shadow.size, (0, 0, 0, 0))
    base.paste(shadow, (0, 0))

    # paste frame onto base with mask
    frame_pos = (shadow_pad, shadow_pad)
    base.paste(frame, frame_pos, mask)

    # apply bottom gradient overlay for contrast
    gradient = Image.new('RGBA', (target_w, int(target_h * 0.35)), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gradient)
    for i in range(gradient.height):
        alpha = int(200 * (i / gradient.height))
        gdraw.line([(0, i), (target_w, i)], fill=(0, 0, 0, alpha))
    base.paste(gradient, (shadow_pad, shadow_pad + target_h - gradient.height), gradient)

    # overlays: bottom-left pill "Copy Link"
    pill_w = int(target_w * 0.44)
    pill_h = 22
    pill_x = shadow_pad + 8
    pill_y = shadow_pad + target_h - pill_h - 8
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # pill background (semi-transparent)
    od.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h], radius=pill_h // 2, fill=(0, 0, 0, 160))
    # text
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 12)
    except Exception:
        font = ImageFont.load_default()
    text = "Copy Link"
    tw, th = od.textsize(text, font=font)
    text_x = pill_x + 10
    text_y = pill_y + (pill_h - th) // 2 - 1
    od.text((text_x, text_y), text, font=font, fill=(255, 255, 255, 230))
    # simple link icon (a small circle and line)
    icon_x = pill_x + 6
    icon_y = pill_y + pill_h // 2
    od.ellipse([icon_x - 5, icon_y - 5, icon_x + 5, icon_y + 5], fill=(255, 255, 255, 200))

    # bottom-right circular cloud/download icon
    circ_r = 18
    circ_x = shadow_pad + target_w - circ_r - 8
    circ_y = shadow_pad + target_h - circ_r - 8
    od.ellipse([circ_x - circ_r, circ_y - circ_r, circ_x + circ_r, circ_y + circ_r], fill=(40, 40, 40, 200))
    # simple cloud: small white rounded rectangle and circle
    cloud_cx = circ_x - 4
    cloud_cy = circ_y
    od.ellipse([cloud_cx - 6, cloud_cy - 6, cloud_cx + 6, cloud_cy + 6], fill=(255, 255, 255, 220))
    od.rectangle([cloud_cx - 10, cloud_cy - 2, cloud_cx + 6, cloud_cy + 6], fill=(255, 255, 255, 220))

    base = Image.alpha_composite(base, overlay)

    # normal output path
    normal_path = out_path_base + '.png'
    hover_path = out_path_base + '_hover.png'

    # save normal
    base.save(normal_path, format='PNG')

    # create hover variant with neon green thin border
    hover = base.copy()
    hdraw = ImageDraw.Draw(hover)
    border_color = (57, 255, 20, 220)  # neon green
    # draw rounded rect border over the frame area
    rect_xy = [shadow_pad, shadow_pad, shadow_pad + target_w, shadow_pad + target_h]
    for i in range(2):
        hdraw.rounded_rectangle([rect_xy[0]-i, rect_xy[1]-i, rect_xy[2]+i, rect_xy[3]+i], radius=radius+i, outline=border_color)

    hover.save(hover_path, format='PNG')

    return normal_path, hover_path
