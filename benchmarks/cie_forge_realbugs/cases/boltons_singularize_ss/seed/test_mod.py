"""Regression test for singularize() not mangling words ending in 'ss'
(boltons #1e61524)."""
from mod import singularize


def test_singularize_double_s():
    # Words ending in a double 's' are already singular, so singularize must
    # not strip the trailing 's' and produce 'glas'/'bos'/'kis'.
    assert singularize('glass') == 'glass'
    assert singularize('boss') == 'boss'
    assert singularize('class') == 'class'
    assert singularize('kiss') == 'kiss'
    assert singularize('address') == 'address'
    assert singularize('business') == 'business'
    # Case pattern is preserved.
    assert singularize('Glass') == 'Glass'
    assert singularize('BOSS') == 'BOSS'
    # The real plurals of these words still singularize correctly.
    assert singularize('glasses') == 'glass'