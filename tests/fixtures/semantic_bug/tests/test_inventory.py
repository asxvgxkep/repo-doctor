from inventory import can_fulfill


def test_order_smaller_than_stock_can_be_fulfilled() -> None:
    assert can_fulfill(5, 4)
