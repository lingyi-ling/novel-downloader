package com.lingyi.noveldl.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val WarmCoral = Color(0xFFD97757)
private val WarmCoralDark = Color(0xFFE0895F)
private val Paper = Color(0xFFF5F4EF)
private val Ink = Color(0xFF1F1E1B)
private val Charcoal = Color(0xFF1B1A17)
private val CharcoalSurface = Color(0xFF23221F)

private val LightColors = lightColorScheme(
    primary = WarmCoral,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFF4E3DA),
    onPrimaryContainer = Color(0xFF4A1F10),
    secondary = Color(0xFF6B675E),
    background = Paper,
    onBackground = Ink,
    surface = Color.White,
    onSurface = Ink,
    surfaceVariant = Color(0xFFFAF9F5),
    onSurfaceVariant = Color(0xFF6B675E),
    outline = Color(0xFFD8D4C6),
    error = Color(0xFFB4472F),
)

private val DarkColors = darkColorScheme(
    primary = WarmCoralDark,
    onPrimary = Charcoal,
    primaryContainer = Color(0xFF4A2A1C),
    onPrimaryContainer = Color(0xFFF4D9CB),
    secondary = Color(0xFFA8A399),
    background = Charcoal,
    onBackground = Color(0xFFEDEBE6),
    surface = CharcoalSurface,
    onSurface = Color(0xFFEDEBE6),
    surfaceVariant = Color(0xFF2A2925),
    onSurfaceVariant = Color(0xFFA8A399),
    outline = Color(0xFF3A3833),
    error = Color(0xFFD07A63),
)

@Composable
fun NovelTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}
