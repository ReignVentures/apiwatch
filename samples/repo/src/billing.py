import stripe


def is_ten_dollars(price_id):
    price = stripe.Price.retrieve(price_id)
    return price["unit_amount_decimal"] == "1000"  # string compare; breaks when type changes
