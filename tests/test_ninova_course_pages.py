"""Course page parsers and read tools: message board, grades, attendance, online lessons.

All fixtures are synthetic and follow the markup of the live Ninova pages.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from ninova_mcp.client import NinovaError
from ninova_mcp.parsing import (
    content_area_is_empty,
    extract_attendance,
    extract_gradebook,
    extract_left_courses,
    extract_message_board,
    extract_message_reply_form,
    extract_message_thread_detail,
    extract_remote_learning,
    extract_signed_in_name,
    message_thread_id,
)
from ninova_mcp.server import NinovaMcpApp
from ninova_mcp.tracking_coverage import scope_coverage

BASE = "https://ninova.itu.edu.tr"
COURSE = f"{BASE}/Sinif/1000.2000"


def page(content: str, *, form_action: str = "/Sinif/1000.2000/MesajPanosu") -> str:
    """A Ninova page shell: header greeting, side menu and the main content column."""
    return f"""<html><head><title>ninova - İTÜ e-Öğrenim Merkezi</title></head><body>
<form name="aspnetForm" method="post" action="{form_action}" id="aspnetForm">
<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />
<input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="" />
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="state-1" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="ABCD1234" />
<div class="tasiyici"><div class="tepe"><div class="oturum"><table><tr>
<td style="text-align:right;">Hoş Geldiniz, <strong>Ada Lovelace</strong></td>
<td><a href="http://girisv3.itu.edu.tr/Logout.aspx">Oturumu Kapat</a></td></tr></table></div></div>
<div class="sol"><div class="ic"><h2>Sınıf İşlemleri</h2>
<a href="/Sinif/1000.2000/SinifBilgileri">Sınıf Bilgileri</a>
<a href="/Sinif/1000.2000/MesajPanosu">Mesaj Panosu</a></div></div>
<div class="orta"><div class="ic">
<div id="ctl00_pnlHeader"><h1>{{heading}}</h1></div>
{content}
</div></div></div></form></body></html>"""


BOARD_HTML = page("""
<div><table class="data" id="ctl00_ContentPlaceHolder1_gvMesajPanosuBasliklari">
<tr><th>Mesaj Başlığı</th><th>Son Mesaj</th></tr>
<tr><td><strong><a href='/Sinif/1000.2000/MesajPanosu/555'>Final exam</a></strong><br />
Will we get a formula sheet?&nbsp;</td>
<td><strong>Grace Hopper </strong><br /> 09 Haziran 2026 20:08</td></tr>
<tr><td><strong><a href='/Sinif/1000.2000/MesajPanosu/556'>Study group</a></strong><br />Join us</td>
<td><strong>Alan Turing</strong><br /> 10 Haziran 2026 09:00</td></tr>
</table></div>
<h1>Yeni Başlık Oluştur</h1>
<input name="ctl00$ContentPlaceHolder1$txtBaslik" type="text" id="ctl00_ContentPlaceHolder1_txtBaslik" />
<textarea name="ctl00$ContentPlaceHolder1$txtIcerik" id="ctl00_ContentPlaceHolder1_txtIcerik"></textarea>
<a id="ctl00_ContentPlaceHolder1_lbEkle" class="buton" href="javascript:WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions(&quot;ctl00$ContentPlaceHolder1$lbEkle&quot;, &quot;&quot;, true, &quot;&quot;, &quot;&quot;, false, true))"><span>Ekle</span></a>
""").replace("{heading}", "Mesaj Panosu")

EMPTY_BOARD_HTML = page("""
<p>Mesaj panosunda başlık bulunmamaktadır.</p>
<input name="ctl00$ContentPlaceHolder1$txtBaslik" type="text" id="ctl00_ContentPlaceHolder1_txtBaslik" />
""").replace("{heading}", "Mesaj Panosu")


def thread_html(posts: list[tuple[str, str, str]], *, reply_form: bool = True, thread_id: int = 555) -> str:
    rows = "".join(
        f"<tr><td>{author} <br />{when}</td><td>{body}</td></tr>" for author, when, body in posts
    )
    form = """
    <tr><td valign="top"><br /><strong>Mesaj &nbsp;</strong></td><td><br />
    <textarea name="ctl00$ContentPlaceHolder1$txtMesaj" rows="2" cols="20" id="ctl00_ContentPlaceHolder1_txtMesaj"></textarea>
    <a id="ctl00_ContentPlaceHolder1_lbMesajEkle" class="buton" href="javascript:WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions(&quot;ctl00$ContentPlaceHolder1$lbMesajEkle&quot;, &quot;&quot;, true, &quot;&quot;, &quot;&quot;, false, true))"><span>Cevapla</span></a>
    <a id="ctl00_ContentPlaceHolder1_LinkButton1" class="butonIptal" href="javascript:WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions(&quot;ctl00$ContentPlaceHolder1$LinkButton1&quot;, &quot;&quot;, true, &quot;&quot;, &quot;&quot;, false, true))"><span>Temizle</span></a>
    </td></tr>""" if reply_form else ""
    content = f"""<table class="data"><tbody>
    <tr><th>Gönderen</th><th>Mesaj</th></tr>{rows}{form}</tbody></table>"""
    return page(content, form_action=f"/Sinif/1000.2000/MesajPanosu/{thread_id}").replace("{heading}", "Final exam")


THREAD_HTML = thread_html([
    ("Grace Hopper", "09 Haziran 2026 20:08", "<p>Will we get a formula sheet?&nbsp;</p>"),
    ("Prof. Example", "10 Haziran 2026 10:15",
     '<p>Yes, one page. See <a href="https://example.edu/rules.pdf">the rules</a>.</p>'),
])

GRADES_HTML = page("""
<table class="data" width="100%"><tr>
<th>Ada Lovelace</th><th>Not</th><th>Açıklama</th></tr>
<tr><td><input type="hidden" name="ctl00$ContentPlaceHolder1$rpGalileoNot$ctl00$hdnObjectId" value="9001" />
<span id="eas9001">Midterm</span>
<script type="text/javascript">var element = document.getElementById('eas9001'); var title = 'Midterm';
var body = '<strong>Not Yüzdesi </strong><span>%40,00</span><br style="clear:both;" />';
body += '<strong>Ortalama </strong><span>62,78</span><br style="clear:both;" />';
body += '<strong>Standart Sapma </strong><span>26,10</span><br style="clear:both;" />';
body += '<strong>Öğrenci Sayısı </strong><span>18</span><br style="clear:both;" />';
body += '<strong>Sıralamanız </strong><span>3</span><br style="clear:both;" />';
new Tip(element, body, {});</script></td>
<td><span>71,50</span></td><td><span>Good</span></td></tr>
<tr><td><input type="hidden" name="ctl00$ContentPlaceHolder1$rpGalileoNot$ctl01$hdnObjectId" value="9002" />
<span id="eas9002">Project</span>
<script type="text/javascript">var body = '<strong>Not Yüzdesi </strong><span>%60,00</span>';
body += '<strong>Sıralamanız </strong><span>0</span>';</script></td>
<td><span>-</span></td><td><span></span></td></tr>
<tr><td>Ağırlıklı Ortalamanız</td><td> 28,60 </td><td>&nbsp;</td></tr>
</table>""").replace("{heading}", "Notlar")

ATTENDANCE_HTML = page("""
<span class="uyari">Akademisyenler yoklama sistemi olarak Kepler uygulamasını da Ninova uygulamasını da kullanabilmektedirler.</span>
<h4>Ninova Yoklama Bilgileri</h4>
<table class="data"><tr><th>Öğrenci</th><th>01.10.2026</th></tr>
<tr><td>Ada Lovelace</td><td>Var</td></tr></table>
<h4>Kepler Yoklama Bilgileri</h4>
<table class="data"><tr><th>Hafta</th><th><span>Pazartesi</span></th></tr>
<tr><td>1. Hafta </td><td><span>1</span> <span>1</span></td></tr>
<tr><td>2. Hafta </td><td><span>1</span> <span>0</span></td></tr>
<tr><td>3. Hafta </td><td><span>0</span> <span>0</span></td></tr>
</table>""").replace("{heading}", "Öğrencinin Yoklama Detayları")

REMOTE_HTML = page("""
<h3>Aktif Uzaktan Eğitim Oturumlarınız</h3>
<div><table class="data" id="ctl00_ContentPlaceHolder1_gvMeetingList">
<tr><th>Başlık</th><th>Başlangıç</th><th>Katıl</th></tr>
<tr><td>Week 3 lecture</td><td>14 Ekim 2026 10:00</td>
<td><a href="https://itu-edu-tr.zoom.us/j/123">Katıl</a></td></tr>
<tr><td>Week 3 lab</td><td>15 Ekim 2026 10:00</td>
<td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$gvMeetingList$ctl03$lbKatil','')">Katıl</a></td></tr>
</table></div>
<h3>Sınıfın Geçmiş Uzaktan Eğitim Oturumları</h3>
<div><table class="data" id="ctl00_ContentPlaceHolder1_gvGecmisMeetings">
<tr><th>Başlık</th><th>Başlangıç</th><th>Kayıt</th></tr>
<tr><td>Week 2 lecture</td><td>07 Ekim 2026 10:00</td>
<td><span class="buton" onclick="PlayVimeoClick(this)" data-vimeo="https://vimeo.com/111222333/abc" data-pass="s3cret">İzle</span></td></tr>
</table></div>""").replace("{heading}", "Uzaktan Eğitim Oturumları")

EMPTY_ANNOUNCEMENTS_HTML = page("").replace("{heading}", "Duyurular")

LEFT_HTML = page("""
<table class="data" id="dataTableDocuments">
<tr><th><strong>İşlem</strong></th><th><strong>Sınıf Adı</strong></th><th><strong>Ders Adı</strong></th>
<th><strong>Başlangıç Tarihi </strong></th><th><strong>Bitiş Tarihi </strong></th>
<th><strong>Sınıftan Ayrılış Tarihi </strong></th></tr>
<tr><td><input type="submit" name="ctl00$ContentPlaceHolder1$rp$ctl01$btnSinifaDon" value="Sınıfa Dön" /></td>
<td>CRN: 10077</td><td>ABC 121</td><td>22.09.2025</td><td>02.01.2026</td><td>09.02.2026</td></tr>
</table>""").replace("{heading}", "Ayrıldığım Sınıflar")


class MessageBoardParsingTests(unittest.TestCase):
    def test_board_lists_topics_and_create_form(self) -> None:
        board = extract_message_board(BOARD_HTML, COURSE + "/MesajPanosu", BASE)
        self.assertEqual(board["count"], 2)
        self.assertTrue(board["can_create_topic"])
        topic = board["topics"][0]
        self.assertEqual(topic["title"], "Final exam")
        self.assertEqual(message_thread_id(topic["url"]), "555")
        self.assertEqual(topic["last_message_author"], "Grace Hopper")
        self.assertEqual(topic["last_message_at_iso"], "2026-06-09T20:08:00+03:00")

    def test_empty_board_is_complete(self) -> None:
        board = extract_message_board(EMPTY_BOARD_HTML, COURSE + "/MesajPanosu", BASE)
        self.assertEqual(board["topics"], [])
        observed = scope_coverage(EMPTY_BOARD_HTML, COURSE + "/MesajPanosu", COURSE + "/MesajPanosu",
                                  "message_board", board)
        self.assertEqual(observed["status"], "complete")

    def test_thread_posts_skip_the_reply_row(self) -> None:
        thread = extract_message_thread_detail(THREAD_HTML, COURSE + "/MesajPanosu/555", BASE)
        self.assertEqual(thread["thread_id"], "555")
        self.assertEqual(thread["title"], "Final exam")
        self.assertEqual(thread["count"], 2)
        self.assertTrue(thread["can_reply"])
        second = thread["posts"][1]
        self.assertEqual(second["index"], 2)
        self.assertEqual(second["author"], "Prof. Example")
        self.assertEqual(second["sent_at_iso"], "2026-06-10T10:15:00+03:00")
        self.assertEqual(second["links"], [{"text": "the rules", "url": "https://example.edu/rules.pdf"}])

    def test_reply_form(self) -> None:
        form = extract_message_reply_form(THREAD_HTML)
        self.assertEqual(form["text_field"], "ctl00$ContentPlaceHolder1$txtMesaj")
        self.assertEqual(form["event_target"], "ctl00$ContentPlaceHolder1$lbMesajEkle")
        self.assertEqual(form["action"], "/Sinif/1000.2000/MesajPanosu/555")
        self.assertEqual(form["values"]["__VIEWSTATE"], "state-1")
        self.assertIsNone(extract_message_reply_form(thread_html([], reply_form=False)))
        self.assertFalse(extract_message_thread_detail(thread_html([], reply_form=False),
                                                       COURSE + "/MesajPanosu/555", BASE)["can_reply"])
        self.assertEqual(extract_signed_in_name(THREAD_HTML), "Ada Lovelace")


class CoursePageParsingTests(unittest.TestCase):
    def test_grade_statistics(self) -> None:
        grades = extract_gradebook(GRADES_HTML, COURSE + "/Notlar", BASE)
        self.assertEqual(grades["weighted_average"], "28,60")
        self.assertEqual(grades["grades"][0], {"title": "Midterm", "score": "71,50", "description": "Good"})
        midterm, project = grades["statistics"]
        self.assertEqual(midterm, {"index": 0, "title": "Midterm", "item_id": "9001", "weight_percent": 40.0,
                                   "class_average": 62.78, "standard_deviation": 26.1,
                                   "student_count": 18, "rank": 3})
        self.assertEqual(project["weight_percent"], 60.0)
        self.assertIsNone(project["rank"])

    def test_attendance_notes_presence_and_native_table(self) -> None:
        attendance = extract_attendance(ATTENDANCE_HTML, COURSE + "/Yoklama", BASE)
        self.assertEqual(attendance["source_system"], "kepler")
        self.assertEqual(attendance["count"], 3)
        self.assertEqual(attendance["weeks_with_presence"], 2)
        self.assertEqual(attendance["total_present_marks"], 3)
        self.assertIn("future weeks", attendance["marks_note"])
        self.assertEqual(attendance["ninova_table"], {"headers": ["Öğrenci", "01.10.2026"],
                                                      "rows": [["Ada Lovelace", "Var"]]})

    def test_remote_learning_join_links_and_recordings(self) -> None:
        remote = extract_remote_learning(REMOTE_HTML, COURSE + "/UzaktanEgitim", BASE)
        lecture, lab = remote["active_sessions"]
        self.assertEqual(lecture["meeting_url"], "https://itu-edu-tr.zoom.us/j/123")
        self.assertEqual(lecture["start_at"], "2026-10-14T10:00:00+03:00")
        self.assertNotIn("meeting_url", lab)
        self.assertNotIn("Katıl_url", lab)
        self.assertEqual(lab["join_via_ninova_page"], COURSE + "/UzaktanEgitim")
        recording = remote["past_sessions"][0]
        self.assertEqual(recording["recording_url"], "https://vimeo.com/111222333/abc")
        self.assertEqual(recording["recording_password"], "s3cret")
        self.assertIn("10 days", remote["recording_note"])

    def test_empty_announcements_page_is_recognized(self) -> None:
        self.assertTrue(content_area_is_empty(EMPTY_ANNOUNCEMENTS_HTML))
        self.assertFalse(content_area_is_empty(BOARD_HTML))
        observed = scope_coverage(EMPTY_ANNOUNCEMENTS_HTML, COURSE + "/Duyurular", COURSE + "/Duyurular",
                                  "announcements", {"announcements": []})
        self.assertEqual(observed, {"status": "complete", "reason": "empty_content_area"})

    def test_left_courses(self) -> None:
        self.assertEqual(extract_left_courses(LEFT_HTML, BASE + "/Kampus?1/AyrildigimSiniflar"), [{
            "class_name": "CRN: 10077", "course": "ABC 121", "start_date": "22.09.2025",
            "end_date": "02.01.2026", "left_at": "09.02.2026", "crn": "10077",
        }])


class FakeNinova:
    """Serves synthetic course pages by URL."""

    base_url = BASE

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def get_html(self, url: str):
        url = url if url.startswith("http") else BASE + url
        self.requests.append(url)
        if url not in self.pages:
            raise NinovaError(f"unexpected request {url}")
        return self.pages[url], SimpleNamespace(url=url, headers={"Content-Type": "text/html"})


def app_with(pages: dict[str, str]) -> tuple[NinovaMcpApp, FakeNinova]:
    app = NinovaMcpApp()
    fake = FakeNinova(pages)
    app._client = fake  # type: ignore[assignment]
    app._course_cache.set("courses", [{"code": "ABC 101", "title": "Example", "url": COURSE}])
    return app, fake


class CourseReadToolTests(unittest.TestCase):
    def test_message_board_and_thread_tools(self) -> None:
        app, _ = app_with({COURSE + "/MesajPanosu": BOARD_HTML, COURSE + "/MesajPanosu/555": THREAD_HTML})
        board = app.get_course_message_board("ABC 101")
        self.assertEqual([topic["thread_id"] for topic in board["topics"]], ["555", "556"])
        self.assertTrue(board["can_create_topic"])
        for reference in ("555", COURSE + "/MesajPanosu/555", "/Sinif/1000.2000/MesajPanosu/555#x", "final EXAM"):
            with self.subTest(reference=reference):
                thread = app.get_course_message_thread("ABC 101", reference)
                self.assertEqual(thread["thread_id"], "555")
                self.assertEqual(thread["course"]["code"], "ABC 101")

    def test_thread_reference_must_belong_to_the_course(self) -> None:
        app, fake = app_with({COURSE + "/MesajPanosu": BOARD_HTML})
        for reference in ("/Sinif/9.9/MesajPanosu/555", "Unknown topic", " "):
            with self.subTest(reference=reference), self.assertRaises(NinovaError):
                app.get_course_message_thread("ABC 101", reference)
        self.assertNotIn(BASE + "/Sinif/9.9/MesajPanosu/555", fake.requests)

    def test_grades_tool_merges_statistics_by_position(self) -> None:
        app, _ = app_with({COURSE + "/Notlar": GRADES_HTML})
        grades = app.get_course_grades("ABC 101")
        self.assertNotIn("statistics", grades)
        self.assertEqual(grades["grades"][0]["class_average"], 62.78)
        self.assertEqual(grades["grades"][0]["item_id"], "9001")
        self.assertEqual(grades["grades"][1]["weight_percent"], 60.0)

    def test_left_courses_tool_and_quiet_empty_announcements(self) -> None:
        app, _ = app_with({BASE + "/Kampus?1/AyrildigimSiniflar": LEFT_HTML,
                           COURSE + "/Duyurular": EMPTY_ANNOUNCEMENTS_HTML})
        self.assertEqual(app.get_left_courses()["courses"][0]["crn"], "10077")
        announcements = app.get_course_announcements("ABC 101")
        self.assertEqual(announcements["count"], 0)
        self.assertNotIn("parse_warning", announcements)


if __name__ == "__main__":
    unittest.main()
