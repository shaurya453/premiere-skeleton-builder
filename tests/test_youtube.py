import tempfile
import unittest
from pathlib import Path
from lxml import etree as ET

from skeleton_builder import FPS, read_docx, xml_sequence
from youtube_media import youtube_id, source_range, source_window, place_video_clips


class YouTubeTests(unittest.TestCase):
    def test_video_id_rejects_foreign_hosts_and_playlists(self):
        self.assertEqual(youtube_id('https://youtu.be/fftGair1ZoA?t=135'), 'fftGair1ZoA')
        self.assertEqual(youtube_id('https://www.youtube.com/watch?v=fftGair1ZoA&list=foo'), 'fftGair1ZoA')
        for url in ['https://youtube.com.evil.example/watch?v=fftGair1ZoA',
                    'https://www.youtube.com/playlist?list=foo', 'file:///fftGair1ZoA']:
            with self.assertRaises(ValueError):
                youtube_id(url)

    def test_source_range_and_clamped_handles(self):
        self.assertEqual(source_range('2:15 - 2:21'), (135, 141))
        self.assertEqual(source_range('0:54–0:55'), (54, 55))
        self.assertEqual(source_range('1:02:03 — 1:02:05'), (3723, 3725))
        self.assertEqual(source_range('0:00 to 0:07'), (0, 7))
        self.assertEqual(source_range('5:19 to 5:23'), (319, 323))
        self.assertEqual(source_window([(135,141)],333,30,False), (105,171))
        self.assertEqual(source_window([(6,10),(90,95)],100,30,False), (0,100))
        self.assertEqual(source_window([(135,141)],333,30,True), (0,333))
        for label in ['0:99 - 1:00','2:21 - 2:15','2:15']:
            with self.assertRaises(ValueError):
                source_range(label)

    def test_exact_selects_preserved_when_narration_is_shorter(self):
        asset = {'kind':'video','path':'source.mp4','title':'Example','width':1920,'height':1080,
                 'source_duration_frames':6000,'has_audio':True}
        cues = [{'kind':'video','start':10,'end':15,'passage':'Narration','review':[],
                 'refs':[{'label':'2:06 - 2:19','target':'https://youtu.be/fftGair1ZoA',
                          'source_start':126,'source_end':139,'video_asset':asset,
                          'in_frame':round(30*FPS),'out_frame':round(43*FPS)}]}]
        edits, selects = place_video_clips(cues, [], 9000)
        self.assertEqual(edits[0]['end_frame']-edits[0]['start_frame'],round(15*FPS)-round(10*FPS))
        self.assertEqual(selects[0]['end_frame']-selects[0]['start_frame'],round(43*FPS)-round(30*FPS))
        self.assertEqual(edits[0]['in_frame'],round(30*FPS))
        self.assertTrue(any('trimmed' in x for x in cues[0]['review']))

    def test_insert_cues_are_skipped_not_placed_as_overlays(self):
        # A hard-insert cue's start==end (a splice point, not a matched passage) would
        # otherwise collapse to a zero-duration, invisible placement under the normal
        # narration-capped `take` math - build() splices it into the timeline separately.
        asset = {'kind': 'video', 'path': 'source.mp4', 'title': 'Example', 'width': 1920,
                 'height': 1080, 'source_duration_frames': 6000, 'has_audio': True}
        cues = [{'kind': 'video', 'start': 10, 'end': 10, 'passage': '(insert)', 'review': [],
                 'insert': True,
                 'refs': [{'label': '2:06 - 2:19', 'target': 'https://youtu.be/fftGair1ZoA',
                           'source_start': 126, 'source_end': 139, 'video_asset': asset,
                           'in_frame': round(30 * FPS), 'out_frame': round(43 * FPS)}]}]
        edits, selects = place_video_clips(cues, [], 9000)
        self.assertEqual(edits, [])
        self.assertEqual(selects, [])

    def _prepare_point_ref(self, point, duration, handles=10):
        # Runs prepare_sources for one "?t=" link with the download and ffmpeg steps stubbed,
        # so only the window/handle/focus math is exercised.
        from unittest.mock import patch
        import youtube_media
        ref = {'label': 'clip', 'target': 'https://youtu.be/fftGair1ZoA?t=%d' % point, 'point_start': float(point)}
        cue = {'kind': 'video', 'refs': [ref], 'review': []}
        info = {'id': 'fftGair1ZoA', 'title': 'Example', 'duration': duration, 'source_url': ref['target'],
                'download_offset_seconds': 0, 'download_end_seconds': duration}
        # probe() reports the prepared window, not the whole video
        prepared = min(duration, point + 15 + handles) - max(0, point - 15 - handles)
        frames = round(prepared * FPS)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(youtube_media, 'download_source', return_value=(Path(tmp) / 'o.mp4', info)), \
             patch.object(youtube_media, 'probe', return_value={'duration': prepared, 'frames': frames, 'has_audio': True}), \
             patch.object(youtube_media.subprocess, 'run'):
            youtube_media.prepare_sources([cue], tmp, handles=handles, full=False, cache=Path(tmp) / 'cache')
        return cue, ref

    def test_point_link_becomes_a_30_second_window_with_handles(self):
        cue, ref = self._prepare_point_ref(100, 600)
        self.assertEqual((ref['source_start'], ref['source_end']), (85, 115))
        # 10s handles: the prepared source runs 75s-125s, so the window sits 10s in.
        self.assertEqual(ref['in_frame'], round(10 * FPS))
        self.assertEqual(ref['out_frame'], round(40 * FPS))
        self.assertEqual(ref['focus_frame'], round(25 * FPS))

    def test_point_link_window_clamps_at_start_and_end_of_video(self):
        _, early = self._prepare_point_ref(5, 600)
        self.assertEqual((early['source_start'], early['source_end']), (0, 20))
        self.assertEqual(early['in_frame'], 0)
        self.assertEqual(early['focus_frame'], round(5 * FPS))
        cue, late = self._prepare_point_ref(100, 110)
        self.assertEqual(late['out_frame'], round(35 * FPS))
        self.assertTrue(any('clamped' in note for note in cue['review']))

    def test_point_link_overlay_starts_at_the_pointed_moment_but_selects_keep_the_window(self):
        asset = {'kind': 'video', 'path': 'source.mp4', 'title': 'Example', 'width': 1920,
                 'height': 1080, 'source_duration_frames': 6000, 'has_audio': True}
        cues = [{'kind': 'video', 'start': 10, 'end': 16, 'passage': 'Narration', 'review': [],
                 'refs': [{'label': 'clip', 'target': 'https://youtu.be/fftGair1ZoA?t=100',
                           'point_start': 100.0, 'source_start': 85, 'source_end': 115, 'video_asset': asset,
                           'in_frame': round(10 * FPS), 'out_frame': round(40 * FPS),
                           'focus_frame': round(25 * FPS)}]}]
        edits, selects = place_video_clips(cues, [], 9000)
        self.assertEqual(edits[0]['in_frame'], round(25 * FPS))
        self.assertEqual(edits[0]['requested_source_start'], 100.0)
        self.assertEqual(edits[0]['end_frame'] - edits[0]['start_frame'], round(16 * FPS) - round(10 * FPS))
        self.assertEqual(selects[0]['in_frame'], round(10 * FPS))
        self.assertEqual(selects[0]['end_frame'] - selects[0]['start_frame'], round(40 * FPS) - round(10 * FPS))

    def test_xml_keeps_source_handles_and_linked_disabled_sound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = {'kind':'video','name':'Example','path':str(root/'source.mp4'),
                    'width':1920,'height':1080,'has_audio':True,'source_duration_frames':2000,
                    'in_frame':900,'start_frame':10,'end_frame':190}
            path=root/'test.xml'
            xml_sequence(path,'Video test',[item],[],[],root/'voice.wav',20,1920,1080)
            tree=ET.parse(str(path))
            video=tree.find('.//video/track/clipitem')
            self.assertEqual(video.findtext('in'),'900')
            self.assertEqual(video.findtext('out'),'1080')
            self.assertEqual(video.findtext('duration'),'2000')
            self.assertIsNone(video.find('stillframe'))
            self.assertEqual(len(video.findall('link')),3)
            tracks=tree.findall('.//sequence/media/audio/track')
            self.assertEqual(len(tracks),3)
            for t in tracks[1:]:
                self.assertEqual(t.findtext('clipitem/enabled'),'FALSE')
                self.assertEqual(t.findtext('clipitem/in'),'900')
                self.assertEqual(t.findtext('clipitem/out'),'1080')

    def test_video_cue_spans_whole_preceding_paragraph(self):
        from zipfile import ZipFile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            text='The campaign had a phone number. Calling it played a laugh. ('
            xml=f'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>{text}</w:t></w:r><w:hyperlink r:id="v"><w:r><w:t>2:15 - 2:21</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p></w:body></w:document>'''
            rels='<Relationships><Relationship Id="v" Target="https://youtu.be/fftGair1ZoA?t=135"/></Relationships>'
            with ZipFile(root/'input.docx','w') as z:
                z.writestr('word/document.xml',xml)
                z.writestr('word/_rels/document.xml.rels',rels)
            _,cues,_,_=read_docx(root/'input.docx',root/'assets')
            self.assertEqual(cues[0]['script_start'],0)
            self.assertIn('The campaign had a phone number.',cues[0]['passage'])


if __name__=='__main__':
    unittest.main()
