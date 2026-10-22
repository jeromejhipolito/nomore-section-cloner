import json

from sectioncloner import lint
from sectioncloner.agent import SECTION_FILE, SECTION_TYPE, TEMPLATE_FILE, TEMPLATE_KEY, content_gaps
from sectioncloner.browser import Shot, Source

SCHEMA = '{% schema %}{"name": "Landing", "settings": [{"type": "text", "id": "title", "label": "Title"}],' \
         ' "blocks": [{"type": "slide", "name": "Slide", "settings": [{"type": "url", "id": "image_url",' \
         ' "label": "Image"}]}]}{% endschema %}'

CLEAN = """
<div class="x"><h1>{{ section.settings.title }}</h1>
{% for block in section.blocks %}<img src="{{ block.settings.image_url }}" alt="{{ block.settings.alt }}">{% endfor %}
<svg viewBox="0 0 10 10"><text>5</text></svg>
<span>&#9733;</span> | </div>
{% style %}.x::after { content: "Hardcoded but CSS"; background: url({{ section.settings.bg | image_url }}); }{% endstyle %}
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
    k = TEMPLATE_KEY
    good = json.dumps({"sections": {k: {"type": SECTION_TYPE}}, "order": [k]})
    assert lint.check_template(good, SECTION_TYPE, k) == []
    two = json.dumps({"sections": {k: {"type": SECTION_TYPE}, "j": {"type": "main-product"}}, "order": [k, "j"]})
    assert lint.check_template(two, SECTION_TYPE, k)
    assert lint.check_template("{", SECTION_TYPE, k)[0].startswith("template JSON")
    assert lint.check_template(json.dumps({"sections": {k: {"type": SECTION_TYPE}}, "order": []}), SECTION_TYPE, k)
    # the screenshot selector depends on the key, so a different key is rejected even with the right type
    other = json.dumps({"sections": {"landing": {"type": SECTION_TYPE}}, "order": ["landing"]})
    assert lint.check_template(other, SECTION_TYPE, k)


def test_content_gaps_finds_missing_text_and_images():
    src = Source(url="u", digest="", texts=["Feel better", "Free shipping today", "ok"],
                 images=["https://shop.example/cdn/shop/files/a.jpg?v=1", "https://shop.example/cdn/shop/files/b.jpg"],
                 shot=Shot())
    files = {SECTION_FILE: "", TEMPLATE_FILE: json.dumps({"t": "<p>Feel   <strong>better</strong></p>",
                                                          "img": "https://shop.example/cdn/shop/files/a.jpg?v=2"})}
    gaps = content_gaps(src, files)
    assert gaps["missing_texts"] == ["Free shipping today"]  # "ok" is too short to count
    assert gaps["missing_image_paths"] == ["/cdn/shop/files/b.jpg"]


def test_parse_template_strips_shopify_header():
    from sectioncloner.shopify import parse_template
    stored = "/*\n * IMPORTANT: The contents of this file are auto-generated.\n */\n{\"order\": [\"k\"]}"
    assert parse_template(stored) == {"order": ["k"]}
    assert parse_template('{"order": []}') == {"order": []}


def test_liquid_string_literals_are_hardcoded_text():
    assert any("Liquid string" in p for p in lint.check_section(CLEAN.replace(
        "{{ section.settings.title }}", "{{ 'Buy now' | upcase }}")))
    assert lint.check_section(CLEAN.replace("{{ section.settings.title }}", "{{ 'products.add' | t }}")) == []


def test_remote_css_url_is_caught_but_setting_urls_pass():
    bad = CLEAN.replace("url({{ section.settings.bg | image_url }})", "url(https://shop.example/cdn/shop/files/bg.jpg)")
    assert "remote url(...) in CSS (put the image in a setting)" in lint.check_section(bad)
    assert lint.check_section(CLEAN) == []


def test_value_attribute_copy_is_caught_but_quantities_pass():
    bad = CLEAN.replace("<span>", '<input type="submit" value="Add to cart"><span>')
    assert any("value=" in p for p in lint.check_section(bad))
    ok = CLEAN.replace("<span>", '<input type="hidden" name="quantity" value="2"><span>')
    assert lint.check_section(ok) == []


def test_content_gaps_no_false_positives_for_case_or_quotes():
    src = Source(url="u", digest="", texts=['Say "hi" now'], images=["https://s.example/cdn/shop/files/IMG_01.jpg"],
                 shot=Shot())
    files = {SECTION_FILE: "", TEMPLATE_FILE: json.dumps({"t": 'Say "hi" now',
                                                          "img": "https://s.example/cdn/shop/files/IMG_01.jpg"})}
    gaps = content_gaps(src, files)
    assert gaps["missing_texts"] == [] and gaps["missing_image_paths"] == []


def test_copy_hidden_in_liquid_filters_and_tags_is_caught():
    for bad in ["{{ block.settings.label | default: 'Best Seller' }}", "{{ price | append: ' OFF' }}",
                "{%- assign cta = 'Add to cart' -%}{{ cta }}", "{{ \"Don't miss out\" }}"]:
        assert any("Liquid string" in p for p in lint.check_section(CLEAN.replace("{{ section.settings.title }}", bad))), bad


def test_machine_keys_in_liquid_and_radio_values_pass():
    ok = CLEAN.replace("{{ section.settings.title }}",
                       "{% if b == 'two-pack' %}{{ section.settings.title | default: '' }}{% endif %}"
                       "{{ section.settings.font | font_face: font_display: 'swap' }}"
                       '<input type="radio" name="bundle" value="two-pack">{{ \'products.add\' | t }}')
    assert lint.check_section(ok) == []
