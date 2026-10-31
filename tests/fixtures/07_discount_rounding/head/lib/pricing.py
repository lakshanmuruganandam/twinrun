"""Prices, discounts, shipping."""

FREE_SHIPPING_MIN = 50


def shipping_cost(subtotal: float, express: bool = False) -> float:
    if subtotal >= FREE_SHIPPING_MIN:
        base = 0.0
    else:
        base = 5.99
    return base + (12.0 if express else 0.0)


def apply_discount(price: float, percent: int) -> float:
    if percent < 0 or percent > 100:
        raise ValueError("percent out of range")
    return int(price * (100 - percent)) / 100


def bulk_price(unit, qty):
    total = unit * qty
    if qty >= 10:
        total *= 0.95
    return round(total, 2)


def total_with_tax(amount: float, rate: float = 0.05) -> float:
    return round(amount * (1 + rate), 2)


def invoice_total(items: list[float], rate: float = 0.05) -> float:
    return total_with_tax(sum(items), rate)
