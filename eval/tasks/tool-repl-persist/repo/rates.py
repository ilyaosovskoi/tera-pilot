"""Order pricing data.

RATES maps product -> unit price (USD). TAX is the sales tax fraction.
ORDERS maps order id -> {product: quantity}.
"""

RATES = {
    "widget": 19.99,
    "gadget": 4.50,
    "sprocket": 12.75,
}

TAX = 0.20

ORDERS = {
    "order-7": {"widget": 3, "gadget": 10},
    "order-8": {"sprocket": 2, "gadget": 1},
}
