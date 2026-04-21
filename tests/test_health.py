async def test_health_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "python_info" in response.text
