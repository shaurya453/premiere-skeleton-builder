import unittest
from skeleton_builder import visual_links, align_cues

class PartialLinkTests(unittest.TestCase):
    def test_split_range_and_end_only_link_are_recovered_once(self):
        raw='Narration. (00:25 - 00:37) (5:39 - 5:49)'
        url='https://youtu.be/bEspSrrQvsM'
        links=[{'start':raw.index('00:25'),'end':raw.index('00:25')+7,'target':url},
               {'start':raw.index('00:37'),'end':raw.index('00:37')+3,'target':url},
               {'start':raw.index('5:49'),'end':raw.index('5:49')+4,'target':url}]
        warnings=[]; result=visual_links(raw,links,{},warnings)
        self.assertEqual([raw[l['start']:l['end']] for l in result],['00:25 - 00:37','5:39 - 5:49'])
        self.assertEqual(warnings,[])

    def test_named_bookmark_and_unrelated_web_link(self):
        raw='A giant bee mascot appears.'
        links=[{'start':2,'end':18,'target':'https://docs.google.com/document/d/test/edit#bookmark=id.bee'},
               {'start':19,'end':26,'target':'https://example.com'}]
        asset={'path':'bee.png'}
        found=visual_links(raw,links,{'bee':asset},[])
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['asset'],asset)
        self.assertTrue(found[0]['inline'])

    def test_ambiguous_range_is_flagged_instead_of_guessed(self):
        raw='0:25 - 0:37'
        links=[{'start':0,'end':4,'target':'https://youtu.be/bEspSrrQvsM'},
               {'start':7,'end':11,'target':'https://youtu.be/8EsUlE7czyM'}]
        warnings=[]
        self.assertFalse(visual_links(raw,links,{},warnings))
        self.assertTrue(any('multiple different' in w for w in warnings))

    def test_adjacent_image_and_video_share_time_without_overlap(self):
        cues=[{'kind':kind,'script_start':0,'script_end':3} for kind in ['image','video']]
        align_cues(['one','two','three'],cues,[{'token':t,'start':i,'end':i+1} for i,t in enumerate(['one','two','three'])])
        self.assertEqual(cues[0]['end'],cues[1]['start'])
        self.assertEqual(cues[1]['end'],3)

    def test_short_misrecognized_name_uses_nearby_anchors(self):
        cues=[{'kind':'image','script_start':1,'script_end':2}]
        words=[{'token':'meet','start':0,'end':1},{'token':'twirly','start':1,'end':2},{'token':'today','start':2,'end':3}]
        align_cues(['meet','twirlie','today'],cues,words)
        self.assertEqual((cues[0]['start'],cues[0]['end']),(1,2))
        self.assertIn('estimated',cues[0]['review'][0])
        words[-1]['start']=20
        align_cues(['meet','twirlie','today'],cues,words)
        self.assertIsNone(cues[0]['start'])
