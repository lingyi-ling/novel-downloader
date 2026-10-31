package com.lingyi.noveldl.data

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.Serializable

@Serializable
data class Health(val status: String = "", val version: String = "")

@Serializable
data class SitesResponse(
    val search: Map<String, String> = emptyMap(),
    val download: Map<String, String> = emptyMap(),
)

@Serializable
data class SearchResult(
    val site: String = "",
    val book_id: String = "",
    val book_url: String = "",
    val cover_url: String = "",
    val title: String = "",
    val author: String = "",
    val latest_chapter: String = "",
    val update_date: String = "",
    val word_count: String = "",
    val priority: Int = 0,
)

@Serializable
data class SearchRequest(
    val keyword: String,
    val sites: List<String>? = null,
    val per_site_limit: Int = 5,
    val timeout: Double = 5.0,
    val sort: String = "quality",
    val order: String? = null,
    val limit: Int? = null,
)

@Serializable
data class SearchResponse(
    val total: Int = 0,
    val results: List<SearchResult> = emptyList(),
)

@Serializable
data class Task(
    val task_id: String = "",
    val title: String = "",
    val site: String = "",
    val book_id: String = "",
    val status: String = "",
    val chapters_done: Int = 0,
    val chapters_total: Int = 0,
    val progress: Double = 0.0,
    val error: String? = null,
    val exported_paths: Map<String, String> = emptyMap(),
    val created_at: Double = 0.0,
    val finished_at: Double? = null,
)

@Serializable
data class TasksResponse(
    val pending: List<Task> = emptyList(),
    val running: List<Task> = emptyList(),
    val completed: List<Task> = emptyList(),
)

@Serializable
data class AddTaskRequest(
    val site: String,
    val book_id: String,
    val title: String? = null,
)

@Serializable
data class CancelResponse(val ok: Boolean = false)

@Serializable
data class HistoryFile(
    val name: String = "",
    val ext: String = "",
    val size: Long = 0,
    val mtime: Double = 0.0,
    val download_url: String = "",
)

@Serializable
data class HistoryResponse(
    val total: Int = 0,
    val files: List<HistoryFile> = emptyList(),
)

@Serializable
data class MusicPlatform(
    val key: String = "",
    val label: String = "",
    val note: String = "",
)

@Serializable
data class MusicPlatformsResponse(
    val platforms: List<MusicPlatform> = emptyList(),
)

@Serializable
data class MusicTrack(
    val provider: String = "",
    val id: String = "",
    val name: String = "",
    val artists: String = "",
    val album: String = "",
    val duration_s: Int = 0,
    val cover_url: String = "",
    val vip: Boolean = false,
)

/**
 * Body of `POST /api/v1/music/search`.
 *
 * `limit`/`offset` use [EncodeDefault] with [EncodeDefault.Mode.ALWAYS] so the
 * pagination parameters stay on the wire even when they equal their defaults
 * (`Api`'s serializer skips plain defaults), and so the server can page as
 * soon as it wires `offset` through to the provider layer.
 */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class MusicSearchRequest(
    val platform: String,
    val keyword: String,
    @EncodeDefault(EncodeDefault.Mode.ALWAYS) val limit: Int = 20,
    @EncodeDefault(EncodeDefault.Mode.ALWAYS) val offset: Int = 0,
)

@Serializable
data class MusicSearchResponse(
    val total: Int = 0,
    val tracks: List<MusicTrack> = emptyList(),
)

/** One probe of `GET /api/v1/music/health` (per-platform connectivity). */
@Serializable
data class MusicHealthItem(
    val platform: String = "",
    val ok: Boolean = false,
    val elapsed_ms: Long = 0,
    val error: String = "",
)

@Serializable
data class MusicJobRequest(
    val platform: String,
    val tracks: List<MusicTrack>,
    val level: String? = null,
)

@Serializable
data class MusicJobCreated(val job_id: String = "")

@Serializable
data class MusicJobProgress(
    val done: Int = 0,
    val total: Int = 0,
)

@Serializable
data class MusicJobResult(
    val status: String = "",
    val reason: String = "",
    val path: String = "",
    val bytes: Long = 0,
)

@Serializable
data class MusicJob(
    val job_id: String = "",
    val status: String = "",
    val progress: MusicJobProgress = MusicJobProgress(),
    val results: List<MusicJobResult> = emptyList(),
    val error: String? = null,
)

@Serializable
data class WatchEntry(
    val site: String = "",
    val book_id: String = "",
    val title: String = "",
    val last_chapter: String = "",
    val updated_at: String = "",
)

@Serializable
data class WatchlistResponse(
    val total: Int = 0,
    val books: List<WatchEntry> = emptyList(),
)

@Serializable
data class WatchlistAddRequest(
    val site: String,
    val book_id: String,
    val title: String? = null,
)

@Serializable
data class WatchlistAddResponse(
    val created: Boolean = false,
    val entry: WatchEntry = WatchEntry(),
)

@Serializable
data class WatchlistCheckRequest(val download: Boolean = false)

/** Ready-to-post body for ``POST /api/v1/tasks`` attached by ``download=true`` checks. */
@Serializable
data class WatchTaskBody(
    val site: String = "",
    val book_id: String = "",
    val title: String? = null,
)

@Serializable
data class WatchCheckItem(
    val site: String = "",
    val book_id: String = "",
    val title: String = "",
    val previous_chapter: String = "",
    val last_chapter: String = "",
    val found: Boolean = false,
    val changed: Boolean = false,
    val needs_download: Boolean = false,
    val error: String? = null,
    val task: WatchTaskBody? = null,
)

@Serializable
data class WatchlistCheckResponse(
    val checked: Int = 0,
    val updated: Int = 0,
    val failed: Int = 0,
    val download: Boolean = false,
    val needs_download: List<WatchCheckItem> = emptyList(),
    val items: List<WatchCheckItem> = emptyList(),
)

// ---------------------------------------------------------------------------
// Login credential requests (POST /api/v1/login/requests/{req_id})
// ---------------------------------------------------------------------------

@Serializable
data class LoginField(
    val name: String = "",
    val label: String = "",
    val type: String = "text",
    val required: Boolean = false,
    val default: String = "",
    val placeholder: String = "",
    val description: String = "",
)

@Serializable
data class LoginRequestDto(
    val req_id: String = "",
    val task_id: String = "",
    val title: String = "",
    val fields: List<LoginField> = emptyList(),
    val prefill: Map<String, String> = emptyMap(),
    val done: Boolean = false,
)

@Serializable
data class LoginRequestsResponse(
    val total: Int = 0,
    val requests: List<LoginRequestDto> = emptyList(),
)

@Serializable
data class LoginSubmitRequest(
    val values: Map<String, String> = emptyMap(),
)

@Serializable
data class LoginSubmitResponse(
    val ok: Boolean = false,
)
