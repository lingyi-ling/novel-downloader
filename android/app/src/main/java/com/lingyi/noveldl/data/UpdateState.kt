package com.lingyi.noveldl.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File

/** Where the in-app updater currently stands. */
sealed interface UpdatePhase {

    data object Idle : UpdatePhase

    data object Checking : UpdatePhase

    data class UpToDate(val info: UpdateInfo) : UpdatePhase

    data class Available(val info: UpdateInfo) : UpdatePhase

    data class Downloading(val info: UpdateInfo) : UpdatePhase

    data class Ready(val info: UpdateInfo, val file: File) : UpdatePhase

    data class Failed(
        val info: UpdateInfo?,
        val reason: UpdateFailure,
        val detail: String? = null,
    ) : UpdatePhase
}

/**
 * Process-wide update state and side effects.
 *
 * The state lives outside the Activity so a download survives a rotation, and
 * both the startup dialog (MainActivity) and the settings section observe the
 * same flow instead of running two competing checks.
 *
 * Installing is deliberately not silent: Android only lets the system installer
 * (`ACTION_VIEW` on a `FileProvider` URI) or a privileged app replace a package.
 */
object UpdateCenter {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    private val _phase = MutableStateFlow<UpdatePhase>(UpdatePhase.Idle)
    val phase: StateFlow<UpdatePhase> = _phase.asStateFlow()

    private val _dialogVisible = MutableStateFlow(false)
    val dialogVisible: StateFlow<Boolean> = _dialogVisible.asStateFlow()

    const val APK_MIME: String = "application/vnd.android.package-archive"

    @Volatile
    private var appContext: Context? = null

    /** True while we wait for the user to return from the "unknown sources" screen. */
    @Volatile
    private var awaitingInstallPermission = false

    /** Called once from `MainActivity.onCreate`; stores a download target. */
    fun init(context: Context) {
        appContext = context.applicationContext
    }

    /**
     * Runs the throttled startup check. [onChecked] receives the parsed info
     * (`null` when the request failed) so the caller can record a successful
     * check and skip the next 24 hours.
     */
    fun check(
        currentVersion: String,
        notifyDialog: Boolean,
        onChecked: (UpdateInfo?) -> Unit = {},
    ) {
        if (_phase.value is UpdatePhase.Downloading) return
        scope.launch {
            _phase.value = UpdatePhase.Checking
            val info = UpdateChecker.check(currentVersion, preferredAbis = deviceAbis())
            when {
                info == null -> {
                    // Stay quiet: a failed check must not bother the user.
                    _phase.value = UpdatePhase.Failed(null, UpdateFailure.CHECK_FAILED)
                }
                info.available -> {
                    _phase.value = UpdatePhase.Available(info)
                    if (notifyDialog) _dialogVisible.value = true
                }
                else -> _phase.value = UpdatePhase.UpToDate(info)
            }
            onChecked(info)
        }
    }

    /**
     * Hides the dialog for this session ("稍后"). The pending update stays in
     * [phase], so the settings section can still offer to install it.
     */
    fun dismiss() {
        _dialogVisible.value = false
    }

    /** Downloads and verifies the asset of the pending update. */
    fun download() {
        val info = when (val current = _phase.value) {
            is UpdatePhase.Available -> current.info
            is UpdatePhase.Failed -> current.info
            else -> return
        } ?: return
        // Refuse before touching the network when the release cannot be
        // verified or offers nothing to install.
        downloadRequestFailure(info)?.let { reason ->
            _phase.value = UpdatePhase.Failed(info, reason)
            return
        }
        val context = appContext ?: return
        scope.launch {
            _phase.value = UpdatePhase.Downloading(info)
            _phase.value = try {
                UpdatePhase.Ready(info, UpdateChecker.download(context, info))
            } catch (e: CancellationException) {
                throw e
            } catch (e: UpdateException) {
                UpdatePhase.Failed(info, e.reason, e.message)
            } catch (e: Exception) {
                UpdatePhase.Failed(info, UpdateFailure.DOWNLOAD_FAILED, e.message)
            }
        }
    }

    /** Whether this app may start the system installer (Android 8+). */
    fun canInstall(context: Context): Boolean =
        context.packageManager.canRequestPackageInstalls()

    /**
     * Hands the verified APK to the system installer. Without the
     * "install unknown apps" grant the user is sent to the per-app switch and
     * the attempt is repeated from [onResume] once they come back.
     *
     * The digest is recomputed here, right before the installer is launched:
     * the verification performed at download time is stale by now (the cache
     * may have been cleared or the file replaced/truncated in between).
     */
    fun install(context: Context) {
        val ready = _phase.value as? UpdatePhase.Ready ?: return
        installPrecheck(ready.file, ready.info.sha256)?.let { reason ->
            _phase.value = UpdatePhase.Failed(ready.info, reason)
            return
        }
        if (!canInstall(context)) {
            awaitingInstallPermission = true
            requestInstallPermission(context)
            return
        }
        awaitingInstallPermission = false
        launchInstaller(context, ready)
    }

    /** Call from `Activity.onResume`; retries the install after the grant. */
    fun onResume(context: Context) {
        if (!awaitingInstallPermission) return
        if (!canInstall(context)) return
        install(context)
    }

    private fun requestInstallPermission(context: Context) {
        val intent = Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:${context.packageName}"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { context.startActivity(intent) }
    }

    private fun launchInstaller(context: Context, ready: UpdatePhase.Ready) {
        // getUriForFile itself throws when the file falls outside the paths
        // declared in file_paths.xml, so the whole chain is guarded: every
        // failure has to surface as INSTALL_FAILED instead of a crash.
        runCatching {
            val uri = FileProvider.getUriForFile(
                context,
                "${context.packageName}.fileprovider",
                ready.file,
            )
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, APK_MIME)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
        }.onFailure {
            _phase.value = UpdatePhase.Failed(
                ready.info,
                UpdateFailure.INSTALL_FAILED,
                it.message,
            )
        }
    }

    private fun deviceAbis(): List<String> = runCatching {
        android.os.Build.SUPPORTED_ABIS.toList()
    }.getOrDefault(emptyList())
}
