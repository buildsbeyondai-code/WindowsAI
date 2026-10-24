from dedupe import top_consumers, unique_in_order


def test_order_and_uniqueness():
    assert unique_in_order(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_empty_and_unhashable_free():
    assert unique_in_order([]) == []
    assert unique_in_order(["x"] * 5) == ["x"]


def test_large_input_matches_reference():
    data = [f"p{i % 997}" for i in range(20000)]
    ref = list(dict.fromkeys(data))
    assert unique_in_order(data) == ref


def test_top_consumers():
    s = [{"name": "a", "cpu": 1}, {"name": "b", "cpu": 3}, {"name": "a", "cpu": 3}]
    assert top_consumers(s, 2) == ["a", "b"]
