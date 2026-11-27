from printq.slicer import _norm


def test_line_breaks_compare_equal_however_they_are_written():
    # An end G-code override arrives with a real line break; the sliced file
    # reports the same text with an escaped one. j12 stopped on this.
    assert _norm("PRINT_END\n") == _norm("PRINT_END\\n")
    assert _norm("A\nB") == _norm("A\\nB")
    assert _norm("A\nB") != _norm("A\nC")
