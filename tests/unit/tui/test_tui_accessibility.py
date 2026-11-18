"""WCAG 2.1 AA and High-Contrast AAA verification tests for the iMednet TUI."""

from __future__ import annotations

import pytest

from imednet_tui.theme import (
    DARK_PALETTE,
    HIGH_CONTRAST_PALETTE,
    LIGHT_PALETTE,
    contrast_ratio,
    format_progress_bar,
    format_status_badge,
    get_active_palette,
    relative_luminance,
)


def test_relative_luminance_bounds_and_validation() -> None:
    """Verify WCAG relative luminance bounds for black/white and invalid hex error."""
    assert relative_luminance("#000000") == pytest.approx(0.0)
    assert relative_luminance("#FFFFFF") == pytest.approx(1.0)
    assert contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0)

    with pytest.raises(ValueError, match="6-digit hex color"):
        relative_luminance("#FFF")


@pytest.mark.parametrize("palette", [DARK_PALETTE, LIGHT_PALETTE], ids=["dark", "light"])
def test_wcag_aa_contrast_ratios_for_dark_and_light_themes(palette: object) -> None:
    """Verify every foreground token meets WCAG 2.1 Level AA (>= 4.5:1) against backgrounds."""
    p = DARK_PALETTE if palette is DARK_PALETTE else LIGHT_PALETTE
    foreground_tokens = {
        "text": p.text,
        "muted": p.muted,
        "accent": p.accent,
        "success": p.success,
        "warning": p.warning,
        "error": p.error,
    }
    for token_name, fg_hex in foreground_tokens.items():
        bg_ratio = contrast_ratio(fg_hex, p.background)
        surface_ratio = contrast_ratio(fg_hex, p.surface)
        assert bg_ratio >= 4.5, (
            f"{p.name}.{token_name} ({fg_hex}) vs background ({p.background}) "
            f"ratio {bg_ratio:.2f} < 4.5:1"
        )
        assert surface_ratio >= 4.5, (
            f"{p.name}.{token_name} ({fg_hex}) vs surface ({p.surface}) "
            f"ratio {surface_ratio:.2f} < 4.5:1"
        )


def test_wcag_aaa_contrast_ratios_for_high_contrast_theme() -> None:
    """Verify high-contrast mode meets >= 7.0:1 AAA contrast across all foreground tokens."""
    foreground_tokens = {
        "text": HIGH_CONTRAST_PALETTE.text,
        "muted": HIGH_CONTRAST_PALETTE.muted,
        "accent": HIGH_CONTRAST_PALETTE.accent,
        "success": HIGH_CONTRAST_PALETTE.success,
        "warning": HIGH_CONTRAST_PALETTE.warning,
        "error": HIGH_CONTRAST_PALETTE.error,
    }
    for token_name, fg_hex in foreground_tokens.items():
        bg_ratio = contrast_ratio(fg_hex, HIGH_CONTRAST_PALETTE.background)
        surface_ratio = contrast_ratio(fg_hex, HIGH_CONTRAST_PALETTE.surface)
        assert bg_ratio >= 7.0, (
            f"high-contrast.{token_name} ({fg_hex}) ratio {bg_ratio:.2f} < 7.0:1"
        )
        assert surface_ratio >= 7.0


def test_get_active_palette_and_formatters() -> None:
    """Verify palette selection, status badges, and progress bar formatting across modes."""
    assert get_active_palette(dark=True, high_contrast=False) == DARK_PALETTE
    assert get_active_palette(dark=False, high_contrast=False) == LIGHT_PALETTE
    assert get_active_palette(dark=False, high_contrast=True) == HIGH_CONTRAST_PALETTE

    bar_default = format_progress_bar(50, width=10, high_contrast=False)
    assert bar_default == "[█████░░░░░]  50%"

    bar_hc = format_progress_bar(75, width=8, high_contrast=True)
    assert bar_hc == "[######--]  75%"

    bar_clamped = format_progress_bar(150, width=6, high_contrast=True)
    assert bar_clamped == "[######] 100%"

    assert format_progress_bar(None, width=6, high_contrast=True) == "[------]   0%"

    badge_ok = format_status_badge("COMPLETED", dark=True, high_contrast=False)
    assert DARK_PALETTE.success in badge_ok
    assert "COMPLETED" in badge_ok

    badge_warn_light = format_status_badge("PROCESSING", dark=False, high_contrast=False)
    assert LIGHT_PALETTE.warning in badge_warn_light

    badge_err_hc = format_status_badge("FAILED", dark=True, high_contrast=True)
    assert HIGH_CONTRAST_PALETTE.error in badge_err_hc
