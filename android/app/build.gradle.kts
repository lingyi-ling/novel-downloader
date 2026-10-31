import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

// Release signing is opt-in. Create android/keystore.properties (git-ignored)
// with:
//   storeFile=release.jks        # absolute, or relative to the android/ dir
//   storePassword=...
//   keyAlias=...
//   keyPassword=...
// When the file is absent or incomplete, release builds fall back to the debug
// key so the module still assembles without real signing material.
val keystorePropertiesFile = rootProject.file("keystore.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.isFile) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}
val hasReleaseSigning = keystorePropertiesFile.isFile &&
    listOf("storeFile", "storePassword", "keyAlias", "keyPassword").all { key ->
        !keystoreProperties.getProperty(key).isNullOrBlank()
    }
if (!hasReleaseSigning) {
    logger.warn(
        "keystore.properties is missing or incomplete; " +
            "release builds will fall back to the debug signing config.",
    )
}

android {
    namespace = "com.lingyi.noveldl"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.lingyi.noveldl"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "1.2.0"
        // Chaquopy requires explicit ABIs; arm64 covers modern phones, x86_64
        // keeps the emulator usable.
        ndk.abiFilters += listOf("arm64-v8a", "x86_64")
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = if (hasReleaseSigning) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
            isMinifyEnabled = false
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
