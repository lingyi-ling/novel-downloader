package com.lingyi.noveldl.security

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

/**
 * Static assertions over the security-relevant XML that only exists as data:
 * the manifest, the FileProvider paths, the backup rules and the network
 * security config. These are read from the source tree (the same bytes a
 * release build packages), so a regression fails the JVM unit-test gate
 * without needing a device.
 *
 * The counterpart of these checks is `gradlew lintDebug`, which audits the
 * *merged* manifest and therefore also catches components added by
 * dependencies.
 */
class AndroidSecurityConfigTest {

    // ------------------------------------------------------------------
    // Manifest: permissions and exported components
    // ------------------------------------------------------------------

    @Test
    fun manifestDeclaresOnlyTheNecessaryPermissions() {
        val permissions = manifest()
            .elements("uses-permission")
            .map { it.androidAttr("name").orEmpty() }
            .toSet()

        assertEquals(
            setOf(
                "android.permission.INTERNET",
                "android.permission.POST_NOTIFICATIONS",
                "android.permission.REQUEST_INSTALL_PACKAGES",
            ),
            permissions,
        )

        // Permissions that would widen the app's reach far beyond what the
        // product needs (unrestricted storage, package inventory, overlays,
        // battery-optimisation exemptions).
        val overBroad = setOf(
            "android.permission.MANAGE_EXTERNAL_STORAGE",
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.QUERY_ALL_PACKAGES",
            "android.permission.SYSTEM_ALERT_WINDOW",
            "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
        )
        assertEquals(emptySet<String>(), permissions.intersect(overBroad))
    }

    @Test
    fun onlyTheLauncherActivityIsExportedAndItAcceptsNoDeepLinks() {
        val app = application()
        val activities = app.elements("activity")
        assertEquals(1, activities.size)

        val activity = activities.single()
        assertEquals(".MainActivity", activity.androidAttr("name"))
        assertEquals("true", activity.androidAttr("exported"))

        val filter = activity.elements("intent-filter").single()
        assertEquals(
            listOf("android.intent.action.MAIN"),
            filter.elements("action").map { it.androidAttr("name") },
        )
        assertEquals(
            listOf("android.intent.category.LAUNCHER"),
            filter.elements("category").map { it.androidAttr("name") },
        )
        // No BROWSABLE/VIEW filter means no attacker-controlled deep-link
        // arguments enter the exported activity.
        assertTrue(filter.elements("data").isEmpty())

        // The app declares no background components of its own; the download
        // completion receiver is registered in-process (HistoryScreen).
        assertTrue(app.elements("service").isEmpty())
        assertTrue(app.elements("receiver").isEmpty())
    }

    @Test
    fun applicationUsesPrivateBackupAndTransportConfigs() {
        val app = application()
        assertEquals("true", app.androidAttr("allowBackup"))
        assertEquals("@xml/data_extraction_rules", app.androidAttr("dataExtractionRules"))
        assertEquals("@xml/backup_rules", app.androidAttr("fullBackupContent"))
        assertEquals("@xml/network_security_config", app.androidAttr("networkSecurityConfig"))
        // Cleartext and CA trust are decided by the network security config,
        // never by this coarse flag.
        assertNull(app.androidAttr("usesCleartextTraffic"))
        assertNull(app.androidAttr("debuggable"))
    }

    // ------------------------------------------------------------------
    // FileProvider
    // ------------------------------------------------------------------

    @Test
    fun fileProviderIsPrivateAndLimitedToVerifiedUpdatesAndLyrics() {
        val provider = application().elements("provider").single()
        assertEquals("androidx.core.content.FileProvider", provider.androidAttr("name"))
        assertEquals("\${applicationId}.fileprovider", provider.androidAttr("authorities"))
        assertEquals("false", provider.androidAttr("exported"))
        assertEquals("true", provider.androidAttr("grantUriPermissions"))
        assertNull(provider.androidAttr("permission"))

        val metadata = provider.elements("meta-data").single()
        assertEquals("android.support.FILE_PROVIDER_PATHS", metadata.androidAttr("name"))
        assertEquals("@xml/file_paths", metadata.androidAttr("resource"))

        val paths = parseXml("src/main/res/xml/file_paths.xml").documentElement
        assertEquals("paths", paths.tagName)
        val entries = paths.elements()
        // Exactly two narrow ranges: the verified update APK and the music
        // engine's output (where `<stem>.lrc` lives). The provider never
        // exposes an entire domain.
        assertEquals(2, entries.size)

        val updates = entries.single { it.getAttribute("name") == "updates" }
        assertEquals("cache-path", updates.tagName)
        assertEquals("updates/", updates.getAttribute("path"))

        val music = entries.single { it.getAttribute("name") == "music" }
        assertEquals("files-path", music.tagName)
        assertEquals("engine_home/downloads/music/", music.getAttribute("path"))
    }

    // ------------------------------------------------------------------
    // Backup / data extraction
    // ------------------------------------------------------------------

