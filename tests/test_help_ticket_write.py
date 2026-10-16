from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ninova_mcp.client import NinovaError
from ninova_mcp.help_ticket_write import DRAFT_TTL_SECONDS, HelpTicketWriter, parse_form, parse_units

P = "ctl00$ctl00$ctl00$cphMain$pageMain$pageDetailMain$"
I = P.replace("$", "_")
CATEGORIES = {1271: "İTÜ/VPN Hizmeti", 52: "Diğer Servis ve Hizmetler Hakkında"}
SUBCATEGORIES = {1271: {1273: "VPN Erişim Sorunu", 1274: "Emekli Akademisyen VPN Hizmeti"}, 52: {}}
EXTRAS = {1273: [("22", "text", "VPN hizmeti aktif mi?", True), ("253", "file", "Ekran görüntüsü", False)]}


def _select(name: str, options: dict[int, str], selected: int | None, placeholder: str) -> str:
    rows = [f'<option value="0">{placeholder}</option>'] + [
        f'<option value="{key}"{" selected" if key == selected else ""}>{label}</option>'
        for key, label in options.items()
    ]
    return f'<select name="{P}{name}" id="{I}{name}" onchange="postback()">{"".join(rows)}</select>'


def form_page(category: int | None = None, subcategory: int | None = None, tickets: tuple[int, ...] = (900,)) -> str:
    parts = [_select("CtrlTalepKonusuId", CATEGORIES, category, "Kategori Seçimi")]
    if category and SUBCATEGORIES[category]:
        parts.append(_select("CtrlTalepAltKonusuId", SUBCATEGORIES[category], subcategory, "Alt Kategori Seçiniz"))
    scripts = []
    for index, (key, kind, label, required) in enumerate(EXTRAS.get(subcategory or 0, [])):
        prefix = f"{P}ctl02$rptDataParameter$ctl0{index}$"
        control_id = f"{I}ctl02_rptDataParameter_ctl0{index}_ctl00_" + ("fuValue" if kind == "file" else "tbValue")
        validator = control_id.rsplit("_", 1)[0] + "_rvData"
        parts.append(f'<input type="hidden" name="{prefix}hfObjectId" value="{key}">')
        if kind == "file":
            parts.append(f'<div><span>{label}</span><input type="file" name="{prefix}ctl00$fuValue" id="{control_id}"></div>')
        else:
            parts.append(f'<div><input type="text" name="{prefix}ctl00$tbValue" id="{control_id}" placeholder="{label}"></div>')
        scripts.append(f'{validator}.controltovalidate = "{control_id}";')
        if not required:
            scripts.append(f'{validator}.enabled = "False";')
    links = "".join(f'<a href="bilet.aspx?id={ticket}">Bilet No {ticket}</a>' for ticket in tickets)
    return f"""<html><body>{links}
    <form method="post" action="./biletolustur.aspx?birim=1&amp;kid=0&amp;akid=0" enctype="multipart/form-data">
    <input type="hidden" name="__VIEWSTATE" value="state-{category}-{subcategory}">
    <input type="hidden" name="__EVENTTARGET" value="">
    <p>Sayın Ada Lovelace, ada@itu.edu.tr e-posta adresinizle bir bilet oluşturuyorsunuz.</p>
    {''.join(parts)}
    <input type="text" name="{P}CtrlTitle" id="{I}CtrlTitle">
    <textarea name="{P}CtrlDescription" id="{I}CtrlDescription"></textarea>
    <input type="file" name="{P}FileUpload1" id="{I}FileUpload1" multiple>
    <input type="submit" name="{P}btnSubmit" value="Yardım Biletini Oluştur">
    </form><script>{' '.join(scripts)}</script></body></html>"""


class FakeHelpDesk:
    def __init__(self, result_url: str | None = None, result_tickets: tuple[int, ...] = (900, 901)) -> None:
        self.posts: list[dict] = []
        self.result_url = result_url
        self.result_tickets = result_tickets

    def get_html(self, url: str):
        if "birimler.aspx" in url:
            html = '<a href="birim.aspx?birim=1&bilgi">Bilgi İşlem Daire Başkanlığı</a><a href="birim.aspx?birim=2&x">Öğrenci İşleri</a>'
            return html, SimpleNamespace(url=url)
        return form_page(), SimpleNamespace(url="https://yardim.itu.edu.tr/biletolustur.aspx?birim=1&kid=0&akid=0")

    def post_multipart(self, url, *, data, files, referer=None, timeout=120):
        self.posts.append({"url": url, "data": dict(data), "files": list(files)})
        target = data.get("__EVENTTARGET")
        page = "https://yardim.itu.edu.tr/biletolustur.aspx?birim=1&kid=0&akid=0"
        if target == P + "CtrlTalepKonusuId":
            return SimpleNamespace(text=form_page(int(data[target])), url=page)
        if target == P + "CtrlTalepAltKonusuId":
            return SimpleNamespace(text=form_page(int(data[P + "CtrlTalepKonusuId"]), int(data[target])), url=page)
        assert P + "btnSubmit" in data
        if self.result_url:
            return SimpleNamespace(text="<p>ok</p>", url=self.result_url)
        return SimpleNamespace(text=form_page(tickets=self.result_tickets), url=page)


