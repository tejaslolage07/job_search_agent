from job_search_agent.parsers.html import html_to_text, normalize_location, unescape_then_text


def test_html_and_location():
    assert html_to_text("<p>Hi <b>x</b></p><ul><li>a</li><li>b</li></ul>") == "Hi x\n- a\n- b"
    assert unescape_then_text("&lt;p&gt;a &amp;amp; b&lt;/p&gt;") == "a & b"
    assert normalize_location("Bangalore,  India ") == "Bengaluru, India"
