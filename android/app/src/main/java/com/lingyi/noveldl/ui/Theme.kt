package com.lingyi.noveldl.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// The two brand colours, shared with the desktop UI: navy carries the
// structure (titles, secondary surfaces) and warm orange carries actions.
private val Navy = Color(0xFF1E3A5F)
private val NavyLight = Color(0xFF9DB8D6)
private val Orange = Color(0xFFC2410C)
private val OrangeDark = Color(0xFFE8835F)
private val Slate = Color(0xFFF8FAFC)
private val Ink = Color(0xFF0F172A)
private val NavyInk = Color(0xFF0B1220)
private val NavySurface = Color(0xFF111C2E)

private val LightColors = lightColorScheme(
    primary = Orange,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFFFE3D6),
    onPrimaryContainer = Color(0xFF7A2E0E),
    secondary = Navy,
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFDCE7F5),
    onSecondaryContainer = Color(0xFF16304F),
    tertiary = Navy,
    onTertiary = Color.White,
    background = Slate,
    onBackground = Ink,
    surface = Color.White,
    onSurface = Ink,
    surfaceVariant = Color(0xFFF1F5F9),
    onSurfaceVariant = Color(0xFF475569),
    outline = Color(0xFFCBD5E1),
    outlineVariant = Color(0xFFE2E8F0),
    error = Color(0xFFDC2626),
    onError = Color.White,
)

private val DarkColors = darkColorScheme(
    primary = OrangeDark,
    onPrimary = Color(0xFF1A1208),
    primaryContainer = Color(0xFF5A2A16),
    onPrimaryContainer = Color(0xFFFFD9C7),
    secondary = NavyLight,
    onSecondary = NavyInk,
    secondaryContainer = Color(0xFF243B57),
    onSecondaryContainer = Color(0xFFD6E4F4),
    tertiary = NavyLight,
    onTertiary = NavyInk,
    background = NavyInk,
    onBackground = Color(0xFFE8EEF7),
    surface = NavySurface,
    onSurface = Color(0xFFE8EEF7),
    surfaceVariant = Color(0xFF1A2942),
    onSurfaceVariant = Color(0xFFA9B7CC),
    outline = Color(0xFF33456A),
    outlineVariant = Color(0xFF22304A),
    error = Color(0xFFF87171),
    onError = Color(0xFF1A1208),
)

@Composable
fun NovelTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}
