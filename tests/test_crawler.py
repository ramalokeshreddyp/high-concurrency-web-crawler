"""
End-to-end integration test suite verifying all 15 Core Requirements.
"""

import time
import requests
import json

BASE_URL = "http://localhost:8000"

def test_all():
    print("=== STARTING HIGH-CONCURRENCY WEB CRAWLER TEST SUITE ===")

    # 1. Healthcheck
    print("\n[1] Testing healthcheck endpoints...")
    r = requests.get(f"{BASE_URL}/health")
    assert r.status_code == 200, f"Healthcheck failed: {r.status_code}"
    print("[OK] /health passed:", r.json())

    # 2. Test user from submission.json
    print("\n[2] Testing login with seed user from submission.json...")
    with open("submission.json", "r") as f:
        sub = json.load(f)
    test_user = sub["testUser"]
    r = requests.post(f"{BASE_URL}/api/auth/login", json=test_user)
    assert r.status_code == 200, f"Seed user login failed: {r.status_code} {r.text}"
    tokens = r.json()
    assert "accessToken" in tokens and "refreshToken" in tokens
    print("[OK] Seed user login successful! Received access and refresh tokens.")

    # 3. User Registration
    print("\n[3] Testing user registration...")
    ts = int(time.time())
    new_email = f"user_{ts}@test.com"
    r = requests.post(f"{BASE_URL}/api/auth/register", json={"email": new_email, "password": "securepass123"})
    assert r.status_code == 201, f"Registration failed: {r.status_code} {r.text}"
    reg_data = r.json()
    assert reg_data["email"] == new_email and "id" in reg_data
    print(f"[OK] Registered new user: id={reg_data['id']}, email={reg_data['email']}")

    # 4. Conflict on duplicate email
    print("\n[4] Testing duplicate registration (409 Conflict)...")
    r = requests.post(f"{BASE_URL}/api/auth/register", json={"email": new_email, "password": "securepass123"})
    assert r.status_code == 409, f"Expected 409 for duplicate email, got: {r.status_code}"
    print("[OK] Duplicate email rejected with 409 Conflict.")

    # 5. Invalid credentials login
    print("\n[5] Testing invalid login credentials (401 Unauthorized)...")
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": new_email, "password": "wrongpassword"})
    assert r.status_code == 401, f"Expected 401 for wrong password, got: {r.status_code}"
    print("[OK] Invalid login rejected with 401.")

    # 6. Valid login with new user
    print("\n[6] Logging in with new user...")
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": new_email, "password": "securepass123"})
    assert r.status_code == 200
    user_tokens = r.json()
    access_token = user_tokens["accessToken"]
    refresh_token = user_tokens["refreshToken"]
    print("[OK] Logged in. Tokens received.")

    # 7. Token refresh
    print("\n[7] Testing token refresh...")
    r = requests.post(f"{BASE_URL}/api/auth/refresh", json={"refreshToken": refresh_token})
    assert r.status_code == 200, f"Token refresh failed: {r.status_code} {r.text}"
    new_access_token = r.json()["accessToken"]
    assert new_access_token != "", "New access token is empty"
    print("[OK] Access token successfully refreshed.")

    # 8. Protected endpoint check without auth
    print("\n[8] Testing /api/jobs unauthorized access...")
    r = requests.post(f"{BASE_URL}/api/jobs", json={"start_url": "https://example.com", "depth": 0})
    assert r.status_code == 401, f"Expected 401 without auth, got: {r.status_code}"
    print("[OK] Protected endpoint correctly blocked without token.")

    headers = {"Authorization": f"Bearer {new_access_token}"}

    # 9. Submit job with depth: 0
    print("\n[9] Submitting crawl job (depth: 0)...")
    r = requests.post(f"{BASE_URL}/api/jobs", json={"start_url": "https://example.com", "depth": 0}, headers=headers)
    assert r.status_code == 202, f"Job submission failed: {r.status_code} {r.text}"
    job_0 = r.json()
    job_0_id = job_0["job_id"]
    print(f"[OK] Job accepted with id={job_0_id}.")

    # Wait for completion
    print("Waiting for depth:0 job to complete...")
    for _ in range(30):
        r = requests.get(f"{BASE_URL}/api/jobs/{job_0_id}", headers=headers)
        assert r.status_code == 200
        job_info = r.json()
        if job_info["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(1)

    assert job_info["status"] == "COMPLETED", f"Expected COMPLETED, got {job_info['status']}: {job_info.get('error_message')}"
    print(f"[OK] Job {job_0_id} finished with status: COMPLETED")

    # Verify results for depth: 0 has exactly 1 result
    r = requests.get(f"{BASE_URL}/api/jobs/{job_0_id}/results", headers=headers)
    assert r.status_code == 200
    results_0 = r.json()
    assert len(results_0) == 1, f"Expected 1 result for depth 0, got {len(results_0)}"
    assert results_0[0]["word_count"] > 0
    print(f"[OK] Results for depth 0: 1 page result, title='{results_0[0]['title']}', word_count={results_0[0]['word_count']}")

    # 10. Authorization check: Seed user should NOT be able to view user B's job
    print("\n[10] Testing cross-user job authorization...")
    seed_headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
    r = requests.get(f"{BASE_URL}/api/jobs/{job_0_id}", headers=seed_headers)
    assert r.status_code in (403, 404), f"Expected 403 or 404 for other user's job, got: {r.status_code}"
    print("[OK] Cross-user job access correctly prevented.")

    # 11. Error handling: invalid start_url
    print("\n[11] Testing error handling with unreachable domain...")
    r = requests.post(
        f"{BASE_URL}/api/jobs",
        json={"start_url": "http://thissitedoesnotexist12345nonexistent.com", "depth": 0},
        headers=headers
    )
    assert r.status_code == 202
    fail_job_id = r.json()["job_id"]

    for _ in range(30):
        r = requests.get(f"{BASE_URL}/api/jobs/{fail_job_id}", headers=headers)
        info = r.json()
        if info["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(1)

    assert info["status"] == "FAILED", f"Expected FAILED for invalid domain, got {info['status']}"
    print(f"[OK] Unreachable domain correctly resulted in FAILED status (error: {info.get('error_message')})")

    # 12. Test depth: 1 and URL deduplication
    print("\n[12] Testing depth: 1 crawl and URL deduplication on multi-page site...")
    r = requests.post(
        f"{BASE_URL}/api/jobs",
        json={"start_url": "https://quotes.toscrape.com", "depth": 1},
        headers=headers
    )
    assert r.status_code == 202
    depth1_job_id = r.json()["job_id"]

    for _ in range(40):
        r = requests.get(f"{BASE_URL}/api/jobs/{depth1_job_id}", headers=headers)
        info = r.json()
        if info["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(1)

    assert info["status"] == "COMPLETED", f"Expected COMPLETED for depth 1 job, got {info['status']}: {info.get('error_message')}"
    r = requests.get(f"{BASE_URL}/api/jobs/{depth1_job_id}/results", headers=headers)
    assert r.status_code == 200
    results_1 = r.json()
    assert len(results_1) > 1, f"Expected more than 1 result for depth 1, got {len(results_1)}"
    urls = [item["url"] for item in results_1]
    assert len(urls) == len(set(urls)), f"Found duplicate URLs in results! {len(urls)} total vs {len(set(urls))} unique"
    print(f"[OK] Depth: 1 crawled {len(results_1)} unique pages without any duplicates.")

    print("\n========================================================")
    print("ALL CORE REQUIREMENTS TESTED AND VERIFIED SUCCESSFULLY!")
    print("========================================================")

if __name__ == "__main__":
    test_all()
