#!/usr/bin/env python3
"""Builds the "HiDPI Text" HOI4 mod: 2x font atlases plus a text shader that samples them.

Run against the installed game; see README.md.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image

from hidpi.bmfont import load_rgba, read_fnt, save_dds_rgba, write_fnt
from hidpi.fontgen import FontSpec, build_atlas, split_atlas
from hidpi.imageops import luminance

MOD_NAME = 'HiDPI Text (2x)'
MOD_DIR = 'hidpi_text'
STEAM = Path.home() / '.local/share/Steam'
DEFAULT_GAME = STEAM / 'steamapps/common/Hearts of Iron IV'
PROTON_DOCS = STEAM / 'steamapps/compatdata/394360/pfx/drive_c/users/steamuser/Documents/Paradox Interactive/Hearts of Iron IV'
PROTON_DOCS_WIN = 'C:/users/steamuser/Documents/Paradox Interactive/Hearts of Iron IV'
REPO = Path(__file__).parent
FONTS = REPO / 'fonts'
LICENSES = REPO / 'licenses'
REPO_URL = 'https://github.com/zsr71/HOI4-HiDPI-SChinese'
UPSTREAM_URL = 'https://github.com/cspoelstra/HOI4-HiDPI'

# Drawn in world space by the map shaders, not text.shader.
SKIP = {'hoi_mapfont4', 'hoi_arrow_font'}

# Chinese fonts are split across several atlas pages and are referenced from
# core_chinese.gfx as gfx/fonts/chinese/<name>.  Map labels and arrow fonts use
# different shaders, so leave those on the vanilla path for now.
LANGUAGE_DIRS = {'chinese'}

# Outline fonts and em sizes (1x pixels) that best reproduce each vanilla bitmap font, found by
# maximising the correlation between rendered and original glyphs. Fonts not listed here are traced
# from their bitmaps instead (see hidpi/trace.py).
UBUNTU_B = 'Ubuntu:style=Bold'
UBUNTU_M = 'Ubuntu:style=Medium'
GOTHIC_DEMI = 'URW Gothic:style=Demi'
GOTHIC_BOOK = 'URW Gothic:style=Book'
SANS = 'Liberation Sans:style=Regular'
SERIF = 'Liberation Serif:style=Regular'
# The hoi4_typewriter fonts (AFL Font nonmetric) have no freely licensed outlines; their traced bitmaps
# come out lumpy. They are drawn from Podkova, the best-fitting openly licensed slab serif, at weights
# that match the vanilla strokes. Podkova is bundled in fonts/ (SIL OFL) as a variable font, given here
# as (file, weight).
PODKOVA_SEMIBOLD = ('Podkova[wght].ttf', 600)
PODKOVA_MEDIUM = ('Podkova[wght].ttf', 500)
OUTLINES = {
    'hoi_16mbs': (UBUNTU_B, 12.59),
    'hoi_16mbs_cryllic': (UBUNTU_B, 12.59),
    'hoi_18mbs': (UBUNTU_B, 14.09),
    'hoi_18mbs_cryllic': (UBUNTU_B, 14.09),
    'hoi_22tech': (UBUNTU_B, 16.84),
    'hoi_18': (UBUNTU_M, 13.83),
    'hoi_18_cryllic': (UBUNTU_M, 13.32),
    'hoi_20b': (UBUNTU_M, 15.10),
    'hoi_20b_cryllic': (UBUNTU_M, 14.62),
    'hoi_20bs': (UBUNTU_M, 15.31),
    'hoi_20bs_cryllic': (UBUNTU_M, 15.31),
    'hoi_26mbs': (UBUNTU_M, 20.35),
    'hoi_26mbs_cryllic': (UBUNTU_M, 20.35),
    'hoi_30': (UBUNTU_M, 29.85),
    'hoi_33': (UBUNTU_M, 25.83),
    'cg_16b': (GOTHIC_DEMI, 13.31),
    'hoi_16tooltip3': (GOTHIC_BOOK, 12.97, 1.0),
    'Arial12': (SANS, 11.59),
    'hoi_16typewriter': (SERIF, 14.58),
    'hoi_16typewriter_cryllic': (SERIF, 14.58),
    'hoi_16typewriter_inverted': (SERIF, 14.58),
    'hoi_16typewriter_cryllic_inverted': (SERIF, 14.58),
    'hoi_22typewriter': (SERIF, 19.82),
    'hoi_22typewriter_cryllic': (SERIF, 19.82),
    'hoi_22typewriter_inverted': (SERIF, 19.82),
    'hoi_22typewriter_cryllic_inverted': (SERIF, 19.82),
    'hoi4_typewriter16': (PODKOVA_SEMIBOLD, 15.0),
    'hoi4_typewriter16_inverted': (PODKOVA_SEMIBOLD, 15.0),
    'hoi4_typewriter22': (PODKOVA_MEDIUM, 19.94),
    'hoi4_typewriter22_inverted': (PODKOVA_MEDIUM, 19.94),
}

# Licence texts in licenses/ for each family of outline fonts the atlases are drawn from; they ship with
# the mod, listed in its CREDITS.txt.
FONT_LICENSES = {
    'Ubuntu': ['Ubuntu-Font-Licence-1.0.txt'],
    'URW Gothic': ['URW-base35-LICENSE.txt', 'URW-base35-COPYING-AGPL-3.0.txt'],
    'Liberation Sans': ['Liberation-OFL.txt'],
    'Liberation Serif': ['Liberation-OFL.txt'],
    'Podkova': ['Podkova-OFL.txt'],
}

# Tooltip fonts (cg_16b and its fallback file) are drawn straight from their outline font with a
# synthesised drop shadow, so text.shader knows the fill grey and can carry them into tooltips at 2x.
# Fill grey and drop shadow (dx, dy, sigma, strength in pixels) were measured from the vanilla bitmaps.
REDRAWN = {
    'cg_16b': dict(fill=0.77, shadow=(0.0, 1.0, 0.75, 1.2)),
    'hoi_16tooltip3': dict(fill=0.9, shadow=(0.5, 1.0, 2.0, 1.5)),
}

DX9_ALIGN = '''#ifdef PDX_DIRECTX_9
			// At UI scale 2 the DX9 GUI is half a screen pixel off D3D9's texel/pixel alignment, so 1:1
			// glyphs get averaged with their neighbours and 2x magnified art comes out unevenly jagged.
			// Shift sampling back onto texel centres.
			vHdUV += 0.5 * float2( ddx( vHdUV ).x, ddy( vHdUV ).y );
#endif'''

# Tooltips and buttons with baked-in labels (textSpriteType) are drawn by text.shader into an offscreen
# texture at one pixel per UI unit, which is then magnified to the UI scale like any other sprite.
# text.shader stores text drawn there as 2x2 sub-pixel coverage plus colour in texels with alpha 255
# (everything else it draws there stays below 255), and marks texel (0,0) with this colour so the
# shaders that draw the texture know to decode it. Texel (0,1) holds the colour at the centre of the
# background art, for text texels with no background next to them.
BEACON = '( float4( 90.0, 60.0, 165.0, 255.0 ) / 255.0 )'


def rcas(sampler):
    """HLSL that sets vHdSharp to OutColor.rgb sharpened with AMD FSR 1 RCAS (on premultiplied colour)."""
    return f'''float2 vHdDX = float2( ddx( vHdUV ).x, 0.0 );
			float2 vHdDY = float2( 0.0, ddy( vHdUV ).y );
			float4 vHdN = tex2D( {sampler}, vHdUV - vHdDY );
			float4 vHdS = tex2D( {sampler}, vHdUV + vHdDY );
			float4 vHdW = tex2D( {sampler}, vHdUV - vHdDX );
			float4 vHdE = tex2D( {sampler}, vHdUV + vHdDX );
			float3 vHdB = vHdN.rgb * vHdN.a;
			float3 vHdH = vHdS.rgb * vHdS.a;
			float3 vHdD = vHdW.rgb * vHdW.a;
			float3 vHdF = vHdE.rgb * vHdE.a;
			float3 vHdC = OutColor.rgb * OutColor.a;
			float3 vHdMin = min( min( vHdB, vHdD ), min( vHdF, vHdH ) );
			float3 vHdMax = max( max( vHdB, vHdD ), max( vHdF, vHdH ) );
			float3 vHdLobe3 = max( -vHdMin / ( 4.0 * vHdMax + 0.0001 ), ( 1.0 - vHdMax ) / ( 4.0 * vHdMin - 4.0001 ) );
			float vHdLobe = max( -0.1875, min( max( vHdLobe3.r, max( vHdLobe3.g, vHdLobe3.b ) ), 0.0 ) );
			float3 vHdSharp = saturate( ( vHdLobe * ( vHdB + vHdD + vHdF + vHdH ) + vHdC ) / ( 4.0 * vHdLobe + 1.0 ) );
			vHdSharp = saturate( vHdSharp / max( OutColor.a, 1.0 / 255.0 ) );'''


def target_text(sampler):
    '''HLSL that redraws a texture written by text.shader as a 1x render target (see BEACON).

    Texels with alpha 255 hold text: each screen pixel shows its sub-pixel's coverage of the text
    colour over the average of the surrounding non-text texels, or over the reference background if
    there are none. Next to text, the texture is magnified without blending in those texels. Updates
    vHdRGB and OutColor.a.
    '''
    names = {(i, j): f'vHdT{"mzp"[i + 1]}{"mzp"[j + 1]}' for j in (-1, 0, 1) for i in (-1, 0, 1)}
    # Text texels and the two marker texels are never blended into their neighbours.
    taps = '\n\t\t\t\t'.join(
        f'float4 {n} = tex2Dlod0( {sampler}, ( vHdCell + float2( {i + 0.5}, {j + 0.5} ) ) * vHdUnit );\n'
        f'\t\t\t\tfloat fHdP{n[4:]} = max( step( 254.5 / 255.0, {n}.a ), vHdCell.x == {-i}.0 && abs( vHdCell.y + {j - 0.5} ) < 1.0 ? 1.0 : 0.0 );'
        for (i, j), n in names.items())
    ring = [n for (i, j), n in names.items() if (i, j) != (0, 0)]
    bg_sum = ' + '.join(f'( 1.0 - fHdP{n[4:]} ) * {n}' for n in ring)
    bg_n = ' + '.join(f'fHdP{n[4:]}' for n in ring)
    return f'''// Texel size from the texture's UV change per UI unit, snapped to a whole number of texels so the
			// samples below hit texel centres exactly.
			float2 vHdUnit = abs( float2( ddx( vHdUV ).x, ddy( vHdUV ).y ) ) / max( abs( float2( ddx( v.vHdUI.x ), ddy( v.vHdUI.y ) ) ), 0.00001 );
			vHdUnit = 1.0 / max( floor( 1.0 / vHdUnit + 0.5 ), 1.0 );
			if ( all( abs( tex2Dlod0( {sampler}, 0.5 * vHdUnit ) - {BEACON} ) < 0.5 / 255.0 ) )
			{{
				float2 vHdPos = vHdUV / vHdUnit;
				float2 vHdCell = floor( vHdPos );
				float2 vHdSub = step( 0.5, vHdPos - vHdCell );
				{taps}
				float fHdText = fHdPzz + {bg_n};
				// The marker texels show their right-hand neighbour.
				float fHdCorner = vHdCell.x == 0.0 && abs( vHdCell.y - 0.5 ) < 1.0 ? 1.0 : 0.0;
				vHdTzz = lerp( vHdTzz, vHdTpz, fHdCorner );
				fHdPzz = lerp( fHdPzz, fHdPpz, fHdCorner );
				if ( fHdPzz > 0.5 )
				{{
					float fHdBgN = 8.0 - ( {bg_n} );
					float4 vHdBg = fHdBgN > 0.5 ? ( {bg_sum} ) / fHdBgN : tex2Dlod0( {sampler}, float2( 0.5, 1.5 ) * vHdUnit );
					float3 vHdByte = floor( vHdTzz.rgb * 255.0 + 0.5 );
					float3 vHdHi = floor( vHdByte / 16.0 );
					float3 vHdLo = vHdByte - 16.0 * vHdHi;
					float3 vHdQ = floor( vHdLo / 2.0 );
					float4 vHdCov = float4( vHdQ, dot( vHdLo - 2.0 * vHdQ, float3( 1.0, 2.0, 4.0 ) ) ) / 7.0;
					float2 vHdRow = lerp( vHdCov.xz, vHdCov.yw, vHdSub.x );
					float fHdCov = lerp( vHdRow.x, vHdRow.y, vHdSub.y );
					vHdRGB = lerp( vHdBg.rgb, vHdHi / 15.0, fHdCov );
					OutColor.a = lerp( vHdBg.a, 1.0, fHdCov );
				}}
				else if ( fHdText > 0.5 )
				{{
					float2 vHdW = abs( vHdPos - vHdCell - 0.5 );
					bool bHdR = vHdSub.x > 0.5;
					bool bHdD = vHdSub.y > 0.5;
					float4 vHdX = lerp( bHdR ? vHdTpz : vHdTmz, vHdTzz, bHdR ? fHdPpz : fHdPmz );
					float4 vHdY = lerp( bHdD ? vHdTzp : vHdTzm, vHdTzz, bHdD ? fHdPzp : fHdPzm );
					float4 vHdXY = bHdR ? ( bHdD ? vHdTpp : vHdTpm ) : ( bHdD ? vHdTmp : vHdTmm );
					vHdXY = lerp( vHdXY, vHdTzz, bHdR ? ( bHdD ? fHdPpp : fHdPpm ) : ( bHdD ? fHdPmp : fHdPmm ) );
					float4 vHdLin = lerp( lerp( vHdTzz, vHdX, vHdW.x ), lerp( vHdY, vHdXY, vHdW.x ), vHdW.y );
					vHdRGB = vHdLin.rgb;
					OutColor.a = vHdLin.a;
				}}
			}}'''


TEXT_VS_STRUCT = ('\tfloat4  vColor\t  : TEXCOORD1;\n};',
                  '\tfloat4  vColor\t  : TEXCOORD1;\n\tfloat4  vHdUI\t  : TEXCOORD2;\n\tfloat2  vHdClip  : TEXCOORD3;\n};')
TEXT_VS = '\t[[\n\t\tVS_OUTPUT main(const VS_INPUT v )\n\t\t{\n\t\t    VS_OUTPUT Out;'
TEXT_VS_3D = ('MainCode VertexShaderText3D\n' + TEXT_VS,
              'MainCode VertexShaderText3D\n' + TEXT_VS + '''
			Out.vHdUI = float4( 0.0, 0.0, 0.0, 0.0 );
			Out.vHdClip = float2( 0.0, 0.0 );''')
TEXT_VS_2D = ('MainCode VertexShaderText\n' + TEXT_VS,
              'MainCode VertexShaderText\n' + TEXT_VS + '''
			// UI coordinates, and the width of what is drawn into as 2 / that.
			Out.vHdUI = float4( v.vPosition.xy, abs( Matrix[0][0] ), 0.0 );
			Out.vHdClip = mul( Matrix, v.vPosition ).xy;''')

TEXT_ORIG = '''float4 OutColor = tex2D( SimpleTexture, v.vTexCoord );
			OutColor = OutColor * v.vColor;
		    return OutColor;'''
TEXT_PATCH = f'''// Font atlases from the HiDPI Text mod are stored at twice the resolution their .fnt describes
			// and carry red, alpha 1/255 corner texels (hidpi/fontgen.py). The engine derives glyph UVs from
			// the real texture size, so for those textures the UVs come out halved.
			float4 vHdTag = tex2Dlod0( SimpleTexture, float2( 0.0, 0.0 ) );
			// The CJK pages may be DXT5-compressed to stay within the game's texture
			// memory limits. Compression can slightly change the marker alpha, so
			// identify the full red/green/blue marker signature instead of requiring
			// an exact 1/255 alpha value.
			bool bHdFont = vHdTag.r > 0.99 && vHdTag.g > 0.10 && vHdTag.g < 0.65 &&
				vHdTag.b > 0.99 && vHdTag.a > 0.0001 && vHdTag.a < 0.99;
			float2 vHdUV = v.vTexCoord * ( bHdFont ? 2.0 : 1.0 );
			// Drawing into a 1x render target (see BEACON in build_mod.py): one UI unit per pixel, and
			// narrower than any screen.
			bool bHdTarget = abs( ddx( v.vHdUI.x ) ) > 0.75 && v.vHdUI.z > 2.0 / 1100.0;
			float2 vHdEdge = float2( v.vHdClip.x + 1.0, 1.0 - v.vHdClip.y ) / abs( float2( ddx( v.vHdClip.x ), ddy( v.vHdClip.y ) ) );
			float2 vHdQuarter = 0.25 * float2( ddx( vHdUV ).x, ddy( vHdUV ).y );
#ifdef PDX_DIRECTX_9
			// At UI scale 2 the DX9 GUI is half a screen pixel off D3D9's texel/pixel alignment, so 1:1
			// glyphs get averaged with their neighbours and 2x magnified art comes out unevenly jagged.
			// Shift sampling back onto texel centres. Render targets are aligned already.
			float fHdTexelsPerPixel = bHdFont ? ddx( vHdUV ).x * exp2( floor( vHdTag.g * 32.0 + 0.5 ) ) : 1.0;
			vHdUV += bHdTarget ? 0.0 : saturate( 2.0 - fHdTexelsPerPixel ) * 0.5 * float2( ddx( vHdUV ).x, ddy( vHdUV ).y );
#endif
			// Keep 2x font atlases on their full-resolution mip level. The game-generated
			// mip chain can otherwise soften glyphs when derivatives select a lower level.
		    float4 OutColor = bHdFont ? tex2Dlod0( SimpleTexture, vHdUV ) : tex2D( SimpleTexture, vHdUV );
			float4 vHdPlain = OutColor * v.vColor;
			// Other textures through here are magnified UI art, and on DX11 the render targets.
			{rcas('SimpleTexture')}
			float3 vHdRGB = vHdSharp;
			{target_text('SimpleTexture')}
			OutColor.rgb = bHdFont ? OutColor.rgb : vHdRGB;
			OutColor = OutColor * v.vColor;
			if ( bHdTarget )
			{{
				OutColor = vHdPlain;
				OutColor.a = min( OutColor.a, 254.0 / 255.0 );
				// Dark text is the engine's drop shadow pass, drawn before the text itself; it stays a plain
				// 1x shadow.
				float3 vHdLuma = float3( 0.299, 0.587, 0.114 );
				if ( bHdFont && vHdTag.b > 0.0 && dot( v.vColor.rgb, vHdLuma ) > 0.1 )
				{{
					// Coverage of the glyph fill (no shadow) in the four atlas texels under this pixel, 3 bits
					// each, plus the colour of the fill at 4 bits per channel.
					float4 vHd00 = tex2Dlod0( SimpleTexture, vHdUV - vHdQuarter );
					float4 vHd10 = tex2Dlod0( SimpleTexture, vHdUV + float2( vHdQuarter.x, -vHdQuarter.y ) );
					float4 vHd01 = tex2Dlod0( SimpleTexture, vHdUV + float2( -vHdQuarter.x, vHdQuarter.y ) );
					float4 vHd11 = tex2Dlod0( SimpleTexture, vHdUV + vHdQuarter );
					float4 vHdFill = float4( dot( vHd00.rgb, vHdLuma ) * vHd00.a, dot( vHd10.rgb, vHdLuma ) * vHd10.a,
					                         dot( vHd01.rgb, vHdLuma ) * vHd01.a, dot( vHd11.rgb, vHdLuma ) * vHd11.a );
					float4 vHdQ = floor( saturate( vHdFill * v.vColor.a / vHdTag.b ) * 7.0 + 0.5 );
					float3 vHdCol = floor( saturate( v.vColor.rgb * vHdTag.b ) * 15.0 + 0.5 );
					float3 vHdBits = float3( fmod( vHdQ.w, 2.0 ), fmod( floor( vHdQ.w / 2.0 ), 2.0 ), floor( vHdQ.w / 4.0 ) );
					// Fully transparent where there is no fill, which leaves the target untouched.
					OutColor = max( max( vHdQ.x, vHdQ.y ), max( vHdQ.z, vHdQ.w ) ) > 0.5
						? float4( ( vHdCol * 16.0 + vHdQ.xyz * 2.0 + vHdBits ) / 255.0, 1.0 ) : float4( 0.0, 0.0, 0.0, 0.0 );
				}}
				if ( !bHdFont && vHdEdge.x < 0.75 && vHdEdge.y >= 0.75 && vHdEdge.y < 1.75 )
					OutColor = float4( ( tex2Dlod0( SimpleTexture, float2( 0.5, 0.5 ) ) * v.vColor ).rgb, 254.0 / 255.0 );
				if ( vHdEdge.x < 0.75 && vHdEdge.y < 0.75 )
					OutColor = {BEACON};
			}}
		    return OutColor;'''

SPRITE_VS_STRUCT = ('\tfloat2  vTexCoord : TEXCOORD0;', '\tfloat2  vTexCoord : TEXCOORD0;\n\tfloat2  vHdUI : TEXCOORD3;')
SPRITE_VS = ('Out.vPosition  = mul( WorldViewProjectionMatrix, float4( v.vPosition.xyz, 1 ) );',
             'Out.vPosition  = mul( WorldViewProjectionMatrix, float4( v.vPosition.xyz, 1 ) );\n\t\t\tOut.vHdUI = v.vPosition.xy;')
SPRITE_ORIG = 'float4 OutColor = tex2D( MapTexture, v.vTexCoord );'
SPRITE_PATCH = f'''float2 vHdUV = v.vTexCoord;
{DX9_ALIGN}
			float4 OutColor = tex2D( MapTexture, vHdUV );
			// UI art, and on DX9 the render targets, is magnified 2x: sharpen it.
			{rcas('MapTexture')}
			float3 vHdRGB = vHdSharp;
			{target_text('MapTexture')}
			OutColor.rgb = vHdRGB;'''

# (original, replacement[, count]) in order; count defaults to the first occurrence, -1 means all.
SHADER_PATCHES = {
    'text.shader': [TEXT_VS_STRUCT, TEXT_VS_3D, TEXT_VS_2D, (TEXT_ORIG, TEXT_PATCH)],
    **{name: [SPRITE_VS_STRUCT, SPRITE_VS, (SPRITE_ORIG, SPRITE_PATCH, -1)] for name in (
        'buttonstate.shader', 'buttonstate_nodowneffect.shader', 'buttonstate_onlydisable.shader',
        'buttonstate_nodownordisableeffect.shader', 'buttonstate_linear.shader',
        'buttonstate_nontransparent.shader', 'buttonstate_fade_frames_to_black.shader')},
}


def _font_base(name):
    return Path(name).name


def _skip_font(name):
    base = _font_base(name)
    return base in SKIP or base.startswith('hoi_mapfont4_') or base.startswith('hoi_arrow_font_')


def referenced_fonts(game, include_cjk=False, cjk_groups=None):
    text = ''.join(p.read_text(encoding='utf-8', errors='replace') for p in (game / 'interface').glob('*.gfx'))
    text = re.sub(r'#[^\n]*', '', text)
    names = set(re.findall(r'"gfx/fonts/([A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)?)"', text))
    result = []
    for name in names:
        if _skip_font(name):
            continue
        if '/' in name:
            language, _ = name.split('/', 1)
            if language in LANGUAGE_DIRS and include_cjk and (
                    cjk_groups is None or _font_base(name).rsplit('_', 1)[0] in cjk_groups or
                    any(_font_base(name).startswith(group + '_') for group in cjk_groups)):
                result.append(name)
            continue
        result.append(name)
    return sorted(result)


def font_file(pattern, cache):
    """Outline font file for an OUTLINES entry: a fontconfig pattern, or a bundled variable font and
    weight, which is instanced into `cache`."""
    if isinstance(pattern, tuple):
        file, weight = pattern
        path = cache / f'{file.split("[")[0]}-{weight}.ttf'
        if not path.exists():
            cache.mkdir(parents=True, exist_ok=True)
            instantiateVariableFont(TTFont(FONTS / file), {'wght': weight}).save(path)
        return str(path)
    try:
        path = subprocess.run(['fc-match', '-f', '%{file}', pattern], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return None
    family = pattern.split(':')[0].lower().replace(' ', '')
    return path if path and family in Path(path).stem.lower().replace(' ', '').replace('-', '') else None


def texture_path(fonts_dir, name):
    for ext in ('.dds', '.tga'):
        if (fonts_dir / (name + ext)).exists():
            return fonts_dir / (name + ext)
    raise FileNotFoundError(name)


def compress_cjk_dds(path):
    """Use the same DXT5 format as vanilla Chinese font atlases."""
    magick = shutil.which('magick')
    if not magick:
        raise RuntimeError('ImageMagick (magick) is required to write CJK DDS atlases')
    compressed = path.with_name(path.stem + '.dxt5.dds')
    source, target = str(path), str(compressed)
    if os.name == 'posix' and Path(magick).resolve().suffix.lower() == '.exe':
        source = subprocess.run(['wslpath', '-w', source], capture_output=True, text=True, check=True).stdout.strip()
        target = subprocess.run(['wslpath', '-w', target], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run([magick, source, '-define', 'dds:compression=dxt5', target], check=True)
    compressed.replace(path)


def has_effects(tex):
    a = tex[..., 3]
    dark = (a * (luminance(tex) < 0.35)).sum() / max(a.sum(), 1e-6)
    return bool(0.15 < dark < 0.9)


def convert(args):
    name, fonts_dir, out_dir, outline, cjk_font, cjk_dds_format = args
    fnt = read_fnt(fonts_dir / (name + '.fnt'))
    tex = load_rgba(texture_path(fonts_dir, name))
    spec = FontSpec(effects=has_effects(tex))
    if name.startswith('chinese/'):
        # Chinese glyphs already have a complete bitmap inventory in the game
        # .fnt file.  Redraw those codepoints from a CJK outline font while
        # keeping the original 1x metrics so UI layout stays unchanged.
        spec.ttf = cjk_font
        spec.em = abs(float(fnt.info.get('size', 16)))
        spec.substitute = True
        # Detect source effects for alignment and missing-outline glyphs, but
        # output only the fill. Keeping the old alpha while whitening its RGB
        # would turn the baked shadow into a bright halo.
        spec.solid = True
    elif outline:
        spec.ttf, spec.em = outline[0], outline[1]
        spec.embolden = outline[2] if len(outline) > 2 else 0.0
        base = _font_base(name)
        spec.substitute = isinstance(OUTLINES[base][0], tuple)
        if base in REDRAWN:
            spec.redraw = True
            spec.fill = REDRAWN[base]['fill']
            spec.shadow = REDRAWN[base]['shadow']
    new_fnt, atlas, counts, (w, h) = build_atlas(fnt, tex, spec)
    target = out_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    pages = split_atlas(new_fnt, atlas) if name.startswith('chinese/') else [
        (new_fnt, atlas, w, h)
    ]
    for index, (page_fnt, page_atlas, page_w, page_h) in enumerate(pages):
        suffix = '' if len(pages) == 1 else f'_p{index + 1}'
        page_target = out_dir / f'{name}{suffix}'
        page_name = page_target.name + '.dds'
        write_fnt(page_target.with_suffix('.fnt'), page_fnt, page_name, page_w, page_h)
        save_dds_rgba(page_target.with_suffix('.dds'), page_atlas)
        if name.startswith('chinese/') and cjk_dds_format == 'dxt5':
            compress_cjk_dds(page_target.with_suffix('.dds'))
        if len(pages) == 1 and (fonts_dir / (name + '.tga')).exists():
            px = np.clip(np.rint(page_atlas * 255), 0, 255).astype(np.uint8)
            Image.fromarray(px, 'RGBA').save(page_target.with_suffix('.tga'), orientation=1)
    return name, counts, spec


def write_patched(game, mod_root):
    patches = {f'gfx/FX/{name}': patch for name, patch in SHADER_PATCHES.items()}
    for rel, pairs in patches.items():
        src = (game / rel).read_text(encoding='utf-8', errors='surrogateescape')
        for orig, patch, *count in pairs:
            if orig not in src:
                sys.exit(f'{rel} has changed in this game version; update SHADER_PATCHES')
            src = src.replace(orig, patch, *(count or [1]))
        dest = mod_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src, encoding='utf-8', errors='surrogateescape')


def write_cjk_interface_patches(game, mod_root, groups, pages_root=None):
    """Point selected Chinese bitmap-font groups at their split atlas pages."""
    pages_dir = Path(pages_root) if pages_root else mod_root / 'gfx/fonts/chinese'
    replacements = {
        'chatfonts.gfx': {'standard'},
        'core_chinese.gfx': {
            'garamond_14', 'garamond_14_bold', 'garamond_16', 'garamond_16_bold',
            'cg_18b', 'hoi_16mbs', 'hoi_18', 'hoi_18mbs', 'hoi_20b', 'hoi_20bs',
            'hoi_24header', 'hoi_30', 'hoi_30header', 'hoi_33', 'hoi_36header',
            'vic_18', 'vic_22', 'vic_36', 'hoi_16typewriter', 'hoi4_typewriter22',
            'hoi4_typewriter22_inverted'
        },
    }
    for filename, names in replacements.items():
        dest = mod_root / 'interface' / filename
        if not names & groups:
            continue
        source = dest if dest.exists() else game / 'interface' / filename
        text = source.read_text(encoding='utf-8', errors='surrogateescape')
        for group in names & groups:
            for page in range(1, 5):
                for extension in ('', 'EX'):
                    old = f'"gfx/fonts/chinese/{group}_{page}of4{extension}"'
                    prefix = f'{group}_{page}of4{extension}_p'
                    pages = sorted(pages_dir.glob(prefix + '*.fnt'))
                    if not pages:
                        continue
                    new = '\n'.join(
                        f'\t\t\t"gfx/fonts/chinese/{path.stem}"' for path in pages
                    )
                    if old in text:
                        text = text.replace(old, new)
                    else:
                        existing_pages = re.compile(
                            r'(?:\s*"gfx/fonts/chinese/' + re.escape(prefix) + r'\d+")+'
                        )
                        text = existing_pages.sub(new, text)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding='utf-8', errors='surrogateescape')


def write_country_filter_patch(game, mod_root):
    """Use the slightly larger CJK menu font for the continent filter buttons.

    At 4K/UI scale 2 the 16px CJK glyphs are sampled at a fractional size and look
    softer than the 20px country names. The filter cells have ample width for the
    existing 18px bold font, so this local GUI override improves legibility without
    changing the rest of the setup screen.
    """
    source = game / 'interface/frontendgamesetupview.gui'
    text = source.read_text(encoding='utf-8', errors='surrogateescape')
    pattern = (r'(name\s*=\s*"country_filter".*?buttonType\s*=\s*\{.*?'
               r'buttonText\s*=\s*"FILTER"\s+buttonFont\s*=\s*)"hoi_16mbs"')
    patched, count = re.subn(pattern, r'\1"hoi_18mbs"', text, count=1, flags=re.S)
    if count != 1:
        sys.exit('frontendgamesetupview.gui country_filter block not found')
    dest = mod_root / 'interface/frontendgamesetupview.gui'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patched, encoding='utf-8', errors='surrogateescape')


def write_national_focus_title_patch(game, mod_root):
    """Use the larger CJK font for the two national-focus node titles.

    Only the continuous and regular focus-node title textboxes are changed;
    costs, search fields, tooltips, and rewards keep their original metrics.
    """
    source = game / 'interface/nationalfocusview.gui'
    text = source.read_text(encoding='utf-8', errors='surrogateescape')
    pattern = (r'(instantTextboxType\s*=\s*\{\s*'
               r'name\s*=\s*"name"\s*'
               r'position\s*=\s*\{\s*x\s*=\s*15\s*y\s*=\s*58\s*\}\s*'
               r'font\s*=\s*)"hoi_16mbs"')
    patched, count = re.subn(pattern, r'\1"hoi_18mbs"', text, count=2, flags=re.S)
    if count != 2:
        sys.exit('nationalfocusview.gui focus node title blocks not found')
    dest = mod_root / 'interface/nationalfocusview.gui'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patched, encoding='utf-8', errors='surrogateescape')


def write_gamesetup_details_patch(game, mod_root):
    """Use the sharper regular 20px CJK font in the country details panel."""
    dest = mod_root / 'interface/frontendgamesetupview.gui'
    source = dest if dest.exists() else game / 'interface/frontendgamesetupview.gui'
    text = source.read_text(encoding='utf-8', errors='surrogateescape')
    section_re = (r'(name\s*=\s*"gamesetup_country_details".*?'
                  r')(?=\n\s*containerWindowType\s*=\s*\{\s*\n?\s*'
                  r'name\s*=\s*"gamesetup_multiplayer_settings_window")')
    match = re.search(section_re, text, flags=re.S)
    if not match:
        sys.exit('frontendgamesetupview.gui country details block not found')
    section = match.group(1)
    patched_section, count = re.subn(r'(?<![A-Za-z0-9_])hoi_20bs(?![A-Za-z0-9_])', 'hoi_20b', section)
    if count == 0:
        sys.exit('frontendgamesetupview.gui country details font not found')
    patched = text[:match.start(1)] + patched_section + text[match.end(1):]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patched, encoding='utf-8', errors='surrogateescape')


def write_gamesetup_difficulty_patch(game, mod_root):
    """Use the sharper regular font for the lower-right difficulty heading."""
    dest = mod_root / 'interface/frontendgamesetupview.gui'
    source = dest if dest.exists() else game / 'interface/frontendgamesetupview.gui'
    text = source.read_text(encoding='utf-8', errors='surrogateescape')
    pattern = (r'(name\s*=\s*"difficulty_text".*?font\s*=\s*)"hoi_24header"')
    patched, count = re.subn(pattern, r'\1"hoi_20b"', text, count=1, flags=re.S)
    if count != 1:
        sys.exit('frontendgamesetupview.gui difficulty heading not found')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patched, encoding='utf-8', errors='surrogateescape')


def write_font_replacements_patch(game, mod_root, relative_path, replacements):
    """Apply exact font-name replacements to one vanilla GUI file."""
    dest = mod_root / relative_path
    source = dest if dest.exists() else game / relative_path
    text = source.read_text(encoding='utf-8', errors='surrogateescape')
    pattern = re.compile(r'(?P<key>font|buttonFont)(?P<gap>\s*=\s*)"(?P<name>'
                         + '|'.join(re.escape(name) for name in replacements)
                         + r')"')
    def replace(match):
        return (match.group('key') + match.group('gap') + '"' +
                replacements[match.group('name')] + '"')
    patched, count = pattern.subn(replace, text)
    if count == 0:
        sys.exit(f'{relative_path} has no target font references')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patched, encoding='utf-8', errors='surrogateescape')


def write_credits(mod_root, used, external_fonts=(), cjk_license=None):
    """CREDITS.txt and licence texts for the outline fonts in `used` ({font file: [game font names]})."""
    lines = [
        f'{MOD_NAME} for Hearts of Iron IV',
        REPO_URL,
        'Based on Chris Spoelstra / cspoelstra: ' + UPSTREAM_URL,
        '',
        'The font textures (gfx/fonts) are redrawings of Hearts of Iron IV\'s fonts at twice the resolution,',
        'and the shaders (gfx/FX) are modified copies of the game\'s own. The game\'s assets remain the',
        'property of Paradox Interactive. The build script that generates this mod is MIT licensed.',
        '',
        'Font textures drawn from the outline fonts below are distributed under the licence of the font',
        'they were drawn from; the licence texts are in the licenses folder. Their source is that font plus',
        'the build script in the repository above. The other font textures are traced from the game\'s own',
        'bitmaps.',
    ]
    (mod_root / 'licenses').mkdir()
    for path, names in sorted(used.items(), key=lambda kv: kv[0]):
        info = TTFont(path, lazy=True)['name']
        family = info.getDebugName(16) or info.getDebugName(1)
        if family not in FONT_LICENSES:
            sys.exit(f'{family} ({path}) has no entry in FONT_LICENSES')
        for licence in FONT_LICENSES[family]:
            shutil.copy(LICENSES / licence, mod_root / 'licenses' / licence)
        lines += ['', f'{family} ({Path(path).name})', '  ' + ' '.join(info.getDebugName(0).split()),
                  '  Licence: ' + ', '.join(FONT_LICENSES[family]), '  Used for: ' + ', '.join(sorted(names))]
    for path in external_fonts:
        with TTFont(path, lazy=True) as font:
            info = font['name']
            family = info.getDebugName(16) or info.getDebugName(1)
            copyright_text = ' '.join((info.getDebugName(0) or '').split())
        license_path = cjk_license
        if license_path is None and family and family.startswith('Noto Sans SC'):
            license_path = LICENSES / 'NotoSansSC-OFL.txt'
        lines += ['', f'CJK outline source: {family} ({Path(path).name})',
                  '  ' + copyright_text]
        if license_path:
            shutil.copy(license_path, mod_root / 'licenses' / 'CJK-font-license.txt')
            lines += ['  Licence: licenses/CJK-font-license.txt']
        else:
            lines += ['  Supply --cjk-font-license before redistributing this font.']
    (mod_root / 'CREDITS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_descriptors(mod_root, version, path_value, remote_file_id=None):
    body = (f'version="1.0"\nname="{MOD_NAME}"\ntags={{\n\t"Graphics"\n\t"Utilities"\n}}\n'
            f'picture="thumbnail.png"\nsupported_version="{version}"\n')
    if remote_file_id:
        body += f'remote_file_id="{remote_file_id}"\n'
    (mod_root / 'descriptor.mod').write_text(body, encoding='utf-8')
    return body + f'path="{path_value}"\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', type=Path, default=DEFAULT_GAME, help='HOI4 install directory')
    ap.add_argument('--out', type=Path, default=Path(__file__).parent / 'dist',
                    help='where to build the mod (default: ./dist)')
    ap.add_argument('--install', type=Path, nargs='?', const=PROTON_DOCS, default=None,
                    help='copy the built mod into this HOI4 user directory (default: the Proton prefix)')
    ap.add_argument('--jobs', type=int, default=os.cpu_count())
    ap.add_argument('--cjk-font', type=Path,
                    help='CJK outline font used for gfx/fonts/chinese (also enables Chinese font output)')
    ap.add_argument('--cjk-weight', type=float, default=500,
                    help='wght axis used when the CJK font is variable (default: 500)')
    ap.add_argument('--cjk-dds-format', choices=('dxt5', 'raw'), default='dxt5',
                    help='CJK atlas texture format; raw avoids block compression (default: dxt5)')
    ap.add_argument('--cjk-groups',
                    help='comma-separated Chinese font groups to build, e.g. standard,hoi_18 (default: all)')
    ap.add_argument('--cjk-font-license', type=Path,
                    help='licence text for a custom CJK font; Noto Sans SC OFL is included automatically')
    ap.add_argument('--mod-name', help='launcher title (defaults to the Chinese title with --cjk-font)')
    ap.add_argument('--mod-dir', help='mod folder name (defaults to hoi4_hidpi_schinese with --cjk-font)')
    args = ap.parse_args()
    global MOD_NAME, MOD_DIR
    MOD_NAME = args.mod_name or ('钢铁雄心 IV｜4K 高清中文字体' if args.cjk_font else MOD_NAME)
    MOD_DIR = args.mod_dir or ('hoi4_hidpi_schinese' if args.cjk_font else MOD_DIR)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', MOD_DIR):
        ap.error('--mod-dir must be a single folder name using letters, digits, underscores or hyphens')
    if any(c in MOD_NAME for c in ('"', '\n', '\r')):
        ap.error('--mod-name cannot contain quotes or newlines')
    if args.cjk_font_license and not args.cjk_font_license.is_file():
        ap.error('--cjk-font-license must be an existing file')

    if args.cjk_font:
        cjk_tt = TTFont(args.cjk_font)
        axes = {axis.axisTag: axis for axis in cjk_tt['fvar'].axes} if 'fvar' in cjk_tt else {}
        cjk_tt.close()
        if 'wght' in axes:
            axis = axes['wght']
            weight = min(max(args.cjk_weight, axis.minValue), axis.maxValue)
            if weight != args.cjk_weight:
                print(f'warning: CJK weight {args.cjk_weight:g} is outside the font axis; using {weight:g}')
            args.out.joinpath('fonts').mkdir(parents=True, exist_ok=True)
            cjk_instance = args.out / 'fonts' / f'{args.cjk_font.stem}-wght{weight:g}.ttf'
            instantiateVariableFont(TTFont(args.cjk_font), {'wght': weight}).save(cjk_instance)
            args.cjk_font = cjk_instance

    fonts_dir = args.game / 'gfx/fonts'
    mod_root = args.out / MOD_DIR
    out_fonts = mod_root / 'gfx/fonts'
    if mod_root.exists():
        shutil.rmtree(mod_root)
    out_fonts.mkdir(parents=True)

    outlines = {}
    for name, (pattern, *rest) in OUTLINES.items():
        path = font_file(pattern, args.out / 'fonts')
        if path:
            outlines[name] = (path, *rest)
        else:
            print(f'warning: {pattern} not installed, {name} will be traced from its bitmap')

    cjk_groups = ({x.strip() for x in args.cjk_groups.split(',') if x.strip()}
                  if args.cjk_groups else None)
    names = referenced_fonts(args.game, include_cjk=args.cjk_font is not None, cjk_groups=cjk_groups)
    jobs = [(n, fonts_dir, out_fonts, outlines.get(_font_base(n)),
             str(args.cjk_font) if n.startswith('chinese/') else None, args.cjk_dds_format) for n in names]
    used = {}
    with ProcessPoolExecutor(args.jobs) as pool:
        for name, counts, spec in pool.map(convert, jobs):
            src = Path(spec.ttf).name if spec.ttf else 'bitmap'
            fx = ' +effects' if spec.effects else ''
            print(f'{name:36s} {src:28s}{fx:9s} ' + ' '.join(f'{k}={v}' for k, v in sorted(counts.items())))
            if counts.get('ttf') and not name.startswith('chinese/'):
                used.setdefault(spec.ttf, []).append(name)

    write_patched(args.game, mod_root)
    if args.cjk_font:
        built_groups = {re.sub(r'_\d+of4(?:EX)?$', '', _font_base(name))
                        for name in names if name.startswith('chinese/')}
        write_cjk_interface_patches(args.game, mod_root, built_groups)
        write_country_filter_patch(args.game, mod_root)
        write_gamesetup_details_patch(args.game, mod_root)
        write_gamesetup_difficulty_patch(args.game, mod_root)
        write_national_focus_title_patch(args.game, mod_root)
        write_font_replacements_patch(
            args.game, mod_root, 'interface/countrydecisionview.gui',
            {'hoi_16mbs': 'hoi_18mbs'})
        write_font_replacements_patch(
            args.game, mod_root, 'interface/countrytradeview.gui',
            {'hoi_16mbs': 'hoi_18mbs', 'hoi_18mbs': 'hoi_20bs'})
        write_font_replacements_patch(
            args.game, mod_root, 'interface/eventwindow.gui',
            {'hoi4_typewriter16': 'hoi_18mbs'})
    write_credits(mod_root, used, [str(args.cjk_font)] if args.cjk_font else (), args.cjk_font_license)
    workshop = REPO / 'workshop' / 'schinese' if args.cjk_font else REPO / 'workshop'
    shutil.copy(workshop / 'thumbnail.png', mod_root / 'thumbnail.png')
    raw = re.search(r'"rawVersion":\s*"(\d+\.\d+)',
                    (args.game / 'launcher-settings.json').read_text(encoding='utf-8', errors='replace'))
    version = f'{raw.group(1)}.*' if raw else '1.*'
    write_descriptors(mod_root, version, '')
    print(f'built {mod_root}')

    if args.install:
        user_dir = args.install
        dest = user_dir / 'mod' / MOD_DIR
        # Keep the Workshop item id the Paradox launcher adds on upload, so later uploads update it.
        mod_file = user_dir / 'mod' / f'{MOD_DIR}.mod'
        remote = re.search(r'remote_file_id="(\d+)"', mod_file.read_text(encoding='utf-8')) if mod_file.exists() else None
        if not remote and (dest / 'descriptor.mod').exists():
            remote = re.search(r'remote_file_id="(\d+)"',
                               (dest / 'descriptor.mod').read_text(encoding='utf-8'))
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(mod_root, dest)
        path_value = f'{PROTON_DOCS_WIN}/mod/{MOD_DIR}' if user_dir == PROTON_DOCS else dest.as_posix()
        mod_file.write_text(write_descriptors(dest, version, path_value, remote and remote.group(1)),
                            encoding='utf-8')
        print(f'installed {dest}')


if __name__ == '__main__':
    main()
