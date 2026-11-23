"""Verify readable saved annotation labels and orientation-safe photo overlays.

Depends on Pillow and the real manual renderer; temporary images check that the
same French labels and class colors reach saved evidence without inference.
"""
from io import BytesIO
from PIL import Image, ImageDraw, ImageOps
import pytest
from api.private_demo.annotation_render import annotation_appearances, annotation_color, render_annotation
from api.private_demo.editor_schemas import EditorDocument, Region


def test_saved_overlays_use_french_labels_and_distinct_class_colors(monkeypatch):
    document = EditorDocument.model_validate({
        'title': 'Démo', 'category': 'chiffrage_rapide', 'metadata': {},
        'parts': [{'id': 'part', 'name': 'Capot', 'model_class': 'p_Capot', 'damages': [
            {'id': 'scratch', 'type': 'd_rayure'}, {'id': 'dent', 'type': 'd_bosse'},
        ]}],
        'photos': [{'id': 'photo', 'regions': [
            {'id': 'part-zone', 'layer': 'parts', 'shape': 'rectangle', 'part_id': 'part', 'points': [{'x': .1, 'y': .3}, {'x': .8, 'y': .8}]},
            {'id': 'scratch-zone', 'layer': 'damages', 'shape': 'rectangle', 'part_id': 'part', 'detection_id': 'scratch', 'points': [{'x': .2, 'y': .4}, {'x': .4, 'y': .6}]},
            {'id': 'dent-zone', 'layer': 'damages', 'shape': 'rectangle', 'part_id': 'part', 'detection_id': 'dent', 'points': [{'x': .5, 'y': .4}, {'x': .7, 'y': .6}]},
        ]}],
    })
    appearances = annotation_appearances(document)
    assert appearances['part-zone']['label'] == 'Capot'
    assert appearances['scratch-zone']['label'] == 'Rayure'
    assert appearances['dent-zone']['label'] == 'Bosse'
    assert appearances['scratch-zone']['color'] != appearances['dent-zone']['color']
    text_drawn = []
    draw_text = ImageDraw.ImageDraw.text

    def capture_text(self, position, text, *args, **kwargs):
        text_drawn.append(text)
        return draw_text(self, position, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', capture_text)
    original = BytesIO()
    Image.new('RGB', (900, 600), 'white').save(original, 'PNG')
    output = render_annotation(original.getvalue(), document.photos[0].regions, 'damages', appearances=appearances)
    rendered = Image.open(BytesIO(output))
    assert rendered.size == (900, 600)
    assert text_drawn == ['Rayure', 'Bosse']
    assert rendered.getpixel((180, 240)) != (255, 255, 255)
    assert rendered.getpixel((850, 550)) == (255, 255, 255)


@pytest.mark.parametrize('orientation', range(1, 9))
def test_labels_and_regions_remain_upright_for_all_exif_orientations(orientation):
    region = Region(id='r', layer='damages', shape='rectangle', points=[{'x': .15, 'y': .4}, {'x': .6, 'y': .8}])
    appearances = {'r': {'label': 'Rayure', 'color': annotation_color('d_rayure')}}

    def original(exif_orientation):
        buffer = BytesIO()
        image = Image.new('RGB', (80, 120) if exif_orientation >= 5 else (120, 80), 'white')
        exif = image.getexif()
        exif[274] = exif_orientation
        image.save(buffer, 'JPEG', exif=exif)
        return buffer.getvalue()

    reference = ImageOps.exif_transpose(Image.open(BytesIO(render_annotation(original(1), [region], 'damages', appearances=appearances))))
    saved = Image.open(BytesIO(render_annotation(original(orientation), [region], 'damages', appearances=appearances)))
    assert saved.getexif()[274] == orientation
    displayed = ImageOps.exif_transpose(saved)
    assert displayed.size == reference.size
    assert displayed.tobytes() == reference.tobytes()
