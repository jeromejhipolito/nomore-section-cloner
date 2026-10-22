import json

from sectioncloner import lint
from sectioncloner.agent import SECTION_FILE, SECTION_TYPE, TEMPLATE_FILE, content_gaps
from sectioncloner.browser import Shot, Source

SCHEMA = '{% schema %}{"name": "Landing", "settings": [{"type": "text", "id": "title", "label": "Title"}],' \
         ' "blocks": [{"type": "slide", "name": "Slide", "settings": [{"type": "url", "id": "image_url",' \
         ' "label": "Image"}]}]}{% endschema %}'

CLEAN = """
<div class="x"><h1>{{ section.settings.title }}</h1>
{% for block in section.blocks %}<img src="{{ block.settings.image_url }}" alt="{{ block.settings.alt }}">{% endfor %}
<svg viewBox="0 0 10 10"><text>5</text></svg>
<span>&#9733;</span> | </div>
{% style %}.x::after { content: "Hardcoded but CSS"; background: url(https://cdn.example.com/a.png); }{% endstyle %}
<script>console.log("code, not copy")</script>
""" + SCHEMA


def test_clean_section_passes():
    assert lint.check_section(CLEAN) == []


def test_hardcoded_text_is_caught():
    problems = lint.check_section(CLEAN.replace("{{ section.settings.title }}", "Feel better fast"))
    assert any("hardcoded text" in p for p in problems)


def test_hardcoded_image_url_and_alt_are_caught():
    bad = CLEAN.replace('src="{{ block.settings.image_url }}"', 'src="https://shop.example/cdn/shop/files/a.jpg"')
    bad = bad.replace('alt="{{ block.settings.alt }}"', 'alt="A device"')
    problems = lint.check_section(bad)
    assert any("src=" in p for p in problems)
    assert any("alt=" in p for p in problems)


def test_text_mixed_with_liquid_is_caught():
    bad = CLEAN.replace("{{ section.settings.title }}", "Only {{ section.settings.title }} today")
    assert any("hardcoded text" in p for p in lint.check_section(bad))


def test_missing_or_broken_schema():
    assert lint.check_section("<p>{{ x }}</p>") == ["schema: no {% schema %} block"]
    assert lint.check_section("{% schema %}{bad json{% endschema %}")[0].startswith("schema:")


def test_duplicate_setting_ids():
    s = '{% schema %}{"name":"a","settings":[{"type":"text","id":"t","label":"a"},' \
        '{"type":"text","id":"t","label":"b"}]}{% endschema %}'
    assert "duplicate setting id t" in lint.check_section(s)


def test_template_shape():
    good = json.dumps({"sections": {"k": {"type": SECTION_TYPE}}, "order": ["k"]})
    assert lint.check_template(good, SECTION_TYPE) == []
    two = json.dumps({"sections": {"k": {"type": SECTION_TYPE}, "j": {"type": "main-product"}}, "order": ["k", "j"]})
    assert lint.check_template(two, SECTION_TYPE)
    assert lint.check_template("{", SECTION_TYPE)[0].startswith("template JSON")
    assert lint.check_template(json.dumps({"sections": {"k": {"type": SECTION_TYPE}}, "order": []}), SECTION_TYPE)


def test_content_gaps_finds_missing_text_and_images():
    src = Source(url="u", digest="", texts=["Feel better", "Free shipping today", "ok"],
                 images=["https://shop.example/cdn/shop/files/a.jpg?v=1", "https://shop.example/cdn/shop/files/b.jpg"],
                 shot=Shot())
    files = {SECTION_FILE: "", TEMPLATE_FILE: json.dumps({"t": "<p>Feel   <strong>better</strong></p>",
                                                          "img": "https://shop.example/cdn/shop/files/a.jpg?v=2"})}
    gaps = content_gaps(src, files)
    assert gaps["missing_texts"] == ["Free shipping today"]  # "ok" is too short to count
    assert gaps["missing_image_paths"] == ["/cdn/shop/files/b.jpg"]
