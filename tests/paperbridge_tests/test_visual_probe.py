import base64
import json
import os
from pathlib import Path
import unittest

from paperbridge.visual_probe import visual_probe


class ProbeContractTests(unittest.TestCase):
    def test_image_bytes_are_private_widget_data_not_model_text(self):
        result = visual_probe()
        self.assertTrue(result.structuredContent['synthetic'])
        self.assertEqual(result.structuredContent['model_visual_access'], 'not_verified')
        self.assertTrue(result.structuredContent['requires_user_action'])
        image = result.meta['paperbridge/image']
        self.assertEqual(image['mimeType'], 'image/png')
        self.assertTrue(base64.b64decode(image['data']).startswith(b'\x89PNG'))
        self.assertNotIn(image['data'], json.dumps(result.structuredContent))
        self.assertTrue(all(c.type == 'text' for c in result.content))


@unittest.skipUnless(os.environ.get('PAPERBRIDGE_BROWSER_TESTS') == '1', 'Opt-in UI browser test')
class ProbeBrowserTests(unittest.TestCase):
    def test_user_click_uploads_png_and_attaches_returned_file_id(self):
        from playwright.sync_api import sync_playwright
        import paperbridge.visual_probe as module
        with sync_playwright() as driver:
            browser = driver.firefox.launch(headless=True)
            try:
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort())
                meta = visual_probe().meta
                page.add_init_script('window.fixtureMeta = ' + json.dumps(meta))
                page.goto('about:blank')
                page.evaluate('''() => {
                  window.uploads = 0; window.states = [];
                  window.openai = {
                    toolResponseMetadata: {mcp_tool_result: {_meta: window.fixtureMeta}},
                    uploadFile: async file => {
                      window.uploads++;
                      window.fileDetails = {name:file.name, type:file.type, size:file.size};
                      return {fileId:'file_synthetic_test'};
                    },
                    setWidgetState: state => window.states.push(state)
                  };
                }''')
                page.set_content(Path(module.__file__).with_name('visual_probe.html').read_text())
                self.assertEqual(page.evaluate('window.uploads'), 0)
                page.get_by_role('button', name='Add image to chat').click()
                page.get_by_role('button', name='Image added').wait_for()
                self.assertEqual(page.evaluate('window.uploads'), 1)
                self.assertEqual(page.evaluate('window.fileDetails.type'), 'image/png')
                self.assertGreater(page.evaluate('window.fileDetails.size'), 100)
                self.assertEqual(page.evaluate('window.states[0].imageIds'), ['file_synthetic_test'])
                self.assertTrue(page.locator('#preview').evaluate('(img) => img.complete && img.naturalWidth === 640'))
                # Restoring the component without file APIs must not claim success.
                page.evaluate('delete window.openai.uploadFile')
                page.set_content(Path(module.__file__).with_name('visual_probe.html').read_text())
                self.assertTrue(page.get_by_role('button').is_disabled())
                self.assertIn('does not expose', page.locator('#status').inner_text())
            finally:
                browser.close()
