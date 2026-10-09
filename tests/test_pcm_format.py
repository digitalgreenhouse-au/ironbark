import numpy as np

from ironbark.tools.pcm_window import fmt_float, fmt_int, to_file_value


def test_file_values_from_bit_perfect_int32_words():
    # Bit-perfect mode: a 24-bit value sits in the top 24 bits of the 32-bit word.
    words = np.array([[0x12345600, -0x100]], dtype=np.int32)
    assert to_file_value(words, 24).tolist() == [[0x123456, -1]]
    assert to_file_value(np.array([[-0x80000000]], np.int32), 16).tolist() == [[-32768]]


def test_file_values_from_shared_mode_floats():
    floats = np.array([[0.5, -1.0]], dtype=np.float32)
    assert to_file_value(floats, 16).tolist() == [[16384, -32768]]


def test_twos_complement_hex_and_binary():
    assert fmt_int(-1, 24, "Hex") == "0xFFFFFF"
    assert fmt_int(0x123456, 24, "Hex") == "0x123456"
    assert fmt_int(-32768, 16, "Binary") == "10000000 00000000"
    assert fmt_int(5, 16, "Binary") == "00000000 00000101"
    assert fmt_int(0x12345600, 32, "Binary") == "00010010 00110100 01010110 00000000"
    assert fmt_int(-8388608, 24, "Decimal").strip() == "-8,388,608"


def test_ieee_754_float_views():
    assert fmt_float(1.0, "Hex") == "0x3F800000"
    assert fmt_float(-2.0, "Binary") == "1 10000000 00000000000000000000000"
