import pytest

from kb.retrieve import rrf_fuse, tenant_scope


def test_rrf_scores_match_formula():
    fused = dict(rrf_fuse([["a", "b", "c"], ["c", "a"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["b"] == pytest.approx(1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)


def test_rrf_order_and_ties():
    order = [item for item, _ in rrf_fuse([["a", "b", "c"], ["c", "a"]])]
    assert order == ["a", "c", "b"]
    # same rank in each list -> tie broken by first appearance
    assert [i for i, _ in rrf_fuse([["x"], ["y"]])] == ["x", "y"]


def test_rrf_item_in_both_lists_beats_top_of_one():
    order = [i for i, _ in rrf_fuse([["solo", "both"], ["both"]])]
    assert order[0] == "both"


def test_rrf_empty():
    assert rrf_fuse([[], []]) == []


def test_tenant_scope_is_strict():
    assert tenant_scope("nitro") == ["nitro"]
    assert tenant_scope("nitro", include_shared=True) == ["nitro", "shared"]
    assert tenant_scope("shared", include_shared=True) == ["shared"]
    with pytest.raises(ValueError):
        tenant_scope("nitro' OR 1=1 --")
