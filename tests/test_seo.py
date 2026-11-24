"""Проверки SEO-маршрутов и метаданных без доступа к рабочей базе.

Запуск: python -m unittest discover -s tests -p 'test_seo.py'
Требуются зависимости сервера и httpx для FastAPI TestClient.
"""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Импорт сервера инициализирует БД: всегда используем временный каталог.
_data_dir = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _data_dir.name
os.environ["ENV_SYNC"] = "0"
os.environ["AUTO_UPDATE"] = "0"
os.environ["GDRIVE_FOLDER_ID"] = ""
os.environ["TWITCH_PUSH_POLL"] = "0"
os.environ["OG_FETCH_REMOTE"] = "0"

from fastapi.testclient import TestClient
import server


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonicals = []
        self.meta = {}
        self.titles = []
        self.h1_count = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonicals.append(attrs.get("href"))
        if tag == "meta":
            key = attrs.get("name") or attrs.get("property")
            if key:
                self.meta[key] = attrs.get("content", "")
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self.h1_count += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.titles.append(data)


class SeoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url="https://krafen.me", follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_public_page_redirects_preserve_query_for_get_and_head(self):
        redirects = {"/index.html": "/", "/recap": "/wrapped", "/recap/": "/wrapped", "/recap.html": "/wrapped"}
        for page in ("watchlist", "gamelist", "setup", "wishlist", "danonsha", "wrapped"):
            redirects[f"/{page}/"] = f"/{page}"
            redirects[f"/{page}.html"] = f"/{page}"
        for original, target in redirects.items():
            for method in ("GET", "HEAD"):
                with self.subTest(original=original, method=method):
                    response = self.client.request(method, original + "?a=1&b=x%2Fy")
                    self.assertEqual(response.status_code, 301)
                    self.assertEqual(response.headers["location"], target + "?a=1&b=x%2Fy")
                    self.assertEqual(self.client.get(target).status_code, 200)

    def test_sitemap_pages_have_consistent_metadata(self):
        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        root = ET.fromstring(sitemap.content)
        urls = [element.text for element in root.findall("{*}url/{*}loc")]
        self.assertEqual(len(urls), 7)
        titles = set()
        descriptions = set()
        for url in urls:
            with self.subTest(url=url):
                parsed = urlparse(url)
                self.assertEqual((parsed.scheme, parsed.netloc), ("https", "krafen.me"))
                response = self.client.get(parsed.path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("noindex", response.headers.get("x-robots-tag", "").lower())
                meta = Metadata()
                meta.feed(response.text)
                self.assertEqual(meta.canonicals, [url])
                self.assertEqual(meta.meta.get("og:url"), url)
                self.assertTrue(meta.meta.get("og:title"))
                self.assertTrue(meta.meta.get("og:description"))
                slug = parsed.path.strip("/") or "index"
                self.assertEqual(meta.meta.get("og:image"), f"https://krafen.me/og/{slug}.png")
                self.assertEqual(meta.meta.get("twitter:card"), "summary_large_image")
                og_resp = self.client.get(f"/og/{slug}.png")
                self.assertEqual(og_resp.status_code, 200)
                self.assertEqual(og_resp.headers.get("content-type"), "image/png")
                self.assertTrue(og_resp.content.startswith(b"\x89PNG"))
                self.assertEqual(meta.h1_count, 1)
                title = "".join(meta.titles)
                description = meta.meta.get("description", "")
                self.assertTrue(30 <= len(title) <= 60)
                self.assertTrue(70 <= len(description) <= 160)
                titles.add(title)
                descriptions.add(description)
        self.assertEqual(len(titles), len(urls))
        self.assertEqual(len(descriptions), len(urls))

    def test_noindex_pages_and_public_data(self):
        robots = self.client.get("/robots.txt").text
        self.assertIn("Disallow: /api/", robots)
        self.assertNotIn("Disallow: /tasks", robots)
        for name in ("watchlist", "gamelist", "setup", "wishlist"):
            with self.subTest(name=name):
                self.assertIn(f"Allow: /api/{name}$", robots)
                response = self.client.get(f"/api/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("noindex", response.headers.get("x-robots-tag", ""))
        for path in ("/tasks", "/admin", "/danonsha/watchlist"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("noindex", response.headers.get("x-robots-tag", ""))
        resp_404 = self.client.get("/this-page-does-not-exist")
        self.assertEqual(resp_404.status_code, 404)
        self.assertIn("text/html", resp_404.headers.get("content-type", ""))
        self.assertIn("404", resp_404.text)
        self.assertIn("noindex", resp_404.headers.get("x-robots-tag", "").lower())

        r_404_html = self.client.get("/404.html")
        self.assertEqual(r_404_html.status_code, 301)
        self.assertEqual(r_404_html.headers["location"], "/404")

        r_404 = self.client.get("/404")
        self.assertEqual(r_404.status_code, 404)
        self.assertIn("text/html", r_404.headers.get("content-type", ""))

        r_api_404 = self.client.get("/api/nonexistent")
        self.assertEqual(r_api_404.status_code, 404)
        self.assertEqual(r_api_404.json(), {"error": "not found"})

        self.assertEqual(self.client.get("/style.css").status_code, 200)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/cmdk.js").status_code, 200)
        self.assertEqual(self.client.get("/suggest").status_code, 200)
        self.assertEqual(self.client.get("/favicon.png").status_code, 200)

        same_orig = {"Origin": "https://krafen.me", "Sec-Fetch-Site": "same-origin"}

        # Новые UI-секции (кинотеатр /danonsha, /suggest, рулетка, PNG-карточки)
        dan_html = self.client.get("/danonsha").text
        for needle in (
            "watch-heatmap", "watch-resume-banner", "watch-chat-top",
            "watch-speed", "watch-share-ts", "watch-chat-search", "/suggest",
        ):
            self.assertIn(needle, dan_html)
        self.assertNotIn("ambient-audio.js", dan_html)
        self.assertNotIn("attachLatestVodToTwitchCard", self.client.get("/").text)
        self.assertNotIn("latest-vod-pill", dan_html)
        self.assertIn("sug-submit", self.client.get("/suggest").text)
        wl_html = self.client.get("/watchlist").text
        gl_html = self.client.get("/gamelist").text
        self.assertIn("pick-random-btn", wl_html)
        self.assertIn("/suggest?type=movie", wl_html)
        self.assertIn("pick-random-btn", gl_html)
        self.assertIn("/suggest?type=game", gl_html)
        self.assertIn("/api/card/setup.png", self.client.get("/setup").text)
        self.assertIn("/api/card/wishlist.png", self.client.get("/wishlist").text)

        # Проверка расчёта 00:00 МСК для ежедневного бэкапа
        from datetime import datetime
        dt_msk = server._next_msk_midnight(datetime(2026, 11, 25, 22, 15, 0, tzinfo=server.MSK_TZ))
        self.assertEqual((dt_msk.day, dt_msk.hour, dt_msk.minute), (26, 0, 0))

        # Генерация PNG-карточек витрины (Pillow)
        for card_kind in ("setup", "wishlist"):
            c_resp = self.client.get(f"/api/card/{card_kind}.png")
            self.assertEqual(c_resp.status_code, 200)
            self.assertEqual(c_resp.headers.get("content-type"), "image/png")
            self.assertTrue(c_resp.content.startswith(b"\x89PNG"))

        # Закрытые админ-диагностики, рекордер-вебхуки и предложка
        self.assertEqual(self.client.post("/api/admin/links/check", headers=same_orig, json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/alerts/test", headers=same_orig, json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/backup/telegram", headers=same_orig, json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/backup/zip").status_code, 401)
        admin_html = self.client.get("/admin").text
        self.assertIn("top-force-backup-btn", admin_html)
        self.assertIn("btn-download-backup-zip", admin_html)
        self.assertEqual(self.client.post("/api/admin/suggestions/resolve", headers=same_orig, json={"id": 1, "action": "reject"}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/import-list", headers=same_orig, json={"target": "watchlist", "raw": "Test"}).status_code, 401)
        self.assertIn(self.client.post("/api/recorder/heartbeat", json={"channel": "danonsha"}).status_code, (401, 503))
        self.assertEqual(self.client.post("/api/suggestions", headers=same_orig, json={"kind": "invalid", "title": "Test"}).status_code, 400)
        self.assertEqual(self.client.post("/api/suggestions", headers=same_orig, json={"kind": "movie", "media_type": "movie", "title": "Interstellar", "year": "2014"}).status_code, 200)
        self.assertEqual(self.client.post("/api/suggestions", headers=same_orig, json={"kind": "other", "media_type": "челлендж", "title": "Пройти игру с закрытыми глазами", "link": "https://youtube.com/watch?v=123"}).status_code, 200)

        for bad_url in ("http://127.0.0.1/test", "http://169.254.169.254/latest", "http://localhost:8080/a", "file:///etc/passwd"):
            self.assertIsNone(server._is_safe_public_http_url(bad_url))
        self.assertEqual(server._is_safe_public_http_url("https://93.184.216.34/pic.png"), "https://93.184.216.34/pic.png")
        self.assertEqual(server._unwrap_checkable_url("/api/img?u=https%3A%2F%2F93.184.216.34%2Fcover.jpg"), "https://93.184.216.34/cover.jpg")
        chapters = server._parse_vod_chapters([{"t": 120, "title": "Start"}, {"t": "15", "label": "Intro"}])
        self.assertEqual(chapters, [{"t": 15, "title": "Intro"}, {"t": 120, "title": "Start"}])
        cues = server._parse_webvtt_cues("WEBVTT\n\n00:00:01.500 --> 00:00:04.000\nПривет чат\n")
        self.assertEqual(cues, [{"start": 1.5, "end": 4.0, "text": "Привет чат"}])
        imp_rows = server._parse_import_rows("watchlist", "title,type,status,rating,year\nSteins;Gate,anime,watched,10,2011")
        self.assertEqual(len(imp_rows), 1)
        self.assertEqual(imp_rows[0]["title"], "Steins;Gate")
        zip_bytes = server._build_full_server_backup_zip()
        self.assertTrue(zip_bytes.startswith(b"PK"))

        # Проверка /api/wrapped, /api/og и Web Push API
        og_api_resp = self.client.get("/api/og?page=wrapped")
        self.assertEqual(og_api_resp.status_code, 200)
        self.assertEqual(og_api_resp.headers.get("content-type"), "image/png")
        self.assertTrue(og_api_resp.content.startswith(b"\x89PNG"))

        wrapped_resp = self.client.get("/api/wrapped")
        self.assertEqual(wrapped_resp.status_code, 200)
        self.assertIn("summary", wrapped_resp.json())
        self.assertIn("available_years", wrapped_resp.json())

        vapid_resp = self.client.get("/api/push/vapid-key")
        self.assertEqual(vapid_resp.status_code, 200)
        self.assertTrue(vapid_resp.json().get("configured"))
        self.assertTrue(vapid_resp.json().get("publicKey"))

        # Проверка защиты от SSRF в подписке на push
        bad_sub = self.client.post(
            "/api/push/subscribe",
            headers=same_orig,
            json={"subscription": {"endpoint": "https://127.0.0.1/push", "keys": {"p256dh": "abc", "auth": "def"}}, "channel": "krafen"},
        )
        self.assertEqual(bad_sub.status_code, 400)

        # Жизненный цикл подписки: subscribe -> status -> unsubscribe
        test_ep = "https://fcm.googleapis.com/fcm/send/test-sub-token-123"
        sub_ok = self.client.post(
            "/api/push/subscribe",
            headers=same_orig,
            json={"subscription": {"endpoint": test_ep, "keys": {"p256dh": "testkey1", "auth": "testauth1"}}, "channel": "danonsha"},
        )
        self.assertEqual(sub_ok.status_code, 200)
        st_on = self.client.post("/api/push/status", json={"endpoint": test_ep, "channel": "danonsha"})
        self.assertTrue(st_on.json().get("subscribed"))
        unsub_ok = self.client.post("/api/push/unsubscribe", headers=same_orig, json={"endpoint": test_ep, "channel": "danonsha"})
        self.assertEqual(unsub_ok.status_code, 200)
        st_off = self.client.post("/api/push/status", json={"endpoint": test_ep, "channel": "danonsha"})
        self.assertFalse(st_off.json().get("subscribed"))

        # Проверка PWA-манифеста, иконок и Service Worker
        man_resp = self.client.get("/manifest.webmanifest")
        self.assertEqual(man_resp.status_code, 200)
        self.assertIn("manifest+json", man_resp.headers.get("content-type", ""))
        self.assertIn("krafen", man_resp.json().get("short_name", ""))
        self.assertIn("stale-while-revalidate", man_resp.headers.get("cache-control", ""))

        self.assertEqual(self.client.get("/manifest.json").status_code, 200)
        self.assertEqual(self.client.get("/icon-192.png").status_code, 200)
        self.assertEqual(self.client.get("/icon-512.png").status_code, 200)
        self.assertEqual(self.client.get("/icon-maskable.png").status_code, 200)

        sw_resp = self.client.get("/sw.js")
        self.assertEqual(sw_resp.status_code, 200)
        self.assertIn("no-store", sw_resp.headers.get("cache-control", ""))

        # Кэширование статики: Cache-Control и ETag 304
        css_resp = self.client.get("/style.css")
        self.assertEqual(css_resp.status_code, 200)
        self.assertIn("stale-while-revalidate", css_resp.headers.get("cache-control", ""))
        css_etag = css_resp.headers.get("etag")
        self.assertTrue(css_etag)
        cond_resp = self.client.get("/style.css", headers={"if-none-match": css_etag})
        self.assertEqual(cond_resp.status_code, 304)

        img_resp = self.client.get("/favicon.png")
        self.assertEqual(img_resp.status_code, 200)
        self.assertIn("max-age=2592000", img_resp.headers.get("cache-control", ""))

        # Наличие PWA-ссылок в HTML
        index_html = self.client.get("/").text
        self.assertIn('rel="manifest"', index_html)
        self.assertIn('name="theme-color"', index_html)

        # Редирект favicon.ico и внешние короткие ссылки из _redirects
        fav_resp = self.client.get("/favicon.ico")
        self.assertEqual(fav_resp.status_code, 301)
        self.assertEqual(fav_resp.headers["location"], "/favicon.png")

        tg_resp = self.client.get("/tg?start=test")
        self.assertEqual(tg_resp.status_code, 302)
        self.assertEqual(tg_resp.headers["location"], "https://t.me/krafen?start=test")

        # Защита чувствительных файлов и закрытых директорий
        for blocked_path in ("/node_fqdn.txt", "/allocations.json", "/etc/cert/private.key", "/tests/pages.test.js", "/.env"):
            with self.subTest(blocked_path=blocked_path):
                self.assertEqual(self.client.get(blocked_path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
