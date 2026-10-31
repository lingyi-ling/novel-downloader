package com.lingyi.noveldl.data

import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface ApiService {
    @GET("api/v1/health")
    suspend fun health(): Health

    @GET("api/v1/sites")
    suspend fun sites(): SitesResponse

    @POST("api/v1/search")
    suspend fun search(@Body body: SearchRequest): SearchResponse

    @GET("api/v1/tasks")
    suspend fun tasks(): TasksResponse

    @POST("api/v1/tasks")
    suspend fun addTask(@Body body: AddTaskRequest): Task

    @POST("api/v1/tasks/{task_id}/cancel")
    suspend fun cancelTask(@Path("task_id") taskId: String): CancelResponse

    /** Pending credential requests created by login-required sites. */
    @GET("api/v1/login/requests")
    suspend fun loginRequests(): LoginRequestsResponse

    /** Submits the credential values and wakes the waiting task. */
    @POST("api/v1/login/requests/{req_id}")
    suspend fun submitLogin(
        @Path("req_id") reqId: String,
        @Body body: LoginSubmitRequest,
    ): LoginSubmitResponse

    @GET("api/v1/history")
    suspend fun history(): HistoryResponse

    @GET("api/v1/music/platforms")
    suspend fun musicPlatforms(): MusicPlatformsResponse

    @POST("api/v1/music/search")
    suspend fun musicSearch(@Body body: MusicSearchRequest): MusicSearchResponse

    @POST("api/v1/music/jobs")
    suspend fun createMusicJob(@Body body: MusicJobRequest): MusicJobCreated

    @GET("api/v1/music/jobs/{job_id}")
    suspend fun musicJob(@Path("job_id") jobId: String): MusicJob

    /** Per-platform connectivity probe; returns a one-element list for a known [platform]. */
    @GET("api/v1/music/health")
    suspend fun musicHealth(
        @Query("platform") platform: String,
        @Query("timeout") timeout: Int,
    ): List<MusicHealthItem>

    @GET("api/v1/watchlist")
    suspend fun watchlist(): WatchlistResponse

    @POST("api/v1/watchlist")
    suspend fun addWatch(@Body body: WatchlistAddRequest): WatchlistAddResponse

    @DELETE("api/v1/watchlist/{site}/{book_id}")
    suspend fun removeWatch(
        @Path("site") site: String,
        @Path("book_id") bookId: String,
    ): CancelResponse

    @POST("api/v1/watchlist/check")
    suspend fun checkWatchlist(@Body body: WatchlistCheckRequest): WatchlistCheckResponse
}
