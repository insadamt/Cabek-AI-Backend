"""Render named, class-colored photo evidence without inference dependencies.

Depends on Pillow, the editor catalog and normalized display coordinates; labels
are drawn upright before restoring the original EXIF orientation and dimensions.
"""
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps
from api.private_demo.catalog import DAMAGE_CLASSES

PALETTE = ('#2563c5', '#087f78', '#8042bc', '#b83280', '#b45318', '#ca3c4b',
           '#426ab3', '#537b16', '#8c4c2b', '#5362a5', '#147b97', '#9b3673')
DAMAGE_LABELS = dict(DAMAGE_CLASSES)


def annotation_color(key):
    return PALETTE[sum(ord(character) for character in key) % len(PALETTE)]


def annotation_appearances(document):
    parts = {part.id: part for part in document.parts}
    appearances = {}
    for photo in document.photos:
        for region in photo.regions:
            part = parts.get(region.part_id)
            damage = next((item for item in part.damages if item.id == region.detection_id), None) if part else None
            if region.layer == 'critical':
                key, label = 'zone_critique', 'Zone critique'
            elif region.layer == 'damages':
                key = damage.type if damage else 'damages'
                label = DAMAGE_LABELS.get(key, key if damage else 'Dommage')
            else:
                key = (part.model_class or part.name) if part else 'parts'
                label = part.name if part else 'Pièce'
            appearances[region.id] = {'label': label, 'color': annotation_color(key)}
    return appearances


def annotation_font(size):
    try:
        return ImageFont.truetype('DejaVuSans.ttf', size)
    except OSError:
        return ImageFont.load_default(size=size)


def draw_annotation_labels(image, regions, appearances):
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font_size = max(10, round(width / 55))
    font = annotation_font(font_size)
    padding = max(3, round(font_size / 3))
    box_height = min(height, font_size + 2 * padding + 3)
    placed = []
    for region in regions:
        appearance = appearances[region.id]
        text = appearance['label'][:80]
        while len(text) > 1 and draw.textlength(text, font=font) + 2 * padding > width - 4:
            text = text[:-1]
        box_width = min(width - 4, round(draw.textlength(text, font=font)) + 2 * padding)
        x = max(2, min(width - box_width - 2, round(min(point.x for point in region.points) * width)))
        y = max(2, min(height - box_height - 2, round(min(point.y for point in region.points) * height) - box_height - 2))
        for _ in range(len(regions)):
            if not any(x < px + pw and x + box_width > px and y < py + ph and y + box_height > py for px, py, pw, ph in placed):
                break
            y = y + box_height + 2 if y + 2 * box_height + 2 < height else 2
        placed.append((x, y, box_width, box_height))
        draw.rounded_rectangle((x, y, x + box_width, y + box_height), radius=max(1, padding // 2), fill=appearance['color'])
        draw.text((x + padding, y + padding), text, font=font, fill='white', anchor='lt')


def render_annotation(original, regions, layer, flattened=None, appearances=None):
    with Image.open(BytesIO(original)) as source:
        original_exif = source.getexif()
        orientation = original_exif.get(274, 1)
    with Image.open(BytesIO(flattened or original)) as source:
        base = source.convert('RGBA')
    # Normalize the working canvas to browser orientation so text never rotates with the raw pixels.

    base.info['exif'] = original_exif.tobytes()
    base = ImageOps.exif_transpose(base)
    width, height = base.size
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_width = max(2, round(width / 350))
    visible = [region for region in regions if region.layer == layer]
    styles = dict(appearances or {})
    defaults = {'parts': 'Pièce', 'damages': 'Dommage', 'critical': 'Zone critique'}
    for region in visible:
        appearance = styles.setdefault(region.id, {'label': defaults[layer], 'color': annotation_color(layer)})
        color = tuple(bytes.fromhex(appearance['color'].lstrip('#')))
        points = [(round(point.x * (width - 1)), round(point.y * (height - 1))) for point in region.points]
        if region.shape == 'rectangle':
            points = [(min(x for x, _ in points), min(y for _, y in points)),
                      (max(x for x, _ in points), max(y for _, y in points))]
            draw.rectangle(points, fill=color + (38,), outline=color + (255,), width=line_width)
        else:
            draw.polygon(points, fill=color + (38,))
            draw.line(points + [points[0]], fill=color + (255,), width=line_width)
    rendered = Image.alpha_composite(base, overlay).convert('RGB')
    draw_annotation_labels(rendered, visible, styles)
    inverse = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
               4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
               6: Image.Transpose.ROTATE_90, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_270}
    if orientation in inverse:
        rendered = rendered.transpose(inverse[orientation])
    output = BytesIO()
    rendered.save(output, format='PNG', exif=original_exif)
    return output.getvalue()
