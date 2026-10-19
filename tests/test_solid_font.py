import unittest
from unittest.mock import patch

import numpy as np

from hidpi.bmfont import Glyph
from hidpi.fontgen import PAD, FontSpec, convert_glyph, fill_level
from hidpi.imageops import sharpen_coverage


class Outline:
    def has(self, cp):
        return True

    def render(self, cp, cell_w, cell_h, pen_x, base_y, scale=1, ss=8):
        out = np.zeros((cell_h * scale, cell_w * scale), dtype=np.float32)
        out[(PAD + 2)*scale:(PAD + 6)*scale,
            (PAD + 2)*scale:(PAD + 4)*scale] = 1.0
        return out


class SolidFontTests(unittest.TestCase):
    def setUp(self):
        self.glyph = Glyph(65, 0, 0, 10, 10, 0, 0, 10)
        # A white stroke with an opaque black shadow separated from it.
        self.texture = np.zeros((10, 10, 4), dtype=np.float32)
        self.texture[2:6, 2:4] = 1.0
        self.texture[3:7, 6:8, 3] = 1.0
        self.spec = FontSpec(effects=True, solid=True, substitute=True)

    def test_outline_output_does_not_copy_source_shadow(self):
        ttf = Outline()
        result = convert_glyph(self.glyph, self.texture, 8, self.spec, ttf, (0, 0))
        expected = ttf.render(65, 14, 14, PAD, PAD + 8, scale=2)
        np.testing.assert_array_equal(result.rgba[..., 3], expected)
        np.testing.assert_array_equal(result.rgba[..., :3][expected > 0], 1.0)
        self.assertEqual(fill_level(result.rgba, self.spec), 1.0)

    def test_missing_outline_uses_fill_not_shadow_as_fallback(self):
        with patch('hidpi.fontgen.trace.available', return_value=False):
            result = convert_glyph(self.glyph, self.texture, 8, self.spec, None, (0, 0))
        fill = np.zeros((14, 14), dtype=np.float32)
        fill[PAD+2:PAD+6, PAD+2:PAD+4] = 1.0
        np.testing.assert_array_equal(result.rgba[..., 3], sharpen_coverage(fill))

    def test_styled_non_solid_font_still_keeps_shadow(self):
        result = convert_glyph(self.glyph, self.texture, 8,
                               FontSpec(effects=True, substitute=True), Outline(), (0, 0))
        fill = Outline().render(65, 14, 14, PAD, PAD + 8, scale=2)
        self.assertGreater(float(result.rgba[..., 3][fill == 0].sum()), 0.0)

    def test_dim_stroke_is_not_mistaken_for_shadow(self):
        self.texture[2:6, 2:4, :3] = 0.25
        result = convert_glyph(self.glyph, self.texture, 8, self.spec, Outline(), (0, 0))
        self.assertEqual(result.source, 'ttf')
        expected = Outline().render(65, 14, 14, PAD, PAD + 8, scale=2)
        np.testing.assert_array_equal(result.rgba[..., 3], expected)

    def test_dim_missing_outline_keeps_fill_without_black_shadow(self):
        self.texture[2:6, 2:4, :3] = 0.25
        with patch('hidpi.fontgen.trace.available', return_value=False):
            result = convert_glyph(self.glyph, self.texture, 8, self.spec, None, (0, 0))
        fill = np.zeros((14, 14), dtype=np.float32)
        fill[PAD+2:PAD+6, PAD+2:PAD+4] = 1.0
        np.testing.assert_array_equal(result.rgba[..., 3], sharpen_coverage(fill))


if __name__ == '__main__':
    unittest.main()
