import unittest

import numpy as np

from hidpi.bmfont import BMFont, Glyph
from hidpi.fontgen import marker, split_atlas


class SplitAtlasTests(unittest.TestCase):
    def test_split_preserves_pixels_advances_and_blank_glyphs(self):
        glyphs = [Glyph(i, i * 4, 0, 4, 4, -1, 2, 5) for i in range(4)]
        glyphs.append(Glyph(32, 0, 0, 0, 0, 0, 0, 3))
        atlas = np.zeros((8, 32, 4), dtype=np.float32)
        for g in glyphs[:4]:
            atlas[:, g.x * 2:(g.x + g.w) * 2] = [g.id / 4, .5, 1, 1]
        font = BMFont({}, {'lineHeight': '12', 'base': '9'}, glyphs)
        pages = split_atlas(font, atlas, 12, 12)
        actual = {}
        for result, pixels, w, h in pages:
            self.assertEqual(pixels.shape, (h * 2, w * 2, 4))
            np.testing.assert_array_equal(pixels[0, 0], marker(w * 2, 1))
            for g in result.glyphs:
                self.assertNotIn(g.id, actual)
                actual[g.id] = g
                src = next(item for item in glyphs if item.id == g.id)
                self.assertEqual((g.xa, g.xo, g.yo), (src.xa, src.xo, src.yo))
                if g.w:
                    np.testing.assert_array_equal(
                        pixels[g.y*2:(g.y+g.h)*2, g.x*2:(g.x+g.w)*2],
                        atlas[src.y*2:(src.y+src.h)*2, src.x*2:(src.x+src.w)*2])
        self.assertEqual(set(actual), {g.id for g in glyphs})

    def test_all_blank_font_still_has_one_valid_page(self):
        font = BMFont({}, {}, [Glyph(32, 0, 0, 0, 0, 0, 0, 4)])
        pages = split_atlas(font, np.zeros((8, 8, 4), dtype=np.float32), 12, 12)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][0].glyphs[0].xa, 4)

    def test_oversize_glyph_rejects_missing_padding(self):
        font = BMFont({}, {}, [Glyph(65, 0, 0, 10, 4, 0, 0, 10)])
        with self.assertRaises(ValueError):
            split_atlas(font, np.zeros((8, 20, 4), dtype=np.float32), 12, 12)
