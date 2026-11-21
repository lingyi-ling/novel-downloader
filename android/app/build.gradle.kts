import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

// Release signing comes from android/keystore.properties (git-ignored):
//   storeFile=release.jks        # absolute, or relative to the android/ dir
//   storePassword=...
//   keyAlias=...
//   keyPassword=...
//
// There is deliberately no fallback to the debug key. A debug-signed APK is
// installable, reports the release versionName and cannot be upgraded by the
// real release key afterwards (Android refuses the signature change), so a
// silent fallback turns a missing file into a broken update channel. Release
// packaging therefore fails with an actionable message instead; `assembleDebug`
// and the JVM unit tests are unaffected.
val keystorePropertiesFile = rootProject.file("keystore.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.isFile) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}
val missingSigningKeys = listOf("storeFile", "storePassword", "keyAlias", "keyPassword")
    .filter { key -> keystoreProperties.getProperty(key).isNullOrBlank() }
val releaseStoreFile = keystoreProperties.getProperty("storeFile")
    ?.takeIf { it.isNotBlank() }
    ?.let { rootProject.file(it) }
val hasReleaseSigning = keystorePropertiesFile.isFile &&
    missingSigningKeys.isEmpty() &&
    releaseStoreFile?.isFile == true

// Escape hatch for local experiments only: produces an *unsigned* release APK
// instead of failing. Never used by scripts/release.py, which requires the
// keystore before it even invokes Gradle.
val allowUnsignedRelease =
    providers.gradleProperty("novelDownloader.allowUnsignedRelease").orNull == "true"

if (!hasReleaseSigning) {
    logger.warn(
        "keystore.properties is missing, incomplete or points at a missing " +
            "keystore; `assembleRelease` will fail. `assembleDebug` still works.",
    )
}

android {
    namespace = "com.lingyi.noveldl"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.lingyi.noveldl"
        minSdk = 26
        targetSdk = 35
        versionCode = 52
        versionName = "1.37.1"
        // Chaquopy requires explicit ABIs; arm64 covers modern phones, x86_64
        // keeps the emulator usable.
        ndk.abiFilters += listOf("arm64-v8a", "x86_64")
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = releaseStoreFile
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            // No debug fallback: without signing material the variant is left
            // unsigned and packaging fails in the task guard below.
            signingConfig = if (hasReleaseSigning) {
                signingConfigs.getByName("release")
            } else {
                null
            }
            // Minification stays off on purpose: Chaquopy embeds the Python
            // source tree, kotlinx.serialization and Compose both rely on
            // generated/shaken-but-referenced code, and R8 has no rules for
            // the Python bridge. isShrinkResources requires minify, so it is
            // implicitly false; the APK size is dominated by CPython anyway.
            isMinifyEnabled = false
            isShrinkResources = false
            // Explicit, not just the AGP default: a debuggable release APK
            // ships android:debuggable="true" (any installed app can attach a
            // debugger and read app-private data) and is refused by Play.
            isDebuggable = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

// Hard gate: a release APK must carry the release key. Checked against the
// task graph so `assembleDebug`, `testDebugUnitTest`, `--dry-run` and IDE sync
// still work on a machine without signing material.
gradle.taskGraph.whenReady {
    // Prefix match: ABI/density splits name their tasks `packageReleaseArm64-v8a`
    // and an app bundle uses `bundleRelease`, so an exact-name check would let
    // a future `splits` block bypass the guard.
    val releasePackaging = allTasks.any {
        it.name.startsWith("packageRelease") || it.name == "bundleRelease"
    }
    if (
        releasePackaging &&
        !hasReleaseSigning &&
        !allowUnsignedRelease &&
        !gradle.startParameter.isDryRun
    ) {
        val reason = when {
            !keystorePropertiesFile.isFile ->
                "android/keystore.properties does not exist"
            missingSigningKeys.isNotEmpty() ->
                "android/keystore.properties is missing: " + missingSigningKeys.joinToString()
            else -> "the storeFile in android/keystore.properties does not exist"
        }
        throw GradleException(
            "Refusing to build a release APK without the release signing key " +
                "($reason).\n" +
                "Create android/keystore.properties with storeFile/storePassword/" +
                "keyAlias/keyPassword, or run `assembleDebug` instead.\n" +
                "Local experiments only: add " +
                "-PnovelDownloader.allowUnsignedRelease=true to produce an " +
                "unsigned APK (not installable, never publishable).",
        )
    }
}

// Embedded Python engine (Chaquopy): runs the novel_downloader service layer
// on-device so the app works without a PC. The Python source tree is shared
// with the repository root (../../src), and the loopback HTTP server in
// apps.embedded.server speaks the same REST contract as novel-api.
chaquopy {
    defaultConfig {
        version = "3.12"
        // Prefer the repo venv as the build-time interpreter when present.
        rootProject.file("../.venv/Scripts/python.exe")
            .takeIf { it.exists() }
            ?.let { buildPython = listOf(it.absolutePath) }
        pip {
            install("aiohttp")
            install("lxml")
            install("cssselect")
            install("pycryptodome")
            install("rich")
            install("platformdirs")
            // Core TLS trust store (pyproject pins a security floor; requests
            // would pull it transitively, but listing it keeps this block the
            // complete core dependency set).
            install("certifi")
            // Optional music engine (search / download / tagging). All of
            // these ship Android-compatible wheels through Chaquopy.
            install("requests")
            install("mutagen")
            // Chaquopy's repository tops out at Pillow 11.0.0, so do not copy
            // pyproject's >=13 security floor here: the APK would fail to
            // resolve. See tests/test_android_deps.py for the tracked gap.
            install("Pillow")
            install("qrcode")
            install("websocket-client")
            install("m3u8")
            install("emoji")
            install("bleach")
            install("filetype")
            install("puremagic")
            install("fake-useragent")
            install("json-repair")
            install("tinytag")
            install("tabulate")
            install("prettytable")
            install("wcwidth")
            install("prompt_toolkit")
            // Direct imports of the vendored musicdl adapters (module level,
            // not lazy). Without them the 5sing / kuwo / qq / fivesing / ...
            // clients register but fail on the first search:
            //   pathvalidate       -> every source's file-name sanitizer
            //   beautifulsoup4     -> HTML scraping in 9 platform clients
            //   cryptography       -> AES/RSA/HKDF in qq/qobuz/netease/soda
            //   typing-extensions  -> Unpack annotations evaluated at import
            install("pathvalidate")
            install("beautifulsoup4")
            install("cryptography")
            install("typing-extensions")
            // 重型平台依赖：TIDAL / YouTube Music / Apple Music / SoundCloud
            install("aigpy")
            install("ytmusicapi")
            // pywidevine 1.9 requires pycryptodome >=3.23, but Chaquopy's
            // repository tops out at 3.21.0. 1.8.0 has the same public API
            // (PSSH/Cdm/Device) and accepts the wheel we can ship.
            install("pywidevine<1.9")
            install("pymp4")
            install("Unidecode")
            install("PyYAML")
            // pywidevine 1.8.0 owns the generated bindings and requires
            // protobuf <5 (the 1.9 bindings need >=6.33).
            install("protobuf<5")
            // NOTE: `orjson` (QQ Music JSON) has no Android wheel. The QQ
            // provider is therefore gated by musicdl.PLATFORM_DEPS instead of
            // being installed here; tests/test_android_deps.py enforces both
            // sides of that contract.
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("../../src")
        }
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.03")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.6")
    implementation("androidx.navigation:navigation-compose:2.8.0")
    implementation("androidx.datastore:datastore-preferences:1.1.1")
    implementation("androidx.documentfile:documentfile:1.0.1")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.jakewharton.retrofit:retrofit2-kotlinx-serialization-converter:1.0.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    debugImplementation("androidx.compose.ui:ui-tooling")

    // Local JVM unit tests (no device required).
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
}