class HelpTicketParsingTests(unittest.TestCase):
    def test_units(self) -> None:
        html = FakeHelpDesk().get_html("https://yardim.itu.edu.tr/birimler.aspx")[0]
        self.assertEqual(parse_units(html), [{"id": 1, "name": "Bilgi İşlem Daire Başkanlığı"},
                                             {"id": 2, "name": "Öğrenci İşleri"}])

    def test_form_fields_requiredness_and_requester(self) -> None:
        form = parse_form(form_page(1271, 1273))
        self.assertEqual(form["category"], 1271)
        self.assertEqual(form["subcategory"], 1273)
        self.assertEqual([(item["key"], item["type"], item["label"], item["required"]) for item in form["extra_fields"]],
                         [("22", "text", "VPN hizmeti aktif mi?", True), ("253", "file", "Ekran görüntüsü", False)])
        self.assertEqual(form["requester"], {"name": "Ada Lovelace", "email": "ada@itu.edu.tr"})
        self.assertEqual(form["listed_ticket_ids"], {900})
        self.assertIsNone(parse_form(form_page(52))["subcategories"] or None)

    def test_non_form_page_is_rejected(self) -> None:
        with self.assertRaisesRegex(NinovaError, "ticket form"):
            parse_form("<html><a>Giriş</a></html>")


class HelpTicketWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000.0
        self.desk = FakeHelpDesk()
        self.writer = HelpTicketWriter(self.desk, clock=lambda: self.now)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def prepare(self, **overrides):
        values = dict(unit_id=1, category_id=1271, subcategory_id=1273, title="VPN bağlanmıyor",
                      description="Satır 1\nSatır 2", extra_fields={"22": "Evet"})
        values.update(overrides)
        return self.writer.prepare(values.pop("unit_id"), values.pop("category_id"), values.pop("title"),
                                   values.pop("description"), **values)

    def test_options_walk_the_form(self) -> None:
        self.assertEqual(len(self.writer.options()["units"]), 2)
        self.assertEqual([item["id"] for item in self.writer.options(1)["categories"]], [1271, 52])
        self.assertEqual([item["id"] for item in self.writer.options(1, 1271)["subcategories"]], [1273, 1274])
        fields = self.writer.options(1, 1271, 1273)["extra_fields"]
        self.assertEqual(fields[0], {"key": "22", "label": "VPN hizmeti aktif mi?", "type": "text", "required": True})
        self.assertFalse(any(post["data"].get(P + "btnSubmit") for post in self.desk.posts))

    def test_prepare_previews_without_submitting(self) -> None:
        draft = self.prepare()
        preview = draft["preview"]
        self.assertEqual((preview["category"], preview["subcategory"]), ("İTÜ/VPN Hizmeti", "VPN Erişim Sorunu"))
        self.assertEqual(preview["description"], "Satır 1\nSatır 2")
        self.assertEqual(preview["extra_fields"][0]["value"], "Evet")
        self.assertEqual(preview["requester"]["email"], "ada@itu.edu.tr")
        self.assertFalse(any(P + "btnSubmit" in post["data"] for post in self.desk.posts))

    def test_submit_fills_a_fresh_form_once(self) -> None:
        attachment = self.root / "error.png"
        attachment.write_bytes(b"\x89PNG\r\n\x1a\n data")
        shot = self.root / "shot.jpg"
        shot.write_bytes(b"\xff\xd8\xff data")
        draft = self.prepare(attachments=[str(attachment)], extra_fields={"22": "Evet", "253": [str(shot)]})
        self.desk.posts.clear()
        result = self.writer.submit(draft["draft_id"], True)
        self.assertEqual(result, {"submitted": True, "ticket_id": 901, "url": "https://yardim.itu.edu.tr/bilet.aspx?id=901"})
        final = self.desk.posts[-1]
        self.assertEqual(final["data"][P + "CtrlTitle"], "VPN bağlanmıyor")
        self.assertEqual(final["data"][P + "CtrlDescription"], "Satır 1\nSatır 2")
        self.assertEqual(final["data"][P + "ctl02$rptDataParameter$ctl00$ctl00$tbValue"], "Evet")
        self.assertEqual(final["data"][P + "CtrlTalepAltKonusuId"], "1273")
        self.assertEqual(final["data"]["__VIEWSTATE"], "state-1271-1273")
        self.assertEqual([(name, file[0]) for name, file in final["files"]],
                         [(P + "FileUpload1", "error.png"), (P + "ctl02$rptDataParameter$ctl01$ctl00$fuValue", "shot.jpg")])
        self.assertEqual(sum(P + "btnSubmit" in post["data"] for post in self.desk.posts), 1)
        with self.assertRaisesRegex(NinovaError, "Unknown or expired"):
            self.writer.submit(draft["draft_id"], True)

    def test_ticket_id_from_redirect(self) -> None:
        self.desk.result_url = "https://yardim.itu.edu.tr/bilet.aspx?id=1060000"
        self.assertEqual(self.writer.submit(self.prepare()["draft_id"], True)["ticket_id"], 1060000)

    def test_unclear_result_is_uncertain(self) -> None:
        self.desk.result_tickets = (900,)
        result = self.writer.submit(self.prepare()["draft_id"], True)
        self.assertEqual(result["submitted"], "uncertain")

    def test_confirmation_and_expiry(self) -> None:
        draft = self.prepare()
        for value in (False, "true", 1):
            with self.assertRaisesRegex(NinovaError, "confirm=true"):
                self.writer.submit(draft["draft_id"], value)  # type: ignore[arg-type]
        self.now += DRAFT_TTL_SECONDS + 1
        with self.assertRaisesRegex(NinovaError, "Unknown or expired"):
            self.writer.submit(draft["draft_id"], True)

    def test_validation(self) -> None:
        cases = [
            ({"title": "a <script>"}, "< or >"),
            ({"description": ""}, "title and a description"),
            ({"category_id": 999}, "not offered by this unit"),
            ({"subcategory_id": 999}, "not offered by this category"),
            ({"subcategory_id": None}, "requires subcategory_id"),
            ({"extra_fields": {}}, "is required"),
            ({"extra_fields": {"22": "x", "77": "y"}}, "Unknown extra field"),
            ({"extra_fields": {"22": "x", "253": "/etc/passwd"}}, "list of file paths"),
            ({"extra_fields": {"22": ["x"]}}, "text field"),
            ({"attachments": ["/etc/passwd"]}, "can be attached"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(NinovaError, message):
                self.prepare(**overrides)
        with self.assertRaisesRegex(NinovaError, "no subcategories"):
            self.prepare(category_id=52, subcategory_id=1273, extra_fields=None)

    def test_category_without_subcategories(self) -> None:
        draft = self.prepare(category_id=52, subcategory_id=None, extra_fields=None)
        self.assertIsNone(draft["preview"]["subcategory"])

    def test_operator_switch(self) -> None:
        import os
        from unittest.mock import patch

        from ninova_mcp.server import NinovaMcpApp

        app = NinovaMcpApp()
        with patch.dict(os.environ, {"NINOVA_HELP_TICKET_WRITES": "0"}):
            with self.assertRaisesRegex(NinovaError, "disabled"):
                app.help_prepare_ticket(1, 1271, "t", "d")
            with self.assertRaisesRegex(NinovaError, "disabled"):
                app.help_submit_ticket("x", True)


class HelpTicketRemoteTests(unittest.TestCase):
    def test_remote_needs_writes(self) -> None:
        import os
        from unittest.mock import patch

        from ninova_mcp import remote

        def names(writes: bool, **env: str) -> set[str]:
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(remote, "remote_file_capabilities", return_value=(True, writes)):
                return set(remote._selected_remote_tool_names())

        tools = {"help_prepare_ticket", "help_submit_ticket"}
        self.assertTrue(tools <= names(True, NINOVA_HELP_TICKET_WRITES="1"))
        self.assertIn("help_ticket_options", names(False))
        self.assertFalse(tools & names(False, NINOVA_HELP_TICKET_WRITES="1"))
        self.assertFalse(tools & names(True, NINOVA_HELP_TICKET_WRITES="0"))

    def test_remote_files_must_be_managed(self) -> None:
        import os
        from unittest.mock import patch

        from ninova_mcp.remote_files import RemoteNinovaMcpApp

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with patch.dict(os.environ, {"NINOVA_REMOTE_FILE_ROOT": str(root)}):
                os.environ.pop("NINOVA_REMOTE_REQUIRED_MOUNT", None)
                app = RemoteNinovaMcpApp()
            (root / "uploads" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n x")
            (root / "outside.png").write_bytes(b"\x89PNG\r\n\x1a\n x")
            app._help_writer = HelpTicketWriter(FakeHelpDesk())
            draft = app.help_prepare_ticket(1, 1271, "t", "d", subcategory_id=1273,
                                            extra_fields={"22": "x", "253": ["uploads/shot.png"]},
                                            attachments=["uploads/shot.png"])
            self.assertEqual(draft["preview"]["attachments"][0]["name"], "shot.png")
            for fields, attachments in (({"22": "x"}, [str(root / "outside.png")]),
                                        ({"22": "x", "253": ["../outside.png"]}, None),
                                        ({"22": "x", "253": str(root / "outside.png")}, None)):
                with self.subTest(fields=fields, attachments=attachments), self.assertRaises(NinovaError):
                    app.help_prepare_ticket(1, 1271, "t", "d", subcategory_id=1273,
                                            extra_fields=fields, attachments=attachments)


if __name__ == "__main__":
    unittest.main()
