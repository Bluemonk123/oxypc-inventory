"""Global Table property: the first column centers ONLY when it's a
checkbox; every other first column (Model Names, Tag Number, WorkID, ...)
stays left-aligned like any other text column. The last (Action) column
is always right-aligned regardless (2026-11-08; was centred).

Found 2026-09-03: the original CSS rule
(.gtable thead th:first-child, .gtable tbody td:first-child) centered every
Global Table's first column unconditionally — including plain-text ones
like Model Based Summary's "Model Names" (templates/devices/list.html).
Fixed by having global-table.js detect a checkbox in the rendered first
body cell and toggle a .gtable-checkbox-first class on the table, which the
first-column centering rule now requires.
"""
import pathlib

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _css():
    return (pathlib.Path(ROOT) / "static" / "css" / "app.css").read_text(encoding="utf-8")


def _js():
    return (pathlib.Path(ROOT) / "static" / "js" / "global-table.js").read_text(encoding="utf-8")


def test_last_column_right_aligned_unconditionally():
    css = _css()
    assert ".gtable thead th:last-child,\n.gtable tbody td:last-child" in css


def test_first_column_centering_requires_the_checkbox_first_class():
    css = _css()
    assert ".gtable.gtable-checkbox-first thead th:first-child" in css
    assert ".gtable.gtable-checkbox-first tbody td:first-child" in css
    # Not present as a bare, unconditional rule anymore.
    assert ".gtable thead th:first-child, .gtable thead th:last-child" not in css


def test_js_detects_checkbox_and_toggles_the_class_independent_of_freeze():
    js = _js()
    assert "function () {" in js
    assert "$table.toggleClass('gtable-checkbox-first', isCheckbox);" in js
    # Bound before the `if (freeze)` block, so the class is correct even for
    # a future table with opts.freeze: false.
    freeze_block_pos = js.index("if (freeze) {")
    mark_pos = js.index("markCheckboxFirstColumn")
    assert mark_pos < freeze_block_pos
