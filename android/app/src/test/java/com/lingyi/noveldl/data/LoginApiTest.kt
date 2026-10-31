package com.lingyi.noveldl.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Contract tests for the login-credential endpoints: parsing the pending
 * request list and the submit call's request shape / response handling.
 */
class LoginApiTest {

    private lateinit var server: MockWebServer

    @Before
    fun startServer() {
        server = MockWebServer()
        server.start()
        Api.configure(server.url("/").toString(), null)
    }

    @After
    fun stopServer() {
        server.shutdown()
        val field = Api::class.java.getDeclaredField("service")
        field.isAccessible = true
        field.set(Api, null)
    }

    private fun json(body: String, code: Int = 200): MockResponse =
        MockResponse()
            .setResponseCode(code)
            .setHeader("Content-Type", "application/json")
            .setBody(body)

    @Test
    fun `loginRequests parses pending list`() = runBlocking {
        server.enqueue(
            json(
                """
                {
                  "total": 1,
                  "requests": [
                    {
                      "req_id": "r1",
                      "task_id": "t1",
                      "title": "Sample Book",
                      "fields": [
                        {
                          "name": "cookies",
                          "label": "Cookie",
                          "type": "cookie",
                          "required": true,
                          "default": "",
                          "placeholder": "Paste cookies",
                          "description": "copy from browser"
                        }
                      ],
                      "prefill": {"cookies": "k=v"},
                      "done": false
                    }
                  ]
                }
                """.trimIndent(),
            ),
        )
        val response = Api.service().loginRequests()
        assertEquals(1, response.total)
        val req = response.requests.single()
        assertEquals("r1", req.req_id)
        assertEquals("t1", req.task_id)
        assertEquals("Sample Book", req.title)
        assertFalse(req.done)
        val field = req.fields.single()
        assertEquals("cookies", field.name)
        assertEquals("cookie", field.type)
        assertTrue(field.required)
        assertEquals("k=v", req.prefill["cookies"])
    }

    @Test
    fun `loginRequests tolerates empty list`() = runBlocking {
        server.enqueue(json("""{"total": 0, "requests": []}"""))
        val response = Api.service().loginRequests()
        assertEquals(0, response.total)
        assertTrue(response.requests.isEmpty())
    }

    @Test
    fun `submitLogin posts values under req_id`() = runBlocking {
        server.enqueue(json("""{"ok": true}"""))
        val response = Api.service().submitLogin(
            "r1",
            LoginSubmitRequest(values = mapOf("cookies" to "key=value; uid=42")),
        )
        assertTrue(response.ok)
        val recorded = server.takeRequest()
        assertEquals("/api/v1/login/requests/r1", recorded.path)
        assertEquals("POST", recorded.method)
        val body = recorded.body.readUtf8()
        assertTrue(body.contains("\"cookies\""))
        assertTrue(body.contains("key=value; uid=42"))
    }
}
