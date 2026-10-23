package com.lingyi.noveldl.data

import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path

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
}