    @Test
    fun cloudBackupExcludesEveryPrivateDomainAndMatchesLegacyRules() {
        val legacy = excludes(
            parseXml("src/main/res/xml/backup_rules.xml").documentElement,
        )
        val extractionRoot = parseXml("src/main/res/xml/data_extraction_rules.xml").documentElement
        val cloud = extractionRoot.elements("cloud-backup").single()

        // Android 11 and Android 12+ must exclude exactly the same paths;
        // a credential dropped from only one list leaks on the other OS.
        assertEquals(legacy, excludes(cloud))

        // Credentials live in several files whose directories depend on
        // platformdirs' Android-vs-Unix branch (files/datastore,
        // files/engine_home, files/novel_downloader/music, possibly
        // files/.local/share/...). Excluding the whole private tree is the
        // only list that cannot silently miss one of them. "root" is the app
        // data directory; file/database/sharedpref/external are sibling
        // domains.
        assertEquals(
            setOf(
                "root:.",
                "file:.",
                "database:.",
                "sharedpref:.",
                "external:.",
            ),
            legacy,
        )
        // An <include> would re-admit files the exclude list removed.
        assertTrue(cloud.elements("include").isEmpty())

        // Device-to-device transfer is intentionally not narrowed: it is a
        // local, user-initiated path to the user's own new device. A future
        // edit that adds excludes there is a policy change and must be
        // deliberate, so fail loudly instead of silently dropping settings.
        assertTrue(extractionRoot.elements("device-transfer").isEmpty())
    }

    // ------------------------------------------------------------------
    // Network security
    // ------------------------------------------------------------------

    @Test
    fun networkSecurityConfigPinsTheUpdateChannel() {
        val root = parseXml("src/main/res/xml/network_security_config.xml").documentElement
        assertEquals("network-security-config", root.tagName)

        // Cleartext stays available for the loopback engine and user-hosted
        // LAN servers, but never for the APK download channel.
        val base = root.elements("base-config").single()
        assertEquals("true", base.getAttribute("cleartextTrafficPermitted"))
        assertEquals(listOf("system"), trustSources(base))

        val update = root.elements("domain-config").single()
        assertEquals("false", update.getAttribute("cleartextTrafficPermitted"))
        assertEquals(
            setOf("github.com", "githubusercontent.com"),
            update.elements("domain").map { it.textContent.trim() }.toSet(),
        )
        assertTrue(
            update.elements("domain").all {
                it.getAttribute("includeSubdomains") == "true"
            },
        )
        // User-installed CAs must not be enough to impersonate GitHub and
        // supply a forged manifest plus a matching APK.
        assertEquals(listOf("system"), trustSources(update))

        // Debug overrides would ship with debuggable builds only, but keeping
        // them out entirely means no release path can ever enable them.
        assertTrue(root.elements("debug-overrides").isEmpty())
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun manifest(): Element = parseXml("src/main/AndroidManifest.xml").documentElement

    private fun application(): Element = manifest().elements("application").single()

    private fun excludes(parent: Element): Set<String> =
        parent.elements("exclude").map { entry ->
            "${entry.getAttribute("domain")}:${entry.getAttribute("path")}"
        }.toSet()

    private fun trustSources(domainConfig: Element): List<String> =
        domainConfig.elements("trust-anchors").single()
            .elements("certificates")
            .map { it.getAttribute("src") }

    private fun Element.elements(tag: String = "*"): List<Element> =
        (0 until childNodes.length)
            .mapNotNull { childNodes.item(it) as? Element }
            .filter { tag == "*" || it.tagName == tag }

    private fun Element.androidAttr(name: String): String? =
        if (hasAttributeNS(ANDROID_NS, name)) getAttributeNS(ANDROID_NS, name) else null

    private fun parseXml(relativePath: String): Document =
        DocumentBuilderFactory.newInstance().apply {
            isNamespaceAware = true
            // These files are ours; still, never resolve external entities.
            runCatching {
                setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)
            }
            runCatching {
                setFeature("http://xml.org/sax/features/external-general-entities", false)
            }
            runCatching {
                setFeature("http://xml.org/sax/features/external-parameter-entities", false)
            }
            runCatching {
                setFeature(
                    "http://apache.org/xml/features/nonvalidating/load-external-dtd",
                    false,
                )
            }
        }.newDocumentBuilder().parse(File(moduleDir, relativePath))

    private companion object {
        const val ANDROID_NS = "http://schemas.android.com/apk/res/android"

        /**
         * The Gradle test worker's working directory is the module directory,
         * but plain `gradle test` invocations from the android/ or repo root
         * must work too, so walk up until one of the two layouts matches.
         */
        val moduleDir: File = run {
            val start = File(System.getProperty("user.dir")).absoluteFile
            generateSequence(start) { it.parentFile }
                .firstNotNullOfOrNull { dir ->
                    when {
                        File(dir, "src/main/AndroidManifest.xml").isFile -> dir
                        File(dir, "app/src/main/AndroidManifest.xml").isFile -> File(dir, "app")
                        else -> null
                    }
                }
                ?: error("Android app module not found above $start")
        }
    }
}
