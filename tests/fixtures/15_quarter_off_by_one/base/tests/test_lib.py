from lib.pricing import bulk_price, shipping_cost
from lib.text import word_count, slugify
from lib.users import password_ok


def test_bulk():
    assert bulk_price(2.5, 4) == 10.0
    assert bulk_price(1.0, 12) == 11.4


def test_words():
    assert word_count("hello big world") == 3
    assert word_count("") == 0


def test_misc():
    assert slugify("Hello World") == "hello-world"
    assert shipping_cost(10.0) == 5.99
    assert password_ok("hunter22")
